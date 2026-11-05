#!/usr/bin/env python3
"""Native Messaging host with OS-backed encrypted profile storage.

stdout carries protocol frames; diagnostics go to stderr or log files
profile keys use Windows DPAPI, macOS Keychain or Linux Secret Service
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import contextlib
import concurrent.futures
import functools
import datetime as dt
import hashlib
import http.client
import json
import os
import platform
import re
import secrets as pysecrets
import shutil
import signal
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from dataclasses import dataclass
import configparser
from pathlib import Path
from typing import Any

# Domain modules are shared by the frozen host and source-based tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mhm.version import VERSION
from mhm.errors import HostError
from mhm.files import app_data_dir, ensure_private_parent, write_private_text
from mhm.config import (
    RealityProfile,
    parse_vless_url,
    _validate_subscription_url,
    checked_profile_id,
    load_mihomo_yaml,
    legacy_mihomo_yaml,
    profile_document,
    wireguard_proxy,
    build_mihomo_config,
    MAX_PROFILE_BYTES,
)
from mhm.tls import normalize_subscription_ca, subscription_tls_context
from mhm.traffic import TrafficStore
from mhm.secrets import (
    SecretBackend,
    FileSecretBackend,
    WindowsDpapiBackend,
    MacKeychainBackend,
    LinuxSecretBackend,
    choose_secret_backend,
)

from mhm.policy import (
    DEFAULT_TRAFFIC_POLICY,
    normalize_traffic_policy,
    traffic_budget,
    normalize_custom_rules,
    with_custom_rules,
    profile_revision,
)

from mhm import transport
from mhm.transport import download_subscription
from mhm.platforms import WindowsKillJob, _linux_child_setup
from mhm.profiles import ProfileService

from mhm.identity import NATIVE_HOST as HOST_NAME

APP_VERSION = VERSION
MAX_MESSAGE_SIZE = 12 * 1024 * 1024
PROTOCOL_VERSION = 1
HOST_VERSION = VERSION
DEFAULT_PORT = 10808
MAX_AUTO_PORT_SCAN = 200
# Commands the current protocol version supports, so the extension can
# feature-detect instead of guessing from HOST_VERSION.
HOST_CAPABILITIES = (
    "mihomoProfiles",
    "wireguardUpdates",
    "subscriptionCertificates",
    "trafficLimits",
    "customRules",
    "groups",
)


BASE_DIR = app_data_dir()
DIAGNOSTIC_LOG = BASE_DIR / "native_host.log"
SETTINGS_PATH = BASE_DIR / "settings.json"
PROFILES_PATH = BASE_DIR / "profiles.json"
SUBSCRIPTIONS_PATH = BASE_DIR / "subscriptions.json"
TRAFFIC_DB_PATH = BASE_DIR / "traffic.sqlite3"


_STORAGE_LOCK = threading.RLock()
_STORAGE_LOCAL = threading.local()


@contextlib.contextmanager
def storage_transaction():
    with _STORAGE_LOCK:
        if getattr(_STORAGE_LOCAL, "active", False):
            yield
            return
        lock_path = BASE_DIR / "storage.lock"
        ensure_private_parent(lock_path)
        with lock_path.open("a+b") as lock:
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + 90
                while True:
                    try:
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise HostError(
                                "Another browser is updating profiles. Try again shortly.",
                                "CONFLICT",
                            )
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX)
            _STORAGE_LOCAL.active = True
            try:
                yield
            finally:
                _STORAGE_LOCAL.active = False
                lock.seek(0)
                if os.name == "nt":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)


def stored_operation(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        with storage_transaction():
            return function(*args, **kwargs)

    return locked


def core_operation(function):
    @functools.wraps(function)
    def locked(self, *args, **kwargs):
        with self._lifecycle_lock:
            return function(self, *args, **kwargs)

    return locked


def log_event(message: str) -> None:
    try:
        ensure_private_parent(DIAGNOSTIC_LOG)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with DIAGNOSTIC_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{timestamp} pid={os.getpid()} {message}\n")
        try:
            os.chmod(DIAGNOSTIC_LOG, 0o600)
        except OSError:
            pass
    except Exception:
        pass


class SettingsStore:
    DEFAULTS = {"mihomoPath": "", "localPort": DEFAULT_PORT, "autoPort": True}

    def load(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            if SETTINGS_PATH.exists():
                value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    data = value
        except Exception as exc:
            log_event(f"settings read failed: {exc}")
        merged = {**self.DEFAULTS, **data}
        try:
            merged["localPort"] = validate_local_port(
                merged.get("localPort", DEFAULT_PORT)
            )
        except HostError:
            merged["localPort"] = DEFAULT_PORT
        merged["mihomoPath"] = str(merged.get("mihomoPath") or "").strip()
        merged["autoPort"] = bool(merged.get("autoPort", True))
        try:
            merged["trafficPolicy"] = normalize_traffic_policy(
                merged.get("trafficPolicy", {})
            )
        except HostError:
            merged["trafficPolicy"] = dict(DEFAULT_TRAFFIC_POLICY)
        return merged

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        if "mihomoPath" in patch:
            path = str(patch.get("mihomoPath") or "").strip()
            if path:
                expanded = os.path.abspath(os.path.expanduser(path))
                if not os.path.isfile(expanded):
                    raise HostError(
                        f"mihomo binary not found: {expanded}", "DEPENDENCY_UNAVAILABLE"
                    )
                path = expanded
            current["mihomoPath"] = path
        if "localPort" in patch:
            current["localPort"] = validate_local_port(patch["localPort"])
        if "autoPort" in patch:
            current["autoPort"] = bool(patch["autoPort"])
        if "trafficPolicy" in patch:
            current["trafficPolicy"] = normalize_traffic_policy(patch["trafficPolicy"])
        write_private_text(
            SETTINGS_PATH, json.dumps(current, ensure_ascii=False, indent=2)
        )
        return current


SETTINGS = SettingsStore()


def validate_local_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise HostError("Invalid local port") from exc
    if not (1024 <= port <= 65535):
        raise HostError("Local port must be in the 1024..65535 range")
    return port


def find_mihomo(explicit_path: str | None = None) -> str:
    settings_path = SETTINGS.load().get("mihomoPath", "")
    env_path = os.environ.get("MIHOMO_PATH", "").strip()
    candidates: list[str] = []
    for value in (explicit_path, settings_path, env_path):
        if value and str(value).strip():
            candidates.append(str(value).strip())

    here = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    candidates.extend(
        [
            str(here / ("mihomo.exe" if os.name == "nt" else "mihomo")),
            "mihomo.exe" if os.name == "nt" else "mihomo",
        ]
    )

    checked: list[str] = []
    for candidate in candidates:
        candidate = os.path.expanduser(candidate)
        checked.append(candidate)
        if (
            os.path.isabs(candidate)
            or os.sep in candidate
            or (os.altsep and os.altsep in candidate)
        ):
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        else:
            found = shutil.which(candidate)
            if found:
                return found

    raise HostError(
        "mihomo was not found. Set its path in extension settings, install mihomo in PATH "
        "or provide the path to the install script."
    )


SECRETS = choose_secret_backend(BASE_DIR)


def is_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def choose_local_port(preferred: int, auto_port: bool) -> int:
    preferred = validate_local_port(preferred)
    if is_port_available(preferred):
        return preferred
    if not auto_port:
        raise HostError(f"Local port {preferred} is already in use", "CONFLICT")
    for offset in range(1, MAX_AUTO_PORT_SCAN + 1):
        candidate = preferred + offset
        if candidate > 65535:
            candidate = 1023 + (candidate - 65535)
        if candidate < 1024:
            continue
        if is_port_available(candidate):
            return candidate
    raise HostError(f"Could not find a free local port near {preferred}")


TRAFFIC = TrafficStore(TRAFFIC_DB_PATH)


def _clash_connections(
    api_port: int, api_secret: str, timeout: float = 1.5
) -> dict[str, int]:
    connection = http.client.HTTPConnection("127.0.0.1", int(api_port), timeout=timeout)
    try:
        connection.request(
            "GET", "/connections", headers={"Authorization": f"Bearer {api_secret}"}
        )
        response = connection.getresponse()
        if response.status != 200:
            raise HostError(f"mihomo stats API returned HTTP {response.status}")
        payload = response.read(2 * 1024 * 1024)
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise HostError("mihomo stats API returned invalid data")
        return {
            "downloadTotal": max(0, int(data.get("downloadTotal", 0) or 0)),
            "uploadTotal": max(0, int(data.get("uploadTotal", 0) or 0)),
            "connections": (
                len(data.get("connections", []))
                if isinstance(data.get("connections"), list)
                else 0
            ),
        }
    finally:
        connection.close()


def cleanup_runtime_directory(directory: str):
    path = Path(directory).resolve()
    if path.parent == (BASE_DIR / "runtime").resolve() and path.name.startswith("mhm-"):
        shutil.rmtree(path, ignore_errors=True)


def _windows_watch_parent(
    parent_pid: int, child_pid: int, directory: str | None = None
) -> int:
    """Wait for the native host to die, then terminate its mihomo child.

    The primary Windows containment mechanism is a kill-on-close Job Object.
    This process is a fallback for environments where job assignment is denied
    (for example by an outer job policy). It is launched detached from stdio.
    """
    if os.name != "nt":
        return 2
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    SYNCHRONIZE = 0x00100000
    PROCESS_TERMINATE = 0x0001
    INFINITE = 0xFFFFFFFF

    parent = kernel32.OpenProcess(SYNCHRONIZE, False, int(parent_pid))
    if parent:
        try:
            kernel32.WaitForSingleObject(ctypes.c_void_p(parent), INFINITE)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(parent))

    child = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, int(child_pid))
    if child:
        try:
            kernel32.TerminateProcess(ctypes.c_void_p(child), 1)
            kernel32.WaitForSingleObject(ctypes.c_void_p(child), 5000)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(child))
    if directory:
        cleanup_runtime_directory(directory)
    return 0


class ManagedCoreProcess:
    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.config_path: Path | None = None
        self.log_path: Path | None = None
        self.log_handle = None
        self.profile_id: str | None = None
        self.local_port: int | None = None
        self.api_port: int | None = None
        self.api_secret: str | None = None
        self.mihomo_path: str | None = None
        self.started_at: int | None = None
        self.session_id: int | None = None
        self._job: WindowsKillJob | None = None
        self._win_watcher: subprocess.Popen[bytes] | None = None
        self._mac_watcher: subprocess.Popen[bytes] | None = None
        self._telemetry_thread: threading.Thread | None = None
        self._telemetry_stop = threading.Event()
        self._telemetry_lock = threading.Lock()
        self._download_total = 0
        self._upload_total = 0
        self._download_bps = 0
        self._upload_bps = 0
        self._connections = 0
        self._last_sample_at: float | None = None
        self._telemetry_error: str | None = None
        self._last_db_flush = 0.0
        self.stop_reason = None

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _reset_telemetry(self) -> None:
        with self._telemetry_lock:
            self._download_total = 0
            self._upload_total = 0
            self._download_bps = 0
            self._upload_bps = 0
            self._connections = 0
            self._last_sample_at = None
            self._telemetry_error = None
            self._last_db_flush = 0.0

    def _sample_telemetry(self, force_db: bool = False) -> None:
        if not self.running() or not self.api_port or not self.api_secret:
            return
        now = time.monotonic()
        try:
            sample = _clash_connections(self.api_port, self.api_secret)
        except Exception as exc:
            with self._telemetry_lock:
                self._telemetry_error = str(exc)
            return

        with self._telemetry_lock:
            previous_down = self._download_total
            previous_up = self._upload_total
            previous_at = self._last_sample_at
            down = sample["downloadTotal"]
            up = sample["uploadTotal"]
            if (
                previous_at is not None
                and now > previous_at
                and down >= previous_down
                and up >= previous_up
            ):
                elapsed = max(now - previous_at, 0.001)
                delta_down = down - previous_down
                delta_up = up - previous_up
                self._download_bps = int(delta_down / elapsed)
                self._upload_bps = int(delta_up / elapsed)
            else:
                # A new mihomo process starts counters at zero. If the first API
                # sample already contains traffic, account for it instead of losing it.
                delta_down = max(
                    0,
                    (
                        down
                        if previous_at is None or down < previous_down
                        else down - previous_down
                    ),
                )
                delta_up = max(
                    0,
                    up if previous_at is None or up < previous_up else up - previous_up,
                )
                self._download_bps = 0
                self._upload_bps = 0
            self._download_total = down
            self._upload_total = up
            self._connections = sample["connections"]
            self._last_sample_at = now
            self._telemetry_error = None
            should_flush = force_db or (now - self._last_db_flush >= 5.0)
            if should_flush:
                self._last_db_flush = now
            session_id = self.session_id
            values = (
                self._download_total,
                self._upload_total,
                self._download_bps,
                self._upload_bps,
                self._connections,
            )
        try:
            TRAFFIC.record_delta(delta_down, delta_up)
        except Exception as exc:
            log_event(f"traffic daily aggregate update failed: {exc}")
        if should_flush and session_id:
            try:
                TRAFFIC.update_session(session_id, *values)
            except Exception as exc:
                log_event(f"traffic db update failed: {exc}")

    def _telemetry_loop(self) -> None:
        while not self._telemetry_stop.wait(1.0):
            if not self.running():
                break
            self._sample_telemetry()
            try:
                budget = traffic_budget(TRAFFIC, SETTINGS.load()["trafficPolicy"])
                if budget["blocked"]:
                    self.stop_reason = "traffic-limit"
                    self.stop()
                    break
            except Exception:
                log_event("traffic budget check failed")

    def _start_telemetry(self) -> None:
        self._telemetry_stop.clear()
        self._reset_telemetry()
        self._sample_telemetry(force_db=True)
        thread = threading.Thread(
            target=self._telemetry_loop, name="mihomo-telemetry", daemon=True
        )
        self._telemetry_thread = thread
        thread.start()

    def _stop_telemetry(self) -> None:
        self._telemetry_stop.set()
        thread = self._telemetry_thread
        self._telemetry_thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        try:
            self._sample_telemetry(force_db=True)
        except Exception:
            pass
        if self.session_id:
            with self._telemetry_lock:
                values = (
                    self._download_total,
                    self._upload_total,
                    self._download_bps,
                    self._upload_bps,
                    self._connections,
                )
            try:
                TRAFFIC.update_session(self.session_id, *values, finish=True)
            except Exception as exc:
                log_event(f"traffic db finalize failed: {exc}")
            self.session_id = None

    def _spawn_process(self, binary: str) -> subprocess.Popen[bytes]:
        command = [binary, "-d", str(self.directory), "-f", str(self.config_path)]
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": self.log_handle,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        elif sys.platform.startswith("linux"):
            parent_pid = os.getpid()
            kwargs["preexec_fn"] = lambda: _linux_child_setup(parent_pid)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)

        if os.name == "nt":
            job = None
            try:
                job = WindowsKillJob()
                job.assign(process.pid)
                self._job = job
            except Exception as exc:
                if job is not None:
                    try:
                        job.close()
                    except Exception:
                        pass
                self._job = None
                log_event(
                    f"Windows Job Object setup failed; enabling watchdog fallback: {exc}"
                )
            self._start_windows_watchdog(process.pid)
        elif sys.platform == "darwin":
            self._start_macos_watchdog(process.pid)
        return process

    def _start_windows_watchdog(self, child_pid: int) -> None:
        if os.name != "nt":
            return
        if getattr(sys, "frozen", False):
            command = [
                sys.executable,
                "--watch-parent",
                str(os.getpid()),
                str(child_pid),
            ]
        else:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--watch-parent",
                str(os.getpid()),
                str(child_pid),
            ]
        command.append(str(self.directory))
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "creationflags": no_window | breakaway,
        }
        try:
            self._win_watcher = subprocess.Popen(command, **kwargs)
        except Exception as exc:
            # Some parent jobs disallow breakaway. A non-breakaway watcher is
            # still useful when the outer job does not kill all descendants.
            log_event(f"Windows watchdog breakaway launch failed: {exc}")
            try:
                kwargs["creationflags"] = no_window
                self._win_watcher = subprocess.Popen(command, **kwargs)
            except Exception as fallback_exc:
                self._win_watcher = None
                log_event(f"Windows watchdog fallback launch failed: {fallback_exc}")

    def _stop_windows_watchdog(self) -> None:
        watcher = self._win_watcher
        self._win_watcher = None
        if watcher and watcher.poll() is None:
            try:
                watcher.terminate()
                watcher.wait(timeout=1)
            except Exception:
                try:
                    watcher.kill()
                except Exception:
                    pass

    def _start_macos_watchdog(self, child_pid: int) -> None:
        zsh = "/bin/zsh"
        if not Path(zsh).exists():
            return
        script = (
            'parent="$1"; child="$2"; '
            'while kill -0 "$parent" 2>/dev/null; do sleep 1; done; '
            'kill -TERM -- -"$child" 2>/dev/null || true; sleep 2; '
            'kill -KILL -- -"$child" 2>/dev/null || true; '
            'rm -rf -- "$3"'
        )
        try:
            self._mac_watcher = subprocess.Popen(
                [
                    zsh,
                    "-c",
                    script,
                    "watchdog",
                    str(os.getpid()),
                    str(child_pid),
                    str(self.directory),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            log_event(f"macOS watchdog setup failed: {exc}")

    def _stop_macos_watchdog(self) -> None:
        watcher = self._mac_watcher
        self._mac_watcher = None
        if watcher and watcher.poll() is None:
            try:
                watcher.terminate()
                watcher.wait(timeout=1)
            except Exception:
                try:
                    watcher.kill()
                except Exception:
                    pass

    def stop(self) -> None:
        proc = self.process
        if proc and proc.poll() is None:
            try:
                self._sample_telemetry(force_db=True)
            except Exception:
                pass
        self._stop_telemetry()
        self.process = None

        if proc and proc.poll() is None:
            try:
                if os.name != "nt":
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        proc.terminate()
                else:
                    proc.terminate()
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            except Exception:
                pass

        self._stop_macos_watchdog()
        self._stop_windows_watchdog()
        if self._job:
            try:
                self._job.close()
            except Exception:
                pass
            self._job = None

        if self.log_handle:
            try:
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None

        if self.config_path:
            try:
                self.config_path.unlink(missing_ok=True)
            except Exception:
                pass
            self.config_path = None

        if self.log_path:
            try:
                self.log_path.unlink(missing_ok=True)
            except Exception:
                pass
            self.log_path = None

        self.profile_id = None
        self.local_port = None
        self.api_port = None
        self.api_secret = None
        self.mihomo_path = None
        self.started_at = None
        self._reset_telemetry()

    def _remove_config_file(self) -> None:
        if not self.config_path:
            return
        try:
            self.config_path.unlink(missing_ok=True)
            self.config_path = None
        except OSError as exc:
            log_event(f"could not remove active config after startup: {exc}")

    def traffic_snapshot(self) -> dict[str, Any]:
        with self._telemetry_lock:
            last_age = None
            if self._last_sample_at is not None:
                last_age = max(0.0, time.monotonic() - self._last_sample_at)
            session = {
                "downloadBytes": self._download_total,
                "uploadBytes": self._upload_total,
                "downloadBps": self._download_bps,
                "uploadBps": self._upload_bps,
                "connections": self._connections,
                "sampleAgeSeconds": (
                    round(last_age, 2) if last_age is not None else None
                ),
                "telemetryError": self._telemetry_error,
            }
        summary = TRAFFIC.summary()
        return {
            "session": session,
            **summary,
            "budget": traffic_budget(TRAFFIC, SETTINGS.load()["trafficPolicy"]),
        }


def read_message() -> dict[str, Any] | None:
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        return None
    if len(raw_length) != 4:
        raise HostError("Corrupted Native Messaging header")
    length = struct.unpack("@I", raw_length)[0]
    if length <= 0 or length > MAX_MESSAGE_SIZE:
        raise HostError(f"Invalid message size: {length}")
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        raise HostError("Native Messaging message was truncated")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostError("Native Messaging received invalid JSON") from exc
    if not isinstance(value, dict):
        raise HostError("Native Messaging expects a JSON object")
    return value


def write_message(message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > 1024 * 1024:
        raise HostError("Native Messaging response is too large")
    sys.stdout.buffer.write(struct.pack("@I", len(payload)))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def handle(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("protocolVersion", PROTOCOL_VERSION) != PROTOCOL_VERSION:
        raise HostError(
            "Update the extension and companion app together", "UNSUPPORTED_PROTOCOL"
        )
    request_id = message.get("requestId")
    if "protocolVersion" in message and request_id is None:
        raise HostError("requestId is required", "INVALID_REQUEST")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id or len(request_id) > 128
    ):
        raise HostError("Invalid requestId", "INVALID_REQUEST")
    command = message.get("command")
    if not isinstance(command, str) or not command or len(command) > 64:
        raise HostError(
            "command must be a non-empty string up to 64 characters", "INVALID_REQUEST"
        )
    return handle_mihomo(message)


def security_info() -> dict[str, Any]:
    return SECRETS.info()


# Mihomo profiles keep source material in the OS-backed secret store.


def validate_mihomo(document: dict[str, Any], profile_id: str) -> None:
    import yaml

    binary = find_mihomo()
    directory = BASE_DIR / "runtime"
    ensure_private_parent(directory / "placeholder")
    with tempfile.TemporaryDirectory(prefix="check-", dir=directory) as temporary:
        directory = Path(temporary)
        path = directory / "config.yaml"
        config = build_mihomo_config(
            document, directory, DEFAULT_PORT, 19090, pysecrets.token_urlsafe(24)
        )
        write_private_text(
            path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        )
        try:
            result = subprocess.run(
                [binary, "-t", "-d", str(directory), "-f", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise HostError("Profile validation timed out", "TIMEOUT") from exc
        if result.returncode:
            raise HostError(
                "mihomo rejected this profile. Check proxy names, groups and rules."
            )


class MihomoProcess(ManagedCoreProcess):
    def __init__(self) -> None:
        super().__init__()
        self._lifecycle_lock = threading.RLock()
        self.directory = None
        self.revision = None
        self.mode = "rule"
        self._document = None

    def restore_resources(self, profile_id):
        import zipfile

        try:
            data = SECRETS.get("resources-" + profile_id)
        except HostError as exc:
            if exc.code == "NOT_FOUND":
                return
            raise
        import io

        with zipfile.ZipFile(io.BytesIO(base64.b64decode(data))) as archive:
            total = 0
            for entry in archive.infolist():
                total += entry.file_size
                if total > 32 * 1024 * 1024 or not re.fullmatch(
                    r"[0-9a-f]{64}\.yaml", entry.filename
                ):
                    raise HostError("Invalid encrypted provider cache")
                path = self.directory / entry.filename
                path.write_bytes(archive.read(entry))
                os.chmod(path, 0o600)

    @core_operation
    def stop(self):
        directory = self.directory
        profile_id = self.profile_id
        super().stop()
        if directory and directory.exists():
            try:
                if profile_id:
                    import io
                    import zipfile

                    buffer = io.BytesIO()
                    total = 0
                    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                        for file in directory.iterdir():
                            if (
                                re.fullmatch(r"[0-9a-f]{64}\.yaml", file.name)
                                and file.is_file()
                            ):
                                total += file.stat().st_size
                                if total > 32 * 1024 * 1024:
                                    raise HostError("Provider cache exceeds 32 MB")
                                archive.write(file, file.name)
                    with storage_transaction():
                        SECRETS.put(
                            "resources-" + profile_id,
                            base64.b64encode(buffer.getvalue()).decode(),
                        )
            except Exception:
                log_event("provider cache could not be saved")
            finally:
                shutil.rmtree(directory, ignore_errors=True)
        self.directory = None
        self._document = None
        self.revision = None

    def api(self, method: str, path: str, data=None, timeout=8):
        if not self.running() or not self.api_port:
            raise HostError("Connect a profile first", "CONFLICT")
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.api_port}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_secret}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=timeout
            ) as response:
                content = response.read(MAX_PROFILE_BYTES + 1)
                if len(content) > MAX_PROFILE_BYTES:
                    raise HostError("Controller response is too large")
                return json.loads(content) if content else {}
        except Exception as exc:
            raise HostError(
                "Could not communicate with mihomo", "DEPENDENCY_UNAVAILABLE"
            ) from exc

    @core_operation
    def start_document(
        self, document: dict[str, Any], profile_id: str | None = None, revision=None
    ) -> dict[str, Any]:
        import yaml

        if self.running():
            self.stop()
        try:
            settings = SETTINGS.load()
            runtime_root = BASE_DIR / "runtime"
            ensure_private_parent(runtime_root / "placeholder")
            self.directory = Path(tempfile.mkdtemp(prefix="mhm-", dir=runtime_root))
            if profile_id:
                self.restore_resources(profile_id)
            self.local_port = choose_local_port(settings["localPort"], True)
            self.api_port = choose_local_port(
                self.local_port + 1 if self.local_port < 65535 else 19090, True
            )
            self.api_secret = pysecrets.token_urlsafe(32)
            config = build_mihomo_config(
                document,
                self.directory,
                self.local_port,
                self.api_port,
                self.api_secret,
            )
            saved = PROFILES.bundle(profile_id) if profile_id else {}
            config["mode"] = saved.get("mode", config.get("mode", "rule"))
            self.mode = config["mode"]
            fd, path = tempfile.mkstemp(
                prefix="runtime-", suffix=".yaml", dir=self.directory
            )
            os.close(fd)
            self.config_path = Path(path)
            write_private_text(
                self.config_path,
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            )
            self.profile_id, self.revision = profile_id, revision
            self._document = document
            self.mihomo_path = find_mihomo()
            self.log_handle = open(os.devnull, "wb")
            self.process = self._spawn_process(self.mihomo_path)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise HostError("mihomo could not start this profile")
                try:
                    self.api("GET", "/version", timeout=0.3)
                    with socket.create_connection(
                        ("127.0.0.1", self.local_port), timeout=0.3
                    ):
                        break
                except (HostError, OSError):
                    time.sleep(0.1)
            else:
                raise HostError("mihomo startup timed out", "TIMEOUT")
            self.started_at = int(time.time())
            for group, choice in saved.get("selections", {}).items():
                with contextlib.suppress(HostError):
                    self.api(
                        "PUT",
                        "/proxies/" + urllib.parse.quote(group, safe=""),
                        {"name": choice},
                    )
            if profile_id:
                self.session_id = TRAFFIC.start_session(
                    profile_id, PROFILES.get_meta(profile_id)["name"], "mihomo"
                )
                self._start_telemetry()
            return self.status()
        except Exception:
            self.stop()
            raise
        finally:
            # mihomo has loaded the configuration. Keep source YAML encrypted.
            self._remove_config_file()

    @core_operation
    def connect(self, profile_id: str) -> dict[str, Any]:
        with storage_transaction():
            meta, bundle = PROFILES.get_meta(profile_id), PROFILES.bundle(profile_id)
        if traffic_budget(TRAFFIC, SETTINGS.load()["trafficPolicy"])["blocked"]:
            self.stop_reason = "traffic-limit"
            raise HostError(
                "Traffic limit reached. Increase or disable the limit in Traffic settings.",
                "TRAFFIC_LIMIT",
            )
        if (
            self.running()
            and self.profile_id == profile_id
            and self.revision == meta.get("revision")
        ):
            return self.status()
        self.stop_reason = None
        document = with_custom_rules(profile_document(bundle["yaml"]), bundle)
        validate_mihomo(document, profile_id)
        previous = (
            (self._document, self.profile_id, self.revision) if self.running() else None
        )
        try:
            return self.start_document(document, profile_id, meta.get("revision"))
        except Exception as exc:
            if previous:
                with contextlib.suppress(Exception):
                    self.start_document(*previous)
            raise exc

    def restart(self):
        if not self.profile_id:
            raise HostError("Connect a profile first")
        return self.connect(self.profile_id)

    def status(self):
        process = self.process
        running = process is not None and process.poll() is None
        return {
            "status": "connected" if running else "disconnected",
            "profileId": self.profile_id if running else None,
            "revision": self.revision if running else None,
            "localPort": self.local_port if running else None,
            "pid": process.pid if running else None,
            "mode": self.mode,
            "startedAt": self.started_at if running else None,
            "health": {"healthy": running},
            "stopReason": self.stop_reason,
            "traffic": self.traffic_snapshot() if running else None,
        }

    def groups(self):
        data = self.api("GET", "/proxies").get("proxies", {})
        return [
            {
                "name": name,
                "type": p.get("type"),
                "now": p.get("now"),
                "choices": [
                    {
                        "name": n,
                        "alive": data.get(n, {}).get("alive"),
                        "delay": (data.get(n, {}).get("history") or [{}])[-1].get(
                            "delay"
                        ),
                    }
                    for n in p.get("all", [])
                ],
            }
            for name, p in data.items()
            if p.get("all") and (name != "GLOBAL" or self.mode == "global")
        ]

    def select(self, group: str, choice: str):
        groups = self.groups()
        target = next((g for g in groups if g["name"] == group), None)
        if (
            not target
            or target["type"] != "Selector"
            or choice not in [c["name"] for c in target["choices"]]
        ):
            raise HostError("Choose an available proxy in a manual selection group")
        self.api(
            "PUT", "/proxies/" + urllib.parse.quote(group, safe=""), {"name": choice}
        )
        bundle = PROFILES.bundle(self.profile_id)
        bundle.setdefault("selections", {})[group] = choice
        PROFILES.commit(self.profile_id, PROFILES.get_meta(self.profile_id), bundle)


def fetch_mihomo_subscription(
    bundle: dict[str, Any], transport: str
) -> tuple[str, dict | None]:
    import yaml

    temporary = None
    try:
        proxy_port = None
        if transport == "wireguard":
            outbound = wireguard_proxy(bundle.get("wireguard", ""))
            temporary = MihomoProcess()
            temporary.start_document(
                {
                    "proxies": [outbound],
                    "rules": ["MATCH,mhm WireGuard"],
                    # The WireGuard outbound resolves names through the DNS
                    # servers supplied by the .conf, including internal zones.
                    "dns": {"enable": False},
                }
            )
            proxy_port = temporary.local_port
        body, headers = download_subscription(
            bundle["url"],
            bundle.get("headers", {}),
            proxy_port,
            tls_options={
                "mode": bundle.get("tlsMode", "system"),
                "caCertificate": bundle.get("caCertificate", ""),
            },
        )
        text = body.decode("utf-8-sig")
        profile_document(text)
        usage = {}
        for part in headers.get("subscription-userinfo", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key in {"upload", "download", "total", "expire"} and value.isdigit():
                usage[key] = int(value)
        return text, usage or None
    except HostError:
        raise
    except Exception as exc:
        raise HostError(
            "Subscription download failed. Check the URL, connection and WireGuard configuration."
        ) from exc
    finally:
        if temporary:
            directory = temporary.directory
            temporary.stop()
            if directory:
                shutil.rmtree(directory, ignore_errors=True)


def create_profile_store(index_path=None, secrets=None, subscriptions_path=None):
    return ProfileService(
        index_path or PROFILES_PATH,
        secrets or SECRETS,
        validate=lambda document, pid: validate_mihomo(document, pid),
        fetch=lambda bundle, transport: fetch_mihomo_subscription(bundle, transport),
        transaction=storage_transaction,
        subscriptions_path=subscriptions_path or SUBSCRIPTIONS_PATH,
        is_active=lambda pid: CLIENT.running() and CLIENT.profile_id == pid,
    )


PROFILES = create_profile_store()
CLIENT = MihomoProcess()
atexit.register(CLIENT.stop)


@stored_operation
def migrate_legacy_profiles():
    for meta in PROFILES._load_metadata():
        if meta.get("format") == "mihomo":
            continue
        bundle = PROFILES.bundle(meta["id"])
        document = profile_document(bundle["yaml"])
        import yaml

        bundle["yaml"] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        revision = hashlib.sha256(bundle["yaml"].encode()).hexdigest()
        PROFILES.commit(
            meta["id"],
            {
                **meta,
                "source": "local",
                "revision": revision,
                "proxyCount": len(document.get("proxies", [])),
                "groupCount": len(document.get("proxy-groups", [])),
                "hasRollback": False,
            },
            bundle,
        )
        with contextlib.suppress(HostError):
            SECRETS.delete(meta["id"])

    try:
        legacy_subscriptions = (
            json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))
            if SUBSCRIPTIONS_PATH.exists()
            else []
        )
    except (OSError, ValueError):
        legacy_subscriptions = []
    migrated = {p.get("legacySubscriptionId") for p in PROFILES._load_metadata()}
    for subscription in legacy_subscriptions:
        if (
            not isinstance(subscription, dict)
            or not subscription.get("id")
            or subscription["id"] in migrated
        ):
            continue
        try:
            source_id = str(subscription["id"])
            raw_url = SECRETS.get("subscription:" + source_id)
            profile_id = "sub-" + hashlib.sha256(source_id.encode()).hexdigest()[:32]
            needs_transport = subscription.get("transport") == "ssh"
            bundle = {
                "source": "subscription",
                "url": raw_url,
                "headers": {},
                "yaml": "",
                "wireguard": "",
            }
            PROFILES.commit(
                profile_id,
                {
                    "name": subscription.get("name") or "Subscription",
                    "source": "subscription",
                    "legacySubscriptionId": source_id,
                    "needsDownload": True,
                    "needsTransportSetup": needs_transport,
                    "transport": "wireguard" if needs_transport else "direct",
                    "updateIntervalMinutes": 0,
                    "revision": None,
                    "hasRollback": False,
                    "createdAt": subscription.get("createdAt", int(time.time())),
                },
                bundle,
            )
        except HostError:
            log_event("one legacy subscription could not be migrated")


def handle_mihomo(message: dict[str, Any]) -> dict[str, Any]:
    command = message.get("command")
    if not SECRETS.secure and command not in {"ping", "getSettings", "saveSettings"}:
        raise HostError(
            "OS secure storage is required. Unlock Keychain or sign in to Windows.",
            "DEPENDENCY_UNAVAILABLE",
        )
    if command == "ping":
        try:
            binary = find_mihomo()
        except HostError:
            binary = None
        return {
            "status": "ok",
            "mihomo": binary,
            "security": security_info(),
            "capabilities": list(HOST_CAPABILITIES),
            "settings": SETTINGS.load(),
        }
    if command in {"bootstrap", "snapshot", "listProfiles"}:
        migrate_legacy_profiles()
        return {
            "profiles": PROFILES.list(),
            "status": CLIENT.status(),
            "security": security_info(),
            "capabilities": list(HOST_CAPABILITIES),
        }
    if command == "saveMihomoProfile":
        return {"profile": PROFILES.save_source(message), "profiles": PROFILES.list()}
    if command in {"updateMihomoProfile", "rollbackMihomoProfile"}:
        pid = str(message.get("profileId") or "")
        item = (
            PROFILES.update(pid)
            if command == "updateMihomoProfile"
            else PROFILES.rollback(pid)
        )
        return {"profile": item, "profiles": PROFILES.list()}
    if command == "deleteProfile":
        pid = str(message.get("profileId") or "")
        if CLIENT.running() and CLIENT.profile_id == pid:
            raise HostError("Disconnect this profile before deleting it", "CONFLICT")
        PROFILES.delete(pid)
        return {"profiles": PROFILES.list()}
    if command == "connect":
        if PROFILES.get_meta(str(message.get("profileId") or "")).get("needsDownload"):
            raise HostError("Download the subscription first")
        return CLIENT.connect(str(message.get("profileId") or ""))
    if command == "disconnect":
        CLIENT.stop()
        return CLIENT.status()
    if command == "status":
        return CLIENT.status()
    if command == "groups":
        return {"groups": CLIENT.groups()}
    if command == "selectProxy":
        CLIENT.select(str(message.get("group") or ""), str(message.get("proxy") or ""))
        return {"groups": CLIENT.groups()}
    if command == "setMode":
        mode = message.get("mode")
        if mode not in {"rule", "global", "direct"}:
            raise HostError("Invalid mode")
        CLIENT.api("PATCH", "/configs", {"mode": mode})
        CLIENT.mode = mode
        bundle = PROFILES.bundle(CLIENT.profile_id)
        bundle["mode"] = mode
        PROFILES.commit(CLIENT.profile_id, PROFILES.get_meta(CLIENT.profile_id), bundle)
        return CLIENT.status()
    if command == "proxyDelay":
        name = str(message.get("proxy") or "")
        path = (
            "/proxies/"
            + urllib.parse.quote(name, safe="")
            + "/delay?"
            + urllib.parse.urlencode(
                {"url": "https://www.gstatic.com/generate_204", "timeout": 5000}
            )
        )
        return CLIENT.api("GET", path)
    if command == "updateDueProfiles":
        updated, failed = [], []
        now = int(time.time())
        for item in PROFILES.list():
            interval = int(item.get("updateIntervalMinutes") or 0)
            if (
                item.get("source") == "subscription"
                and interval
                and now - int(item.get("lastAttemptAt") or 0) >= interval * 60
            ):
                try:
                    PROFILES.update(item["id"])
                    updated.append(item["id"])
                except Exception:
                    failed.append(item["id"])
        return {"updated": updated, "failed": failed, "profiles": PROFILES.list()}
    if command == "getSettings":
        return {"settings": SETTINGS.load()}
    if command == "saveSettings":
        return {
            "settings": SETTINGS.save(
                {
                    k: message[k]
                    for k in ("mihomoPath", "localPort", "autoPort")
                    if k in message
                }
            )
        }
    if command == "trafficOverview":
        return {
            "traffic": {
                **TRAFFIC.summary(),
                "budget": traffic_budget(TRAFFIC, SETTINGS.load()["trafficPolicy"]),
                "series": TRAFFIC.daily_series(),
                "sessions": TRAFFIC.recent_sessions(),
            }
        }
    if command == "saveTrafficPolicy":
        settings = SETTINGS.save({"trafficPolicy": message.get("policy", {})})
        return {"budget": traffic_budget(TRAFFIC, settings["trafficPolicy"])}
    if command == "profileRules":
        pid = str(message.get("profileId") or "")
        bundle = PROFILES.bundle(pid)
        document = profile_document(bundle["yaml"])
        return {
            "rules": bundle.get("customRules", []),
            "targets": ["DIRECT", "REJECT"]
            + [
                g["name"]
                for g in document.get("proxy-groups", [])
                if isinstance(g, dict) and isinstance(g.get("name"), str)
            ],
        }
    if command == "saveProfileRules":
        pid = str(message.get("profileId") or "")
        with storage_transaction():
            meta, bundle = PROFILES.get_meta(pid), PROFILES.bundle(pid)
            bundle["customRules"] = normalize_custom_rules(message.get("rules", []))
            validate_mihomo(
                with_custom_rules(profile_document(bundle["yaml"]), bundle), pid
            )
            meta.update(revision=profile_revision(bundle), updatedAt=int(time.time()))
            item = PROFILES.commit(pid, meta, bundle)
        return {
            "profile": item,
            "profiles": PROFILES.list(),
            "rules": bundle["customRules"],
        }
    if command == "profileSource":
        bundle = PROFILES.bundle(str(message.get("profileId") or ""))
        return {
            "source": bundle.get("source", "local"),
            "url": bundle.get("url", ""),
            "yamlLength": len(bundle.get("yaml", "")),
            "headers": bundle.get("headers", {}),
            "hasWireguard": bool(bundle.get("wireguard")),
            "tlsMode": bundle.get("tlsMode", "system"),
            "caCertificate": bundle.get("caCertificate", ""),
        }
    if command == "profileContent":
        text = PROFILES.bundle(str(message.get("profileId") or ""))["yaml"]
        offset = max(0, int(message.get("offset") or 0))
        return {
            "text": text[offset : offset + 100000],
            "nextOffset": offset + 100000 if offset + 100000 < len(text) else None,
        }
    if command == "trafficStats":
        return {
            "traffic": (
                CLIENT.traffic_snapshot()
                if CLIENT.running()
                else {
                    "session": None,
                    **TRAFFIC.summary(),
                    "budget": traffic_budget(TRAFFIC, SETTINGS.load()["trafficPolicy"]),
                }
            )
        }
    raise HostError("Unsupported command", "UNKNOWN_COMMAND")


def error_code_for(exc: Exception) -> str:
    """HostError always carries the code its raiser intended. Any other
    exception type (sqlite3.Error, OSError, ...) is an unanticipated
    failure and is reported as INTERNAL_ERROR rather than guessed at from
    its message text -- text-sniffing previously produced wrong codes
    whenever incidental wording ("invalid", "active", ...) appeared in an
    unrelated error.
    """
    return exc.code if isinstance(exc, HostError) else "INTERNAL_ERROR"


def log_caller_origin(argv: list[str]) -> None:
    """Chromium browsers invoke a Native Messaging host with the calling
    extension's origin as the first CLI argument (e.g.
    ``chrome-extension://<32-char-id>/``); Firefox passes its own origin
    similarly. The browser already enforces ``allowed_origins`` from the
    host manifest before ever launching this process, so this is a
    diagnostic signal for an unexpected caller, not an access control gate
    on its own -- treat only strict Chromium builds. Assumptions about the
    exact argv layout across Firefox/Chromium versions are not something a
    manifest change alone can guarantee, so this never blocks a request.
    """
    if len(argv) < 2:
        log_event("caller origin: not provided by the browser")
        return
    origin = argv[1]
    if re.match(r"^(chrome-extension|moz-extension)://[^/]+/?$", origin):
        log_event(f"caller origin: {origin}")
    else:
        log_event(f"caller origin: unrecognized argv[1] format {origin!r}")


def configure_binary_stdio_on_windows() -> None:
    if os.name != "nt":
        return
    import msvcrt  # type: ignore

    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def main() -> int:
    if len(sys.argv) in {4, 5} and sys.argv[1] == "--watch-parent":
        try:
            return _windows_watch_parent(
                int(sys.argv[2]),
                int(sys.argv[3]),
                sys.argv[4] if len(sys.argv) == 5 else None,
            )
        except (TypeError, ValueError):
            return 2

    configure_binary_stdio_on_windows()
    ensure_private_parent(DIAGNOSTIC_LOG)
    log_event(
        f"host start; frozen={bool(getattr(sys, 'frozen', False))}; argv_count={len(sys.argv)}; "
        f"secret_backend={SECRETS.name}"
    )
    log_caller_origin(sys.argv)

    def shutdown(_signum, _frame):
        raise SystemExit(0)

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            try:
                signal.signal(sig, shutdown)
            except Exception:
                pass

    output_lock = threading.Lock()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="mhm-operations"
    )

    def respond(message):
        request_id = message.get("requestId")
        try:
            result = handle(message)
            response = {
                "ok": True,
                "protocolVersion": PROTOCOL_VERSION,
                "hostVersion": HOST_VERSION,
                **result,
            }
        except Exception as exc:
            code = error_code_for(exc)
            # Unexpected exceptions may contain source material or credentials.
            error = (
                str(exc)
                if isinstance(exc, HostError)
                else "The companion app could not complete this operation."
            )
            log_event(f"request failed: errorCode={code}")
            response = {
                "ok": False,
                "protocolVersion": PROTOCOL_VERSION,
                "hostVersion": HOST_VERSION,
                "errorCode": code,
                "error": error,
            }
        if request_id is not None:
            response["requestId"] = request_id
        with output_lock:
            try:
                write_message(response)
            except HostError:
                write_message(
                    {
                        "ok": False,
                        "protocolVersion": PROTOCOL_VERSION,
                        "hostVersion": HOST_VERSION,
                        "requestId": request_id,
                        "errorCode": "INVALID_VALUE",
                        "error": "This response exceeds the browser message limit. Reduce the profile size.",
                    }
                )
            except (BrokenPipeError, OSError):
                pass

    try:
        while True:
            message = read_message()
            if message is None:
                return 0
            if message.get("command") in {"status", "trafficStats", "disconnect"}:
                respond(message)
            else:
                executor.submit(respond, message)
    except HostError:
        log_event("invalid native messaging frame")
        return 2
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        CLIENT.stop()
        log_event("host cleanup complete")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Install the native host and register the selected browser.

browser ids are resolved on the target machine before registration
standalone installers download verified mihomo releases; deb packages bundle it
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import urllib.parse
import webbrowser
import zipfile
from typing import Any, Iterable

from setup._version import VERSION
from setup.browsers import BROWSERS

PRODUCT_MARKER = "mhm-extension.json"
PRODUCT_MARKER_ID = "io.mhm.browser.extension"
from setup._identity import NATIVE_HOST as HOST_NAME, FIREFOX_ID as FIREFOX_EXTENSION_ID

PRODUCT_VERSION = VERSION
EXTENSION_ID_RE = re.compile(r"(?<![a-p])([a-p]{32})(?![a-p])")
FIREFOX_EXTENSION_ID_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
GITHUB_API = "https://api.github.com/repos/MetaCubeX/mihomo"
CHROME_WEB_STORE_HOSTS = {"chromewebstore.google.com", "chrome.google.com"}
OPERA_ADDONS_HOSTS = {
    "addons.opera.com",
    "chromewebstore.google.com",
    "chrome.google.com",
}
FIREFOX_ADDONS_HOSTS = {"addons.mozilla.org"}
CHROME_WEB_STORE_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
SUPPORTED_BROWSERS = set(BROWSERS)


class SetupError(RuntimeError):
    pass


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if sys.platform != "win32":
        temporary.chmod(0o600)
    temporary.replace(path)


class OperationLock:
    """OS-held lock shared by CLI and UI; released automatically after a crash."""

    def __init__(self, base: Path | None = None) -> None:
        base = base or app_data_dir()
        base.parent.mkdir(parents=True, exist_ok=True)
        path = base.parent / f".{base.name}-setup.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        self.file = os.fdopen(fd, "r+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                if path.stat().st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise SetupError("Another mhm setup operation is running.") from exc

    def close(self) -> None:
        self.file.close()


class SetupTransaction:
    """Small recoverable journal for user-level setup file changes."""

    def __init__(self, operation: str, base: Path | None = None) -> None:
        self.base = (base or app_data_dir()).expanduser()
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.directory = self.base / "transactions" / f"{stamp}-{os.getpid()}"
        self.backups = self.directory / "backups"
        self.operation = operation
        self.state = "running"
        self.steps: list[dict[str, str]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.registry_snapshots: list[dict[str, Any]] = []
        self.directory.mkdir(parents=True, exist_ok=False)
        self._save()

    def _save(self) -> None:
        payload = {
            "schemaVersion": 1,
            "operation": self.operation,
            "state": self.state,
            "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "steps": self.steps,
            "snapshots": [
                {"path": item["path"], "existed": item["existed"]}
                for item in self.snapshots
            ],
            "registrySnapshots": self.registry_snapshots,
        }
        _write_json(self.directory / "journal.json", payload)

    def snapshot(self, path: Path) -> None:
        path = path.expanduser()
        if any(item["path"] == str(path) for item in self.snapshots):
            return
        item: dict[str, Any] = {"path": str(path), "existed": path.is_file()}
        if path.is_file():
            self.backups.mkdir(parents=True, exist_ok=True)
            backup = self.backups / f"{len(self.snapshots):04d}"
            shutil.copy2(path, backup)
            item["backup"] = str(backup)
        self.snapshots.append(item)
        self._save()

    def snapshot_registry(self, key_name: str) -> None:
        if sys.platform != "win32":
            return
        import winreg

        item: dict[str, Any] = {
            "key": key_name,
            "keyExisted": False,
            "valueExisted": False,
        }
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_QUERY_VALUE
            ) as key:
                item["keyExisted"] = True
                try:
                    item["value"], item["type"] = winreg.QueryValueEx(key, None)
                    item["valueExisted"] = True
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        self.registry_snapshots.append(item)
        self._save()

    def step(self, name: str, detail: str = "ok") -> None:
        self.steps.append({"name": name, "status": detail})
        self._save()

    def commit(self) -> None:
        self.state = "completed"
        self._save()

    def rollback(self, reason: str) -> None:
        for item in reversed(self.snapshots):
            path = Path(item["path"])
            try:
                if item["existed"]:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(item["backup"]), path)
                elif path.is_file():
                    path.unlink()
            except OSError:
                self.steps.append(
                    {
                        "name": "rollback",
                        "status": f"could not restore {_public_path(path)}",
                    }
                )
        if self.registry_snapshots:
            import winreg

            for item in reversed(self.registry_snapshots):
                try:
                    if item["valueExisted"]:
                        with winreg.CreateKeyEx(
                            winreg.HKEY_CURRENT_USER,
                            item["key"],
                            0,
                            winreg.KEY_SET_VALUE,
                        ) as key:
                            winreg.SetValueEx(key, None, 0, item["type"], item["value"])
                    elif item["keyExisted"]:
                        with winreg.OpenKey(
                            winreg.HKEY_CURRENT_USER,
                            item["key"],
                            0,
                            winreg.KEY_SET_VALUE,
                        ) as key:
                            winreg.DeleteValue(key, "")
                    else:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, item["key"])
                except FileNotFoundError:
                    pass
                except OSError:
                    self.steps.append(
                        {
                            "name": "rollback-registry",
                            "status": "could not restore Native Messaging registration",
                        }
                    )
        self.steps.append({"name": "rollback", "status": reason[:500]})
        self.state = "rolled-back"
        self._save()


def validate_extension_id(value: str) -> str:
    value = str(value or "").strip().lower()
    if value == FIREFOX_EXTENSION_ID:
        return value
    if not re.fullmatch(r"[a-p]{32}", value):
        raise SetupError("Extension ID must be a Chrome/Edge ID or Firefox addon ID.")
    return value


def extension_id_from_text(value: str | None) -> str | None:
    if not value:
        return None
    match = EXTENSION_ID_RE.search(str(value).lower())
    return match.group(1) if match else None


def normalize_store_url(value: str | None, browser: str = "chrome") -> str | None:
    if not value:
        return None
    browser = str(browser or "chrome").lower()
    if browser not in SUPPORTED_BROWSERS:
        raise SetupError(f"Unsupported browser: {browser}")
    try:
        parsed = urllib.parse.urlparse(str(value).strip())
    except ValueError as exc:
        raise SetupError("Invalid extension store URL.") from exc
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in BROWSERS[browser].store_hosts:
        raise SetupError(
            f"Extension URL must be an HTTPS URL from the {BROWSERS[browser].label} extension store."
        )
    if browser in {"chrome", "opera", "edge"} and not extension_id_from_text(value):
        raise SetupError(
            "Extension store URL does not contain a valid Chromium extension ID."
        )
    return parsed.geturl()


def normalize_web_store_url(value: str | None) -> str | None:
    return normalize_store_url(value, "chrome")


def chrome_extension_id_from_public_key(key_b64: str) -> str:
    try:
        key = base64.b64decode(key_b64, validate=True)
    except Exception as exc:  # noqa: BLE001 - normalize input errors for CLI
        raise SetupError("manifest.key is not valid Base64.") from exc
    digest = hashlib.sha256(key).digest()[:16]
    alphabet = "abcdefghijklmnop"
    return "".join(alphabet[b >> 4] + alphabet[b & 0x0F] for b in digest)


def extension_id_from_manifest(manifest_path: Path) -> str | None:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Cannot read extension manifest: {manifest_path}") from exc
    key = data.get("key")
    if not key:
        return None
    return chrome_extension_id_from_public_key(str(key))


def _marker_matches(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("product") == PRODUCT_MARKER_ID


def browser_user_data_roots() -> list[tuple[str, Path]]:
    home = Path.home()
    roots: list[tuple[str, Path]] = []
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roots.extend(
            [
                ("chrome", local / "Google" / "Chrome" / "User Data"),
                ("chrome-beta", local / "Google" / "Chrome Beta" / "User Data"),
                ("chromium", local / "Chromium" / "User Data"),
                ("edge", local / "Microsoft" / "Edge" / "User Data"),
                (
                    "opera",
                    Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
                    / "Opera Software"
                    / "Opera Stable",
                ),
                ("brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ]
        )
    elif sys.platform == "darwin":
        app = home / "Library" / "Application Support"
        roots.extend(
            [
                ("chrome", app / "Google" / "Chrome"),
                ("chrome-beta", app / "Google" / "Chrome Beta"),
                ("chromium", app / "Chromium"),
                ("opera", app / "com.operasoftware.Opera"),
                ("edge", app / "Microsoft Edge"),
                ("brave", app / "BraveSoftware" / "Brave-Browser"),
            ]
        )
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        roots.extend(
            [
                ("chrome", config / "google-chrome"),
                ("chrome-beta", config / "google-chrome-beta"),
                ("chromium", config / "chromium"),
                ("opera", config / "opera"),
                ("edge", config / "microsoft-edge"),
                ("brave", config / "BraveSoftware" / "Brave-Browser"),
            ]
        )
    return roots


def firefox_profile_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "win32":
        root = (
            Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
            / "Mozilla"
            / "Firefox"
            / "Profiles"
        )
    elif sys.platform == "darwin":
        root = home / "Library" / "Application Support" / "Firefox" / "Profiles"
    else:
        root = home / ".mozilla" / "firefox"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _iter_profile_dirs(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    if (root / "Preferences").is_file() or (root / "Extensions").is_dir():
        return [root]
    profiles: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if (
            child.name == "Default"
            or child.name.startswith("Profile ")
            or child.name in {"Guest Profile", "System Profile"}
        ):
            profiles.append(child)
    return profiles


def _detect_from_preferences(profile_dir: Path) -> set[str]:
    found: set[str] = set()
    prefs = profile_dir / "Preferences"
    if not prefs.is_file():
        return found
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return found
    settings = data.get("extensions", {}).get("settings", {})
    if not isinstance(settings, dict):
        return found
    for extension_id, entry in settings.items():
        if not re.fullmatch(r"[a-p]{32}", str(extension_id)) or not isinstance(
            entry, dict
        ):
            continue
        raw_path = entry.get("path")
        if not raw_path:
            continue
        candidate = Path(str(raw_path))
        candidates = [candidate]
        if not candidate.is_absolute():
            candidates.extend([profile_dir / candidate, profile_dir.parent / candidate])
        if any(
            _marker_matches(path / PRODUCT_MARKER)
            for path in candidates
            if path.exists()
        ):
            found.add(str(extension_id))
    return found


def detect_installed_extensions() -> list[dict[str, str]]:
    results: dict[tuple[str, str], dict[str, str]] = {}
    for browser, root in browser_user_data_roots():
        if not root.is_dir():
            continue
        for profile in _iter_profile_dirs(root):
            extensions_dir = profile / "Extensions"
            if extensions_dir.is_dir():
                for id_dir in extensions_dir.iterdir():
                    if not id_dir.is_dir() or not re.fullmatch(
                        r"[a-p]{32}", id_dir.name
                    ):
                        continue
                    for version_dir in id_dir.iterdir():
                        if version_dir.is_dir() and _marker_matches(
                            version_dir / PRODUCT_MARKER
                        ):
                            key = (browser, id_dir.name)
                            results[key] = {
                                "browser": browser,
                                "profile": profile.name,
                                "extensionId": id_dir.name,
                                "source": "installed",
                            }
                            break
            for extension_id in _detect_from_preferences(profile):
                key = (browser, extension_id)
                results[key] = {
                    "browser": browser,
                    "profile": profile.name,
                    "extensionId": extension_id,
                    "source": "unpacked",
                }
    for profile in firefox_profile_dirs():
        extensions = profile / "extensions.json"
        if not extensions.is_file():
            continue
        try:
            data = json.loads(extensions.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for addon in data.get("addons", []):
            if (
                isinstance(addon, dict)
                and addon.get("id") == FIREFOX_EXTENSION_ID
                and addon.get("active", True)
            ):
                results[("firefox", FIREFOX_EXTENSION_ID)] = {
                    "browser": "firefox",
                    "profile": profile.name,
                    "extensionId": FIREFOX_EXTENSION_ID,
                    "source": "installed",
                }
                break
    return sorted(
        results.values(),
        key=lambda item: (item["browser"], item["profile"], item["extensionId"]),
    )


def extension_is_installed(extension_id: str, browser: str | None = None) -> bool:
    extension_id = validate_extension_id(extension_id)
    browser = str(browser or "").lower()
    return any(
        item["extensionId"] == extension_id
        and (
            not browser
            or item["browser"] == browser
            or (
                browser == "chrome"
                and item["browser"] in {"chrome", "chrome-beta", "chromium"}
            )
        )
        for item in detect_installed_extensions()
    )


def _write_external_extension_file(path: Path, extension_id: str) -> Path:
    validate_extension_id(extension_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"external_update_url": CHROME_WEB_STORE_UPDATE_URL}
    path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    path.chmod(0o644)
    return path


def _windows_is_64bit() -> bool:
    if sys.platform != "win32":
        return False
    arch = (
        os.environ.get("PROCESSOR_ARCHITEW6432")
        or os.environ.get("PROCESSOR_ARCHITECTURE")
        or ""
    ).lower()
    return "64" in arch


def provision_external_extension(extension_id: str) -> list[str]:
    """Register a Chrome Web Store extension after its ID is known at install time.

    On Windows this must run elevated because Chrome's documented external-extension
    registry location is HKLM. macOS supports a per-user External Extensions file.
    Linux uses Chrome's system external-extension directory and therefore requires
    root (or the caller to elevate only this operation).
    """
    extension_id = validate_extension_id(extension_id)
    installed: list[str] = []
    if sys.platform == "win32":
        if not _is_windows_admin():
            raise SetupError(
                "Windows external extension registration requires elevation."
            )
        import winreg  # type: ignore

        access = winreg.KEY_SET_VALUE
        if _windows_is_64bit() and hasattr(winreg, "KEY_WOW64_32KEY"):
            access |= winreg.KEY_WOW64_32KEY
        key_name = rf"Software\Google\Chrome\Extensions\{extension_id}"
        key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key_name, 0, access)
        try:
            winreg.SetValueEx(
                key, "update_url", 0, winreg.REG_SZ, CHROME_WEB_STORE_UPDATE_URL
            )
        finally:
            winreg.CloseKey(key)
        installed.append(rf"HKLM\{key_name}")
        return installed

    if sys.platform == "darwin":
        target = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "External Extensions"
            / f"{extension_id}.json"
        )
        installed.append(str(_write_external_extension_file(target, extension_id)))
        return installed

    if sys.platform.startswith("linux"):
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            raise SetupError(
                "Linux external extension registration requires root privileges."
            )
        target = Path("/opt/google/chrome/extensions") / f"{extension_id}.json"
        installed.append(str(_write_external_extension_file(target, extension_id)))
        return installed

    raise SetupError(
        "External Chrome Web Store installation is not supported on this platform."
    )


def _provision_linux_external_with_sudo(
    extension_id: str, *, interactive: bool
) -> list[str]:
    if not sys.platform.startswith("linux"):
        return provision_external_extension(extension_id)
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        return provision_external_extension(extension_id)
    sudo = shutil.which("sudo")
    if not sudo or not interactive:
        raise SetupError(
            "Installing the Chrome Web Store external-extension entry on Linux requires root. "
            "Run mhm-setup interactively with sudo available, provide --extension-url, "
            "or install the extension manually."
        )
    with tempfile.TemporaryDirectory(prefix="mhm-extension-") as tmp:
        source = Path(tmp) / f"{extension_id}.json"
        source.write_text(
            json.dumps({"external_update_url": CHROME_WEB_STORE_UPDATE_URL}) + "\n",
            encoding="utf-8",
        )
        target = f"/opt/google/chrome/extensions/{extension_id}.json"
        print(
            "Administrator authorization is required only to register the Chrome Web Store extension metadata."
        )
        result = subprocess.run(
            [sudo, "install", "-D", "-m", "0644", str(source), target], check=False
        )
        if result.returncode != 0:
            raise SetupError(
                f"Unable to register the external extension entry (sudo exit {result.returncode})."
            )
        return [target]


def resolve_extension_id(
    explicit_id: str | None,
    extension_url: str | None,
    extension_manifest: Path | None,
    *,
    interactive: bool,
    browser: str = "chrome",
) -> tuple[str, str | None]:
    browser = str(browser or "chrome").lower()
    if browser not in SUPPORTED_BROWSERS:
        raise SetupError(f"Unsupported browser: {browser}")
    normalized_url = (
        normalize_store_url(extension_url, browser) if extension_url else None
    )

    if browser == "firefox":
        if explicit_id and validate_extension_id(explicit_id) != FIREFOX_EXTENSION_ID:
            raise SetupError(f"Firefox extension ID must be {FIREFOX_EXTENSION_ID}.")
        return FIREFOX_EXTENSION_ID, normalized_url

    url_id = extension_id_from_text(normalized_url) if normalized_url else None
    if explicit_id:
        resolved_id = validate_extension_id(explicit_id)
        if resolved_id == FIREFOX_EXTENSION_ID:
            raise SetupError("Firefox add-on ID cannot be used for Chrome or Edge.")
        if url_id and validate_extension_id(url_id) != resolved_id:
            raise SetupError("Extension ID does not match the extension store URL.")
        return resolved_id, normalized_url
    if url_id:
        return validate_extension_id(url_id), normalized_url
    if extension_manifest:
        from_key = extension_id_from_manifest(extension_manifest)
        if from_key:
            return validate_extension_id(from_key), normalized_url

    detected = [
        item
        for item in detect_installed_extensions()
        if item["browser"] == browser
        or (
            browser == "chrome"
            and item["browser"] in {"chrome", "chrome-beta", "chromium"}
        )
    ]
    unique_ids = sorted(
        {
            item["extensionId"]
            for item in detected
            if re.fullmatch(r"[a-p]{32}", item["extensionId"])
        }
    )
    if len(unique_ids) == 1:
        return unique_ids[0], normalized_url
    if not interactive:
        page = {
            "chrome": "chrome://extensions",
            "edge": "edge://extensions",
            "opera": "opera://extensions",
        }[browser]
        reason = (
            "Multiple mhm extension IDs were detected"
            if unique_ids
            else "No mhm extension was detected"
        )
        raise SetupError(
            f"{reason} in {browser}. Install/load the mhm extension first, open {page}, "
            "and copy its ID (32 letters a-p). Then run:\n"
            f"make install ARGS='--browser {browser} --extension-id YOUR_EXTENSION_ID --extension-install skip'\n"
            f"Standalone installer: install --browser {browser} --extension-id YOUR_EXTENSION_ID --extension-install skip"
        )
    while True:
        raw = input("Extension ID / store URL: ").strip()
        if not raw:
            continue
        candidate = extension_id_from_text(raw)
        if not candidate:
            print("No valid Chromium extension ID was found in that value.")
            continue
        if raw.startswith(("http://", "https://")):
            try:
                normalized_url = normalize_store_url(raw, browser)
            except SetupError as exc:
                print(str(exc))
                continue
        return validate_extension_id(candidate), normalized_url


def app_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "mhm"
        )
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "mhm"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "mhm"


def _system_tls_context():
    if sys.platform.startswith("linux"):
        # the deb launcher uses system python and distro ca paths
        return ssl.create_default_context()
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _http_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"mhm-setup/{PRODUCT_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("MHM_GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(
            request, timeout=30, context=_system_tls_context()
        ) as response:
            return json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SetupError(
            f"Unable to query official mihomo release metadata: {exc}"
        ) from exc


def _platform_asset_parts() -> tuple[str, str, str]:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "amd64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise SetupError(
            f"Unsupported architecture for automatic mihomo installation: {platform.machine()}"
        )

    if sys.platform == "win32":
        return "windows", arch, "zip"
    if sys.platform == "darwin":
        return "darwin", arch, "gz"
    if sys.platform.startswith("linux"):
        return "linux", arch, "gz"
    raise SetupError(
        f"Unsupported platform for automatic mihomo installation: {sys.platform}"
    )


def resolve_mihomo_asset(version: str = "latest") -> dict[str, str]:
    if version == "latest":
        release = _http_json(f"{GITHUB_API}/releases/latest")
    else:
        normalized = version.removeprefix("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.-]+)?", normalized):
            raise SetupError("Invalid mihomo version.")
        release = _http_json(f"{GITHUB_API}/releases/tags/v{normalized}")

    if release.get("prerelease"):
        raise SetupError(
            "Refusing to install a prerelease as the stable mihomo package."
        )
    tag = str(release.get("tag_name") or "")
    normalized = tag.removeprefix("v")
    if not normalized:
        raise SetupError("Official release metadata did not contain a version tag.")

    os_name, arch, archive_type = _platform_asset_parts()
    cpu = "amd64-compatible" if arch == "amd64" else arch
    asset_name = f"mihomo-{os_name}-{cpu}-v{normalized}.{archive_type}"
    assets = release.get("assets") or []
    asset = next((item for item in assets if item.get("name") == asset_name), None)
    if not asset:
        raise SetupError(f"Official mihomo release does not contain {asset_name}.")
    digest = str(asset.get("digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise SetupError(
            "The official release asset does not expose a SHA-256 digest through the GitHub API; "
            "refusing an unverified download."
        )
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith("https://github.com/MetaCubeX/mihomo/releases/download/"):
        raise SetupError("Unexpected mihomo release download URL.")
    return {
        "version": normalized,
        "name": asset_name,
        "url": url,
        "sha256": digest.split(":", 1)[1].lower(),
        "archiveType": archive_type,
    }


def _download_verified(url: str, destination: Path, expected_sha256: str) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"mhm-setup/{PRODUCT_VERSION}"}
    )
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(
            request, timeout=60, context=_system_tls_context()
        ) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
    except (OSError, urllib.error.URLError) as exc:
        destination.unlink(missing_ok=True)
        raise SetupError(f"Unable to download mihomo: {exc}") from exc
    actual = digest.hexdigest()
    if actual != expected_sha256.lower():
        destination.unlink(missing_ok=True)
        raise SetupError(
            f"mihomo SHA-256 mismatch: expected {expected_sha256}, got {actual}"
        )


def _extract_mihomo(archive: Path, archive_type: str, destination: Path) -> None:
    if archive_type == "zip":
        with zipfile.ZipFile(archive) as zf:
            entries = [
                n
                for n in zf.namelist()
                if Path(n).name.lower().startswith("mihomo")
                and n.lower().endswith(".exe")
            ]
            if len(entries) != 1:
                raise SetupError(
                    "Expected one mihomo executable in the official archive."
                )
            destination.write_bytes(zf.read(entries[0]))
    elif archive_type == "gz":
        import gzip

        with gzip.open(archive, "rb") as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
    else:
        raise SetupError("Unsupported mihomo archive format.")
    if sys.platform != "win32":
        destination.chmod(0o755)


def probe_mihomo(path: Path) -> dict[str, str] | None:
    candidate = path.expanduser()
    if not candidate.is_file():
        return None
    try:
        output = subprocess.check_output(
            [str(candidate), "-v"], stderr=subprocess.STDOUT, text=True, timeout=8
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"Mihomo(?:\s+(?:version|Meta))?\s+v?([0-9][^\s]+)", output, re.I)
    if not match:
        return None
    version = match.group(1).removeprefix("v")
    return {"path": str(candidate.resolve()), "version": version, "source": "detected"}


def detect_mihomo() -> list[dict[str, str]]:
    """Return verified local mihomo installations, ordered by user relevance."""
    candidates: list[Path] = []
    base = app_data_dir()
    for metadata in (
        base / "settings.json",
        base / "mihomo-install.json",
        base / "setup-state.json",
    ):
        try:
            data = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw = data.get("mihomoPath") or data.get("path")
        if raw:
            candidates.append(Path(str(raw)))

    found_in_path = shutil.which("mihomo")
    if found_in_path:
        candidates.append(Path(found_in_path))

    if sys.platform == "win32":
        candidates += [
            Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files")
            / "mihomo"
            / "mihomo.exe",
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            / "mihomo"
            / "mihomo.exe",
        ]
    elif sys.platform == "darwin":
        candidates += [
            Path("/opt/homebrew/bin/mihomo"),
            Path("/usr/local/bin/mihomo"),
            Path("/usr/bin/mihomo"),
        ]
    else:
        candidates += [
            Path("/usr/local/bin/mihomo"),
            Path("/usr/bin/mihomo"),
            Path.home() / ".local" / "bin" / "mihomo",
        ]

    # Include mhm-managed versioned installs, newest directory names first.
    managed = base / "mihomo"
    if managed.is_dir():
        binary_name = "mihomo.exe" if sys.platform == "win32" else "mihomo"
        candidates += [
            d / binary_name
            for d in sorted(managed.iterdir(), reverse=True)
            if d.is_dir()
        ]

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = (
                str(candidate.expanduser().resolve()).lower()
                if sys.platform == "win32"
                else str(candidate.expanduser().resolve())
            )
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        info = probe_mihomo(candidate)
        if info:
            results.append(info)
    return results


def embedded_native_host_dir() -> Path | None:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = root / "payload" / "native_host"
    return candidate if candidate.is_dir() else None


def embedded_native_host_archive() -> Path | None:
    """Return the native-host payload shared by CLI and terminal setup.

    Keeping the onedir bundle inside a ZIP prevents the outer PyInstaller build
    from rewriting loader paths of nested native modules. This is especially
    important for Homebrew Python on macOS, where ``_sqlite3`` uses an rpath to
    the bundled ``libsqlite3.dylib``.
    """
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = root / "payload" / "native_host.zip"
    if not hasattr(sys, "_MEIPASS"):
        candidate = (
            Path(__file__).resolve().parents[1] / ".build" / "setup" / "native_host.zip"
        )
    return candidate if candidate.is_file() else None


def _user_native_host_pointer(base: Path | None = None) -> Path:
    base = base or app_data_dir()
    return base / "native_host" / "current.json"


def _read_user_native_host_pointer(base: Path | None = None) -> Path | None:
    pointer = _user_native_host_pointer(base)
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        candidate = Path(str(data.get("path") or "")).expanduser()
    except (OSError, json.JSONDecodeError):
        return None
    return candidate if candidate.is_file() else None


def install_user_native_host(
    source: Path | None = None, base: Path | None = None
) -> Path:
    """Install a bundled native-host onedir as an immutable per-user release.

    Versioned release directories avoid replacing files that can be locked by a
    running Chrome Native Messaging process on Windows. The current pointer is
    switched only after the new payload has been copied and validated.
    """
    source = source or embedded_native_host_archive() or embedded_native_host_dir()
    if source is None or not (
        source.is_dir() or source.is_file() and source.suffix.lower() == ".zip"
    ):
        raise SetupError("The installer does not contain a native-host payload.")
    base = base or app_data_dir()
    host_root = base / "native_host"
    releases = host_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    release_name = f"{PRODUCT_VERSION}-{stamp}-{os.getpid()}"
    staging = releases / f".{release_name}.new"
    target_dir = releases / release_name
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    if source.is_dir():
        shutil.copytree(source, staging)
    else:
        staging.mkdir(parents=True)
        try:
            with zipfile.ZipFile(source) as archive:
                # Python 3.12+ rejects common traversal tricks via zipfile.Path
                # only at use time, so validate every member explicitly here.
                root = staging.resolve()
                for member in archive.infolist():
                    destination = (staging / member.filename).resolve()
                    if destination != root and root not in destination.parents:
                        raise SetupError(
                            "Bundled native-host archive contains an unsafe path."
                        )
                archive.extractall(staging)
        except (OSError, zipfile.BadZipFile) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise SetupError("Bundled native-host archive is damaged.") from exc
    executable_name = "native_host.exe" if sys.platform == "win32" else "native_host"
    executable = staging / executable_name
    if not executable.is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise SetupError("Bundled native-host payload is incomplete.")
    if sys.platform != "win32":
        executable.chmod(0o755)
        for item in staging.rglob("*"):
            try:
                item.chmod(item.stat().st_mode & ~0o022)
            except OSError:
                pass
    staging.replace(target_dir)
    installed = target_dir / executable_name
    pointer = _user_native_host_pointer(base)
    _write_json(pointer, {"path": str(installed.resolve()), "version": PRODUCT_VERSION})

    # Best-effort cleanup. Keep the current release and one previous release;
    # locked/running releases are intentionally left in place until a later repair.
    release_dirs = [
        d for d in releases.iterdir() if d.is_dir() and not d.name.startswith(".")
    ]
    release_dirs.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
    referenced: set[Path] = set()
    for browser in SUPPORTED_BROWSERS:
        for manifest in _native_messaging_targets(browser, base):
            try:
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
                raw_path = metadata.get("path")
                if raw_path:
                    referenced.add(Path(raw_path).resolve().parent)
            except (OSError, ValueError, TypeError):
                continue
    for old in release_dirs[2:]:
        if old == target_dir or old.resolve() in referenced:
            continue
        try:
            shutil.rmtree(old)
        except OSError:
            pass
    return installed.resolve()


def install_mihomo(version: str = "latest", base: Path | None = None) -> dict[str, str]:
    base = base or app_data_dir()
    metadata_path = base / "mihomo-install.json"
    asset = resolve_mihomo_asset(version)
    binary_name = "mihomo.exe" if sys.platform == "win32" else "mihomo"
    target = base / "mihomo" / asset["version"] / binary_name

    if target.is_file():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if (
            existing.get("version") == asset["version"]
            and existing.get("sha256") == asset["sha256"]
        ):
            print(f"mihomo {asset['version']} is already installed: {target}")
            return {**asset, "path": str(target)}

    print(f"Downloading mihomo {asset['version']} from MetaCubeX GitHub Releases...")
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mhm-mihomo-") as tmp_dir:
        archive = Path(tmp_dir) / asset["name"]
        _download_verified(asset["url"], archive, asset["sha256"])
        temp_binary = Path(tmp_dir) / binary_name
        _extract_mihomo(archive, asset["archiveType"], temp_binary)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(temp_binary, target)
        if sys.platform != "win32":
            target.chmod(0o755)

    try:
        output = subprocess.check_output(
            [str(target), "-v"], stderr=subprocess.STDOUT, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        target.unlink(missing_ok=True)
        raise SetupError(f"Downloaded mihomo failed its version check: {exc}") from exc
    if asset["version"] not in output:
        target.unlink(missing_ok=True)
        raise SetupError(
            "Downloaded mihomo version output does not match the verified release metadata."
        )

    metadata = {
        "version": asset["version"],
        "path": str(target),
        "source": asset["url"],
        "sha256": asset["sha256"],
        "installedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    _write_json(metadata_path, metadata)
    print(f"Installed mihomo separately: {target}")
    return {**asset, "path": str(target)}


def merge_settings(mihomo_path: Path, base: Path | None = None) -> Path:
    base = base or app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / "settings.json"
    settings: dict[str, Any] = {"mihomoPath": "", "localPort": 10808, "autoPort": True}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            settings.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass
    settings["mihomoPath"] = str(mihomo_path.resolve())
    _write_json(path, settings)
    return path


def _write_manifest(path: Path, extension_id: str, native_host: Path) -> None:
    extension_id = validate_extension_id(extension_id)
    allowed = (
        {"allowed_extensions": [extension_id]}
        if extension_id == FIREFOX_EXTENSION_ID
        else {"allowed_origins": [f"chrome-extension://{extension_id}/"]}
    )
    if extension_id != FIREFOX_EXTENSION_ID:
        origins = set(allowed["allowed_origins"])
        candidates = [path, *path.parent.glob(f"{HOST_NAME}-*.json")]
        # Chrome and Opera share Chromium's host lookup. Keep both installations authorized.
        for candidate in candidates:
            try:
                previous = json.loads(candidate.read_text(encoding="utf-8"))
                if previous.get("name") == HOST_NAME:
                    origins.update(
                        origin
                        for origin in previous.get("allowed_origins", [])
                        if re.fullmatch(r"chrome-extension://[a-p]{32}/", origin)
                    )
            except (OSError, ValueError):
                pass
        allowed["allowed_origins"] = sorted(origins)
    _write_json(
        path,
        {
            "name": HOST_NAME,
            "description": "mhm native host",
            "path": str(native_host.resolve()),
            "type": "stdio",
            **allowed,
        },
    )


def native_messaging_registry_keys(browser: str) -> list[str]:
    return [
        owner + rf"\NativeMessagingHosts\{HOST_NAME}"
        for owner in BROWSERS[browser].registry_owners
    ]


def configure_native_messaging(
    extension_id: str,
    native_host: Path,
    base: Path | None = None,
    browser: str | None = None,
) -> list[Path]:
    extension_id = validate_extension_id(extension_id)
    browser = (
        browser or ("firefox" if extension_id == FIREFOX_EXTENSION_ID else "chrome")
    ).lower()
    if browser not in SUPPORTED_BROWSERS:
        raise SetupError(f"Unsupported browser: {browser}")
    native_host = native_host.resolve()
    if not native_host.is_file():
        raise SetupError(f"Native host executable was not found: {native_host}")
    base = base or app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    if (browser == "firefox") != (extension_id == FIREFOX_EXTENSION_ID):
        raise SetupError("Extension ID does not match the selected browser.")
    installed = _native_messaging_targets(browser, base)
    for path in installed:
        _write_manifest(path, extension_id, native_host)
    if sys.platform == "win32":
        import winreg

        for key_name in native_messaging_registry_keys(browser):
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, key_name, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(installed[0]))
    return installed


def _native_messaging_targets(browser: str, base: Path | None = None) -> list[Path]:
    """Return manifest paths owned by mhm without creating their directories."""
    base = base or app_data_dir()
    targets = [base / f"{HOST_NAME}-{browser}.json"]
    home = Path.home()
    directories = []
    if sys.platform == "darwin":
        directories = [
            home / "Library/Application Support" / path
            for path in BROWSERS[browser].mac_native
        ]
    elif sys.platform.startswith("linux"):
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        directories = [
            (
                home / path.removeprefix("@home/")
                if path.startswith("@home/")
                else config / path
            )
            for path in BROWSERS[browser].linux_native
        ]
    targets.extend(directory / f"{HOST_NAME}.json" for directory in directories)
    return targets


def _configured_extension_ids(base: Path | None = None) -> set[str]:
    ids: set[str] = set()
    base = base or app_data_dir()
    candidates = [base / "setup-state.json"]
    candidates.extend(
        base / f"{HOST_NAME}-{browser}.json" for browser in SUPPORTED_BROWSERS
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        explicit = str(data.get("extensionId") or "")
        if explicit:
            try:
                ids.add(validate_extension_id(explicit))
            except SetupError:
                pass
        for origin in data.get("allowed_origins", []):
            found = extension_id_from_text(str(origin))
            if found:
                ids.add(found)
    return ids


def remove_native_messaging(base: Path | None = None) -> list[str]:
    """Remove all per-user Native Messaging registrations created by mhm."""
    removed: list[str] = []
    for browser in sorted(SUPPORTED_BROWSERS):
        for path in _native_messaging_targets(browser, base):
            if path.is_file():
                path.unlink(missing_ok=True)
                removed.append(str(path))
    if sys.platform == "win32":
        import winreg  # type: ignore

        registry_keys = [
            rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
            rf"Software\Chromium\NativeMessagingHosts\{HOST_NAME}",
            rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
            rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}",
        ]
        for key_name in registry_keys:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name)
                removed.append(rf"HKCU\{key_name}")
            except FileNotFoundError:
                pass
            except OSError:
                # A key with unexpected children is not ours to remove recursively.
                pass
    return removed


def remove_external_extension_registration(extension_id: str) -> list[str]:
    """Remove only Store registration metadata; browsers own extension removal."""
    extension_id = validate_extension_id(extension_id)
    if extension_id == FIREFOX_EXTENSION_ID:
        return []
    removed: list[str] = []
    if sys.platform == "darwin":
        path = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "External Extensions"
            / f"{extension_id}.json"
        )
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(str(path))
    elif sys.platform == "win32" and _is_windows_admin():
        import winreg  # type: ignore

        key_name = rf"Software\Google\Chrome\Extensions\{extension_id}"
        access = winreg.KEY_WRITE
        if _windows_is_64bit() and hasattr(winreg, "KEY_WOW64_32KEY"):
            access |= winreg.KEY_WOW64_32KEY
        try:
            winreg.DeleteKeyEx(
                winreg.HKEY_LOCAL_MACHINE, key_name, access & ~winreg.KEY_WRITE, 0
            )
            removed.append(rf"HKLM\{key_name}")
        except (AttributeError, FileNotFoundError):
            try:
                winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, key_name)
                removed.append(rf"HKLM\{key_name}")
            except FileNotFoundError:
                pass
    elif sys.platform.startswith("linux") and (
        not hasattr(os, "geteuid") or os.geteuid() == 0
    ):
        path = Path("/opt/google/chrome/extensions") / f"{extension_id}.json"
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed


def _remove_saved_secrets(base: Path) -> None:
    def stored_ids(filename: str, prefix: str = "") -> list[str]:
        try:
            items = json.loads((base / filename).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [
            prefix + str(item.get("id"))
            for item in items
            if isinstance(item, dict) and item.get("id")
        ]

    secret_ids = stored_ids("profiles.json") + stored_ids(
        "subscriptions.json", "subscription:"
    )
    if sys.platform == "darwin":
        if (base / "profiles.secrets.encrypted").is_file():
            secret_ids.append(
                "mhm-storage-key-"
                + hashlib.sha256(str(base.resolve()).encode()).hexdigest()[:16]
            )
        security = shutil.which("security") or "/usr/bin/security"
        for secret_id in secret_ids:
            subprocess.run(
                [security, "delete-generic-password", "-a", secret_id, "-s", HOST_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    elif sys.platform.startswith("linux"):
        if (base / "profiles.secrets.encrypted").is_file():
            secret_ids.append(
                "mhm-storage-key-"
                + hashlib.sha256(str(base.resolve()).encode()).hexdigest()[:16]
            )
        secret_tool = shutil.which("secret-tool")
        if secret_tool:
            for secret_id in secret_ids:
                subprocess.run(
                    [
                        secret_tool,
                        "clear",
                        "application",
                        HOST_NAME,
                        "profile",
                        secret_id,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )


def uninstall_user_components(
    *, remove_user_data: bool = False, base: Path | None = None
) -> dict[str, Any]:
    """Undo the per-user work performed by Setup.

    The browser extension itself is intentionally left to the browser's own
    extension manager. By default profiles, subscriptions and traffic history
    are retained so reinstalling mhm can restore them.
    """
    base = (base or app_data_dir()).expanduser()
    extension_ids = _configured_extension_ids(base)
    removed = remove_native_messaging(base)
    for extension_id in extension_ids:
        removed.extend(remove_external_extension_registration(extension_id))

    if remove_user_data:
        _remove_saved_secrets(base)
        if base.is_dir():
            shutil.rmtree(base)
            removed.append(str(base))
    else:
        for relative in ("native_host", "mihomo"):
            target = base / relative
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(str(target))
        for relative in ("mihomo-install.json", "setup-state.json"):
            target = base / relative
            if target.is_file():
                target.unlink(missing_ok=True)
                removed.append(str(target))
    return {
        "removed": removed,
        "keptUserData": not remove_user_data,
        "extensionIds": sorted(extension_ids),
    }


def native_host_ping(native_host: Path) -> dict[str, Any]:
    request = json.dumps(
        {"protocolVersion": 1, "requestId": "installer-health", "command": "ping"},
        separators=(",", ":"),
    ).encode("utf-8")
    packet = struct.pack("=I", len(request)) + request
    try:
        proc = subprocess.Popen(
            [str(native_host)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = proc.communicate(packet, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SetupError(f"Native host health check failed to start: {exc}") from exc
    if len(stdout) < 4:
        detail = stderr.decode("utf-8", "replace").strip()
        raise SetupError(
            f"Native host health check returned no message. {detail}".strip()
        )
    size = struct.unpack("=I", stdout[:4])[0]
    body = stdout[4 : 4 + size]
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError("Native host health check returned invalid data.") from exc
    if not response.get("ok"):
        raise SetupError(
            f"Native host health check failed: {response.get('error') or 'unknown error'}"
        )
    if response.get("protocolVersion") != 1:
        raise SetupError(
            f"Native host health check returned unsupported protocol version: {response.get('protocolVersion')}"
        )
    return response


def _read_windows_native_host_from_registry() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore

        access = winreg.KEY_READ
        if hasattr(winreg, "KEY_WOW64_64KEY"):
            access |= winreg.KEY_WOW64_64KEY
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"Software\mhm\NativeHost", 0, access
        )
        try:
            install_dir, _ = winreg.QueryValueEx(key, "InstallDir")
        finally:
            winreg.CloseKey(key)
        candidate = Path(str(install_dir)) / "native_host" / "native_host.exe"
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def locate_native_host(explicit: Path | None = None) -> Path:
    if explicit:
        candidate = explicit.expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise SetupError(f"Native host executable was not found: {candidate}")
    if embedded_native_host_archive() or embedded_native_host_dir():
        return install_user_native_host()
    user_candidate = _read_user_native_host_pointer()
    if user_candidate:
        return user_candidate
    # Compatibility with early mhm development installs that used a fixed path.
    legacy_user_candidate = (
        app_data_dir()
        / "native_host"
        / ("native_host.exe" if sys.platform == "win32" else "native_host")
    )
    if legacy_user_candidate.is_file():
        return legacy_user_candidate
    registry = _read_windows_native_host_from_registry()
    if registry:
        return registry
    if sys.platform == "darwin":
        candidate = Path("/Library/Application Support/mhm/native_host/native_host")
        if candidate.is_file():
            return candidate
    elif sys.platform.startswith("linux"):
        candidate = Path("/opt/mhm/native_host/native_host")
        if candidate.is_file():
            return candidate
    raise SetupError(
        "Native host is unavailable. Run make build first or pass --native-host."
    )


def _is_windows_admin() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _run_windows_elevated_self(arguments: list[str]) -> int:
    """Run only the machine-scoped setup phase elevated and wait for it.

    Keeping the main setup process unelevated preserves the interactive user's
    HKCU, profile, DPAPI identity and browser session even when UAC uses
    over-the-shoulder administrator credentials.
    """
    if sys.platform != "win32":
        raise SetupError("Elevation helper is Windows-only.")
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    if getattr(sys, "frozen", False):
        executable = sys.executable
        child_args = arguments
    else:
        executable = sys.executable
        child_args = [str(Path(__file__).resolve()), *arguments]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = subprocess.list2cmdline(child_args)
    info.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise SetupError(
            f"Administrator elevation failed (Win32 error {ctypes.get_last_error()})."
        )
    if not info.hProcess:
        raise SetupError("Administrator elevation returned no process handle.")
    try:
        wait = ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        if wait == 0xFFFFFFFF:
            raise SetupError(
                f"Waiting for elevated setup failed (Win32 error {ctypes.get_last_error()})."
            )
        exit_code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(
            info.hProcess, ctypes.byref(exit_code)
        ):
            raise SetupError(
                f"Unable to read elevated setup exit code (Win32 error {ctypes.get_last_error()})."
            )
        return int(exit_code.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(info.hProcess)


def browser_executables() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    seen: set[str] = set()
    candidates: list[tuple[str, Path]] = []
    if sys.platform == "win32":
        roots = [
            Path(os.environ.get("PROGRAMFILES") or r"C:\Program Files"),
            Path(os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"),
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"),
        ]
        patterns = [
            ("chrome", Path("Google/Chrome/Application/chrome.exe")),
            ("opera", Path("Programs/Opera/launcher.exe")),
            ("edge", Path("Microsoft/Edge/Application/msedge.exe")),
            ("firefox", Path("Mozilla Firefox/firefox.exe")),
        ]
        for root in roots:
            for name, rel in patterns:
                candidates.append((name, root / rel))
    elif sys.platform == "darwin":
        candidates = [
            (
                "chrome",
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ),
            ("opera", Path("/Applications/Opera.app/Contents/MacOS/Opera")),
            (
                "edge",
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ),
            ("firefox", Path("/Applications/Firefox.app/Contents/MacOS/firefox")),
        ]
    else:
        for name, commands in {
            "chrome": (
                "google-chrome",
                "google-chrome-stable",
                "chromium",
                "chromium-browser",
            ),
            "opera": ("opera",),
            "edge": ("microsoft-edge", "microsoft-edge-stable"),
            "firefox": ("firefox",),
        }.items():
            for command in commands:
                resolved = shutil.which(command)
                if resolved:
                    candidates.append((name, Path(resolved)))
                    break
    for name, candidate in candidates:
        key = str(candidate).lower()
        if candidate.is_file() and key not in seen:
            seen.add(key)
            found.append((name, candidate))
    return found


def browser_executable(browser: str) -> Path | None:
    browser = str(browser or "").lower()
    return next((path for name, path in browser_executables() if name == browser), None)


def open_browser_url(url: str, browser: str | None = None) -> bool:
    browser = str(browser or "").lower()
    executable = browser_executable(browser) if browser else None
    if executable:
        try:
            subprocess.Popen([str(executable), "--new-tab", url])
            return True
        except OSError:
            pass
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def save_setup_state(
    extension_id: str,
    native_host: Path,
    mihomo: dict[str, str],
    base: Path | None = None,
    browser: str | None = None,
) -> Path:
    base = base or app_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    state = {
        "extensionId": extension_id,
        "browser": browser
        or ("firefox" if extension_id == FIREFOX_EXTENSION_ID else "chrome"),
        "nativeHostPath": str(native_host.resolve()),
        "mihomoVersion": mihomo.get("version"),
        "mihomoPath": mihomo.get("path"),
        "configuredAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    path = base / "setup-state.json"
    _write_json(path, state)
    return path


def _public_path(path: Path) -> str:
    try:
        relative = path.expanduser().resolve().relative_to(Path.home().resolve())
        return str(Path("~") / relative)
    except (OSError, ValueError):
        return str(path)


def diagnose_system(base: Path | None = None) -> dict[str, Any]:
    """Return a secret-free support report suitable for JSON export."""
    base = (base or app_data_dir()).expanduser()
    checks: list[dict[str, Any]] = []

    def add(check_id: str, status: str, summary: str, path: Path | None = None) -> None:
        item: dict[str, Any] = {"id": check_id, "status": status, "summary": summary}
        if path is not None:
            item["path"] = _public_path(path)
        checks.append(item)

    add(
        "app-data",
        "ok" if base.is_dir() else "warning",
        "available" if base.is_dir() else "not created yet",
        base,
    )
    state_path = base / "setup-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("not an object")
        add("setup-state", "ok", "valid", state_path)
    except FileNotFoundError:
        state = {}
        add("setup-state", "warning", "not installed", state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        state = {}
        add("setup-state", "error", "invalid JSON", state_path)

    host = _read_user_native_host_pointer(base)
    configured_host = Path(str(state.get("nativeHostPath") or ""))
    if host is None and configured_host.is_file():
        host = configured_host
    if host:
        try:
            response = native_host_ping(host)
            add(
                "native-host",
                "ok",
                f"healthy, protocol {response.get('protocolVersion')}",
                host,
            )
        except SetupError as exc:
            add("native-host", "error", str(exc)[:500], host)
    else:
        configured = Path(str(state.get("nativeHostPath") or ""))
        add(
            "native-host",
            "error" if state else "warning",
            "executable not found",
            configured if str(configured) != "." else None,
        )

    mihomo = Path(str(state.get("mihomoPath") or ""))
    add(
        "mihomo",
        "ok" if mihomo.is_file() else ("error" if state else "warning"),
        "available" if mihomo.is_file() else "executable not found",
        mihomo if str(mihomo) != "." else None,
    )
    channels = load_browser_channels()
    for browser in sorted(SUPPORTED_BROWSERS):
        details = channels.get(browser, {})
        if not isinstance(details, dict):
            details = {}
        configured = bool(str(details.get("store_url") or "").strip())
        add(
            f"store-{browser}",
            "ok" if configured else "warning",
            "configured" if configured else "release URL is not configured",
        )
    return {
        "schemaVersion": 1,
        "productVersion": PRODUCT_VERSION,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
        },
        "checks": checks,
    }


def repair_installation(base: Path | None = None) -> dict[str, Any]:
    base = (base or app_data_dir()).expanduser()
    state_path = base / "setup-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError("Repair requires a valid previous setup-state.json.") from exc
    extension_id = validate_extension_id(str(state.get("extensionId") or ""))
    browser = str(
        state.get("browser")
        or ("firefox" if extension_id == FIREFOX_EXTENSION_ID else "chrome")
    )
    transaction = SetupTransaction("repair", base)
    for path in [
        _user_native_host_pointer(base),
        *_native_messaging_targets(browser, base),
    ]:
        transaction.snapshot(path)
    for key_name in native_messaging_registry_keys(browser):
        transaction.snapshot_registry(key_name)
    try:
        host = _read_user_native_host_pointer(base) or Path(
            str(state.get("nativeHostPath") or "")
        )
        if not host.is_file():
            host = locate_native_host(None)
        ping = native_host_ping(host)
        manifests = configure_native_messaging(extension_id, host, base, browser)
        transaction.commit()
        return {
            "repairedManifests": [_public_path(path) for path in manifests],
            "health": ping.get("status", "ok"),
        }
    except BaseException as exc:
        transaction.rollback(str(exc))
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install and configure mhm runtime components."
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove mhm runtime integration installed for the current user.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print installer discovery as JSON without changing the system.",
    )
    parser.add_argument(
        "--mihomo",
        type=Path,
        help="Use an existing mihomo executable instead of downloading one.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Inspect the installation without changing it.",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Restore Native Messaging registration from saved setup state.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output for --diagnose or --repair.",
    )
    parser.add_argument(
        "--remove-user-data",
        action="store_true",
        help="With --uninstall, also remove profiles, subscriptions, secrets and traffic history.",
    )
    parser.add_argument(
        "--extension-id", help="Chrome/Chromium extension ID resolved at install time."
    )
    parser.add_argument(
        "--extension-url",
        help="Chrome Web Store URL; the ID is extracted from the URL.",
    )
    parser.add_argument(
        "--browser",
        choices=tuple(sorted(SUPPORTED_BROWSERS)),
        default="chrome",
        help="Browser channel to configure.",
    )
    parser.add_argument(
        "--extension-manifest",
        type=Path,
        help="Manifest whose public key is used to calculate the ID.",
    )
    parser.add_argument(
        "--extension-install",
        choices=("auto", "external", "browser", "skip"),
        default="skip",
        help="Extension install strategy. 'auto' uses Web Store URL when supplied, otherwise Chrome external-install metadata.",
    )
    parser.add_argument(
        "--native-host",
        type=Path,
        help="Existing native host executable for configure-only mode.",
    )
    parser.add_argument(
        "--mihomo-version",
        default="latest",
        help="Stable mihomo version, or 'latest' (default).",
    )
    parser.add_argument(
        "--skip-mihomo",
        action="store_true",
        help="Keep the existing mihomo setting instead of downloading it.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting for unresolved installation data.",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not open the Web Store/setup page.",
    )
    parser.add_argument("--machine-phase", action="store_true", help=argparse.SUPPRESS)
    return parser


def _machine_phase(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        raise SetupError("The internal machine phase is Windows-only.")
    if not _is_windows_admin():
        raise SetupError("The internal machine phase must run elevated.")
    if not args.extension_id or args.browser != "chrome":
        raise SetupError("The elevated phase requires a Chrome extension ID.")
    extension_id = validate_extension_id(args.extension_id)
    if args.uninstall:
        remove_external_extension_registration(extension_id)
    else:
        provision_external_extension(extension_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    interactive = not args.non_interactive and sys.stdin.isatty()
    transaction: SetupTransaction | None = None
    operation_lock: OperationLock | None = None

    try:
        if args.machine_phase:
            return _machine_phase(args)

        if args.inspect:
            print(
                json.dumps(
                    {
                        "productVersion": PRODUCT_VERSION,
                        "extensions": detect_installed_extensions(),
                        "mihomo": detect_mihomo(),
                        "channels": {
                            browser: details
                            for browser, details in load_browser_channels().items()
                            if browser in SUPPORTED_BROWSERS
                            and isinstance(details, dict)
                        },
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.diagnose:
            report = diagnose_system()
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                for item in report["checks"]:
                    print(f"{item['status'].upper():7} {item['id']}: {item['summary']}")
            return (
                1 if any(item["status"] == "error" for item in report["checks"]) else 0
            )

        operation_lock = OperationLock()

        if args.repair:
            result = repair_installation()
            print(
                json.dumps(result, ensure_ascii=False, indent=2)
                if args.json
                else f"Repair completed: {len(result['repairedManifests'])} manifest(s)."
            )
            return 0

        if args.uninstall:
            result = uninstall_user_components(remove_user_data=args.remove_user_data)
            print(f"Removed {len(result['removed'])} mhm runtime item(s).")
            if result["keptUserData"]:
                print("Profiles, subscriptions and traffic history were preserved.")
            print(
                "Remove the mhm extension from the browser's extension manager if desired."
            )
            return 0

        extension_manifest = (
            args.extension_manifest.expanduser().resolve()
            if args.extension_manifest
            else None
        )
        extension_id, extension_url = resolve_extension_id(
            args.extension_id,
            args.extension_url,
            extension_manifest,
            interactive=interactive,
            browser=args.browser,
        )
        print(f"Extension ID resolved at install time: {extension_id}")
        already_installed = extension_is_installed(extension_id, args.browser)

        transaction = SetupTransaction("install")
        for path in [
            app_data_dir() / "settings.json",
            app_data_dir() / "mihomo-install.json",
            app_data_dir() / "setup-state.json",
            _user_native_host_pointer(),
            *_native_messaging_targets(args.browser),
        ]:
            transaction.snapshot(path)
        for key_name in native_messaging_registry_keys(args.browser):
            transaction.snapshot_registry(key_name)
        transaction.step("preflight")

        # Auto mode deliberately prefers a user-visible Web Store page when its URL
        # was supplied. A bare install-time ID uses Chrome's documented external
        # installation mechanism, which still requires user confirmation on
        # Windows/macOS and does not embed the ID into any build artifact.
        external_install = False
        browser_install = False
        if not already_installed:
            if args.extension_install == "external":
                external_install = True
            elif args.extension_install == "browser":
                if not extension_url:
                    raise SetupError(
                        "--extension-install browser requires --extension-url when the extension is not installed."
                    )
                browser_install = True
            elif args.extension_install == "auto":
                if extension_url:
                    browser_install = True
                elif extension_manifest and not args.extension_id:
                    print(
                        "The ID was calculated from a manifest key; automatic Web Store installation is skipped for this development source."
                    )
                elif args.browser == "chrome":
                    external_install = True
                else:
                    print(
                        "No store URL supplied; install the extension in the selected browser separately."
                    )

        if external_install and args.browser != "chrome":
            raise SetupError(
                "Managed extension registration is supported only for Chrome. Use browser or skip."
            )

        if external_install and sys.platform == "win32":
            if _is_windows_admin():
                provision_external_extension(extension_id)
            else:
                result = _run_windows_elevated_self(
                    [
                        "--machine-phase",
                        "--extension-id",
                        extension_id,
                        "--non-interactive",
                        "--no-open-browser",
                    ]
                )
                if result != 0:
                    raise SetupError(
                        f"Elevated extension registration failed with exit code {result}."
                    )
        elif external_install and sys.platform == "darwin":
            entries = provision_external_extension(extension_id)
            print(
                f"Registered Chrome Web Store extension metadata in {len(entries)} user location(s)."
            )
        elif external_install and sys.platform.startswith("linux"):
            entries = _provision_linux_external_with_sudo(
                extension_id, interactive=interactive
            )
            print(
                f"Registered Chrome Web Store extension metadata in {len(entries)} system location(s)."
            )
        elif external_install:
            raise SetupError(
                "Automatic external extension installation is not supported on this platform."
            )
        transaction.step(
            "extension-registration",
            "configured" if external_install else "not-required",
        )

        native_host = locate_native_host(args.native_host)
        print(f"Native host: {native_host}")
        transaction.step("native-host")

        if args.mihomo:
            mihomo = probe_mihomo(args.mihomo)
            if not mihomo:
                raise SetupError("--mihomo must point to a working mihomo executable.")
            merge_settings(Path(mihomo["path"]))
        elif args.skip_mihomo:
            candidates = detect_mihomo()
            if not candidates:
                raise SetupError(
                    "--skip-mihomo requires a working installed mihomo or a saved path."
                )
            mihomo = candidates[0]
            merge_settings(Path(mihomo["path"]))
        else:
            mihomo = install_mihomo(args.mihomo_version)
            merge_settings(Path(mihomo["path"]))
        transaction.step("mihomo")

        manifests = configure_native_messaging(
            extension_id, native_host, browser=args.browser
        )
        print(f"Native Messaging configured for {len(manifests)} manifest location(s).")
        transaction.step("native-messaging")

        ping = native_host_ping(native_host)
        print(
            f"Native host health check: OK ({ping.get('version') or ping.get('hostVersion') or 'ready'})"
        )
        save_setup_state(extension_id, native_host, mihomo, browser=args.browser)
        transaction.step("health-check")

        if not args.no_open_browser:
            if browser_install and extension_url:
                print(
                    "Opening the Chrome Web Store. Chrome requires user confirmation to install the extension."
                )
                open_browser_url(extension_url, args.browser)
                if interactive:
                    input(
                        "After the extension is installed/enabled, press Enter to continue..."
                    )
            elif external_install and not already_installed:
                print("Chrome Web Store external-install metadata is registered.")
                print(
                    "Restart Chrome if it is running and confirm enabling the extension when Chrome asks."
                )
                if interactive:
                    input(
                        "After the extension is installed/enabled, press Enter to continue..."
                    )

            if extension_is_installed(extension_id, args.browser):
                setup_url = (
                    f"chrome-extension://{extension_id}/setup.html"
                    if args.browser != "firefox"
                    else "about:addons"
                )
                if not open_browser_url(setup_url, args.browser):
                    print(f"Open this page in the browser: {setup_url}")
            else:
                if extension_url and not browser_install:
                    print(
                        f"If Chrome did not offer the extension automatically, install it here: {extension_url}"
                    )
                print(
                    "The native runtime is ready; open setup.html after the extension becomes available in Chrome."
                )

        print("\nmhm setup completed.")
        print(f"Extension ID: {extension_id}")
        print(f"mihomo: {mihomo['path']}")
        print(
            "The extension setup page can verify the native host and create/import a profile."
        )
        transaction.commit()
        return 0
    except KeyboardInterrupt:
        if transaction is not None and transaction.state == "running":
            transaction.rollback("interrupted")
        print("Setup interrupted; previous configuration restored.", file=sys.stderr)
        return 130
    except SetupError as exc:
        if transaction is not None and transaction.state == "running":
            transaction.rollback(str(exc))
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if transaction is not None and transaction.state == "running":
            transaction.rollback(f"unexpected error: {exc}")
        raise
    finally:
        if operation_lock is not None:
            operation_lock.close()


# Browser store configuration.
def embedded_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def load_browser_channels() -> dict[str, Any]:
    candidates = [
        embedded_root() / "payload" / "browser_channels.json",
        Path(__file__).with_name("browser_channels.json"),
    ]
    data: dict[str, Any] = {}
    for candidate in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
                break
        except (OSError, json.JSONDecodeError):
            continue
    for browser in SUPPORTED_BROWSERS:
        item = data.setdefault(browser, {})
        env_url = os.environ.get(f"MHM_{browser.upper()}_STORE_URL", "").strip()
        if env_url:
            item["store_url"] = env_url
    return data


if __name__ == "__main__":
    raise SystemExit(main())

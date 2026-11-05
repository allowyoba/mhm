"""OS-backed encrypted storage with no plaintext fallback on supported platforms."""

from __future__ import annotations
import os
import sys
import ctypes
import base64
import json
import hashlib
import secrets as pysecrets
import subprocess
import shutil
from pathlib import Path
from typing import Any
from .errors import HostError
from .files import ensure_private_parent, write_private_text

from .identity import NATIVE_HOST as HOST_NAME


class SecretBackend:
    name = "unknown"
    secure = False

    def put(self, profile_id: str, secret: str) -> None:
        raise NotImplementedError

    def get(self, profile_id: str) -> str:
        raise NotImplementedError

    def delete(self, profile_id: str) -> None:
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "secure": self.secure}


class FileSecretBackend(SecretBackend):
    name = "file-0600-fallback"
    secure = False

    def __init__(self, base_dir: Path) -> None:
        self.path = base_dir / "profiles.secrets.json"

    def _load(self) -> dict[str, str]:
        try:
            if self.path.exists():
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return {str(k): str(v) for k, v in value.items()}
        except Exception as exc:
            raise HostError(f"Could not read fallback profile storage: {exc}") from exc
        return {}

    def _save(self, values: dict[str, str]) -> None:
        write_private_text(self.path, json.dumps(values, ensure_ascii=False))

    def put(self, profile_id: str, secret: str) -> None:
        values = self._load()
        values[profile_id] = secret
        self._save(values)

    def get(self, profile_id: str) -> str:
        value = self._load().get(profile_id)
        if value is None:
            raise HostError("Profile secret not found", "NOT_FOUND")
        return value

    def delete(self, profile_id: str) -> None:
        values = self._load()
        values.pop(profile_id, None)
        self._save(values)


if os.name == "nt":

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_ulong),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]


class WindowsDpapiBackend(SecretBackend):
    name = "windows-dpapi-current-user"
    secure = True

    def __init__(self, base_dir: Path) -> None:
        self.path = base_dir / "profiles.secrets.dpapi"
        blob_ptr = ctypes.POINTER(DATA_BLOB)
        self.crypt32 = ctypes.windll.crypt32
        self.kernel32 = ctypes.windll.kernel32
        self.crypt32.CryptProtectData.argtypes = [
            blob_ptr,
            ctypes.c_wchar_p,
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            blob_ptr,
        ]
        self.crypt32.CryptProtectData.restype = ctypes.c_int
        self.crypt32.CryptUnprotectData.argtypes = [
            blob_ptr,
            ctypes.c_void_p,
            blob_ptr,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            blob_ptr,
        ]
        self.crypt32.CryptUnprotectData.restype = ctypes.c_int
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _blob(data: bytes):
        buffer = ctypes.create_string_buffer(data)
        blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    def _protect(self, data: bytes) -> bytes:
        in_blob, _ = self._blob(data)
        out_blob = DATA_BLOB()
        if not self.crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
        ):
            raise HostError("Windows DPAPI: could not encrypt profile secrets")
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self.kernel32.LocalFree(out_blob.pbData)

    def _unprotect(self, data: bytes) -> bytes:
        in_blob, _ = self._blob(data)
        out_blob = DATA_BLOB()
        if not self.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
        ):
            raise HostError(
                "Windows DPAPI: could not decrypt profile secrets for the current user"
            )
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self.kernel32.LocalFree(out_blob.pbData)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            plain = self._unprotect(base64.b64decode(self.path.read_bytes()))
            value = json.loads(plain.decode("utf-8"))
            return (
                {str(k): str(v) for k, v in value.items()}
                if isinstance(value, dict)
                else {}
            )
        except HostError:
            raise
        except Exception as exc:
            raise HostError(
                f"Windows DPAPI: profile secret storage is corrupted: {exc}"
            ) from exc

    def _save(self, values: dict[str, str]) -> None:
        plain = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        protected = base64.b64encode(self._protect(plain)).decode("ascii")
        write_private_text(self.path, protected)

    def put(self, profile_id: str, secret: str) -> None:
        values = self._load()
        values[profile_id] = secret
        self._save(values)

    def get(self, profile_id: str) -> str:
        value = self._load().get(profile_id)
        if value is None:
            raise HostError("Profile secret not found", "NOT_FOUND")
        return value

    def delete(self, profile_id: str) -> None:
        values = self._load()
        values.pop(profile_id, None)
        self._save(values)


class EncryptedSecretBackend(SecretBackend):
    secure = True

    def __init__(self, base_dir: Path) -> None:
        self.path = base_dir / "profiles.secrets.encrypted"
        self.key_account = (
            "mhm-storage-key-"
            + hashlib.sha256(str(base_dir.resolve()).encode()).hexdigest()[:16]
        )

    def _platform_secret(
        self, account: str, value: bytes | None = None, delete=False
    ) -> bytes:
        raise NotImplementedError

    def _key(self, create=False):
        try:
            return self._platform_secret(self.key_account)
        except HostError as exc:
            if exc.code != "NOT_FOUND" or not create:
                raise
            key = pysecrets.token_bytes(32)
            self._platform_secret(self.key_account, key)
            return key

    def _load(self):
        if not self.path.exists():
            return {}
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        try:
            data = base64.b64decode(self.path.read_bytes(), validate=True)
            return json.loads(
                AESGCM(self._key()).decrypt(data[:12], data[12:], HOST_NAME.encode())
            )
        except HostError:
            raise
        except Exception as exc:
            raise HostError("Encrypted profile storage could not be opened") from exc

    def _save(self, values):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = pysecrets.token_bytes(12)
        data = json.dumps(values, ensure_ascii=False).encode()
        encrypted = AESGCM(self._key(create=True)).encrypt(
            nonce, data, HOST_NAME.encode()
        )
        write_private_text(self.path, base64.b64encode(nonce + encrypted).decode())

    def put(self, profile_id: str, secret: str) -> None:
        values = self._load()
        values[profile_id] = secret
        self._save(values)

    def get(self, profile_id: str) -> str:
        values = self._load()
        if profile_id in values:
            return values[profile_id]
        return self._platform_secret(profile_id).decode()

    def delete(self, profile_id: str) -> None:
        values = self._load()
        if profile_id in values:
            values.pop(profile_id)
            self._save(values)
        else:
            self._platform_secret(profile_id, delete=True)


class MacKeychainBackend(EncryptedSecretBackend):
    name = "macos-keychain-aes-gcm"

    def __init__(self, security_bin: str, base_dir: Path) -> None:
        super().__init__(base_dir)

    def _platform_secret(
        self, account: str, value: bytes | None = None, delete=False
    ) -> bytes:
        # Native Security API keeps secret values out of process arguments.
        from ctypes import c_void_p, c_long, c_char_p, c_ubyte, POINTER

        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFDataCreate.argtypes = [c_void_p, c_void_p, c_long]
        cf.CFDataCreate.restype = c_void_p
        cf.CFDictionaryCreate.argtypes = [
            c_void_p,
            POINTER(c_void_p),
            POINTER(c_void_p),
            c_long,
            c_void_p,
            c_void_p,
        ]
        cf.CFDictionaryCreate.restype = c_void_p
        cf.CFDataGetLength.argtypes = [c_void_p]
        cf.CFDataGetLength.restype = c_long
        cf.CFDataGetBytePtr.argtypes = [c_void_p]
        cf.CFDataGetBytePtr.restype = POINTER(c_ubyte)
        cf.CFRelease.argtypes = [c_void_p]
        security.SecItemCopyMatching.argtypes = [c_void_p, POINTER(c_void_p)]
        security.SecItemCopyMatching.restype = ctypes.c_int32
        security.SecItemAdd.argtypes = [c_void_p, POINTER(c_void_p)]
        security.SecItemAdd.restype = ctypes.c_int32
        security.SecItemUpdate.argtypes = [c_void_p, c_void_p]
        security.SecItemUpdate.restype = ctypes.c_int32
        security.SecItemDelete.argtypes = [c_void_p]
        security.SecItemDelete.restype = ctypes.c_int32
        owned = []

        def constant(name):
            return c_void_p.in_dll(security, name).value

        def string(text):
            item = cf.CFStringCreateWithCString(None, text.encode(), 0x08000100)
            owned.append(item)
            return item

        def dictionary(pairs):
            keys = (c_void_p * len(pairs))(*(p[0] for p in pairs))
            values = (c_void_p * len(pairs))(*(p[1] for p in pairs))
            result = cf.CFDictionaryCreate(None, keys, values, len(pairs), None, None)
            owned.append(result)
            return result

        try:
            pairs = [
                (constant("kSecClass"), constant("kSecClassGenericPassword")),
                (constant("kSecAttrService"), string(HOST_NAME)),
                (constant("kSecAttrAccount"), string(account)),
            ]
            query = dictionary(pairs)
            if delete:
                status = security.SecItemDelete(query)
            elif value is not None:
                buffer = ctypes.create_string_buffer(value)
                data = cf.CFDataCreate(None, buffer, len(value))
                owned.append(data)
                attributes = [(constant("kSecValueData"), data)]
                status = security.SecItemUpdate(query, dictionary(attributes))
                if status == -25300:
                    status = security.SecItemAdd(dictionary(pairs + attributes), None)
            else:
                true_value = c_void_p.in_dll(cf, "kCFBooleanTrue").value
                result = c_void_p()
                status = security.SecItemCopyMatching(
                    dictionary(pairs + [(constant("kSecReturnData"), true_value)]),
                    ctypes.byref(result),
                )
                if status == 0:
                    try:
                        return ctypes.string_at(
                            cf.CFDataGetBytePtr(result), cf.CFDataGetLength(result)
                        )
                    finally:
                        cf.CFRelease(result)
            if status == -25300:
                if delete:
                    return b""
                raise HostError("Secret not found in Keychain", "NOT_FOUND")
            if status != 0:
                raise HostError(
                    "Unlock the macOS login Keychain and allow mhm to access its encryption key",
                    "DEPENDENCY_UNAVAILABLE",
                )
            return b""
        finally:
            for item in reversed(owned):
                cf.CFRelease(item)


class LinuxSecretBackend(EncryptedSecretBackend):
    name = "linux-secret-service-aes-gcm"

    def __init__(self, secret_tool: str | None, base_dir: Path) -> None:
        super().__init__(base_dir)
        self.secret_tool = secret_tool
        self.secure = bool(secret_tool and os.environ.get("DBUS_SESSION_BUS_ADDRESS"))

    def get(self, profile_id: str) -> str:
        try:
            return super().get(profile_id)
        except HostError as exc:
            if exc.code != "NOT_FOUND":
                raise
        legacy = self.path.with_name("profiles.secrets.json")
        if not legacy.exists():
            raise HostError("Profile secret not found", "NOT_FOUND")
        if legacy.stat().st_mode & 0o077:
            raise HostError(
                "Restrict legacy secret-file permissions to the current user before migration"
            )
        values = json.loads(legacy.read_text(encoding="utf-8"))
        if profile_id not in values:
            raise HostError("Profile secret not found", "NOT_FOUND")
        value = str(values[profile_id])
        self.put(profile_id, value)
        del values[profile_id]
        if values:
            write_private_text(legacy, json.dumps(values))
        else:
            legacy.unlink()
        return value

    def _platform_secret(
        self, account: str, value: bytes | None = None, delete=False
    ) -> bytes:
        if not self.secret_tool:
            raise HostError(
                "Install libsecret-tools and unlock a desktop Secret Service keyring.",
                "DEPENDENCY_UNAVAILABLE",
            )
        attrs = ["application", HOST_NAME, "profile", account]
        if delete:
            command = [self.secret_tool, "clear", *attrs]
        elif value is not None:
            command = [
                self.secret_tool,
                "store",
                "--label",
                "mhm encryption key",
                *attrs,
            ]
        else:
            command = [self.secret_tool, "lookup", *attrs]
        try:
            result = subprocess.run(
                command,
                input=base64.b64encode(value) if value is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(
                "Unlock your desktop keyring and check Secret Service availability.",
                "DEPENDENCY_UNAVAILABLE",
            ) from exc
        if result.returncode:
            if value is None and not delete and result.returncode == 1:
                raise HostError("Secret not found in the desktop keyring", "NOT_FOUND")
            if delete and result.returncode == 1:
                return b""
            raise HostError(
                "Unlock your desktop keyring and check Secret Service availability.",
                "DEPENDENCY_UNAVAILABLE",
            )
        if value is None and not delete:
            raw = result.stdout.strip()
            try:
                return base64.b64decode(raw, validate=True)
            except ValueError:
                return raw  # Import earlier URI secrets on first access.
        return b""


def choose_secret_backend(base_dir: Path) -> SecretBackend:
    if os.name == "nt":
        return WindowsDpapiBackend(base_dir)
    if sys.platform == "darwin":
        return MacKeychainBackend("/usr/bin/security", base_dir)
    if sys.platform.startswith("linux"):
        return LinuxSecretBackend(shutil.which("secret-tool"), base_dir)
    raise HostError("This operating system is not supported", "DEPENDENCY_UNAVAILABLE")

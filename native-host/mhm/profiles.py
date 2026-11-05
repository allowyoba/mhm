"""Profile metadata repository and encrypted-source lifecycle service."""

from __future__ import annotations
import contextlib
import functools
import hashlib
import json
import re
import time
import uuid as uuidlib
from typing import Any
from .errors import HostError
from .files import write_private_text
from .config import (
    checked_profile_id,
    legacy_mihomo_yaml,
    profile_document,
    wireguard_proxy,
    _validate_subscription_url,
)
from .tls import normalize_subscription_ca
from .policy import with_custom_rules, profile_revision


def transactional(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self.transaction():
            return method(self, *args, **kwargs)

    return call


class ProfileIndex:
    def __init__(self, index_path):
        self.index_path = index_path

    def _load_metadata(self) -> list[dict[str, Any]]:
        try:
            if not self.index_path.exists():
                return []
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        except Exception as exc:
            raise HostError(f"Could not read profile list: {exc}") from exc
        return []

    def _save_metadata(self, items: list[dict[str, Any]]) -> None:
        write_private_text(
            self.index_path, json.dumps(items, ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _sort(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            items,
            key=lambda x: (str(x.get("name", "")).casefold(), str(x.get("id", ""))),
        )

    def list(self) -> list[dict[str, Any]]:
        return self._sort(self._load_metadata())

    def version(self) -> str:
        payload = json.dumps(
            self.list(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def get_meta(self, profile_id: str) -> dict[str, Any]:
        checked_profile_id(profile_id)
        for item in self._load_metadata():
            if item.get("id") == profile_id:
                return item
        raise HostError("Profile not found", "NOT_FOUND")


class ProfileService(ProfileIndex):
    def __init__(
        self,
        index_path,
        secrets,
        validate,
        fetch,
        transaction,
        subscriptions_path,
        is_active,
    ):
        super().__init__(index_path)
        self.secrets = secrets
        self.validate = validate
        self.fetch = fetch
        self.transaction = transaction
        self.subscriptions_path = subscriptions_path
        self.is_active = is_active

    @transactional
    def bundle(self, profile_id: str) -> dict[str, Any]:
        meta = self.get_meta(profile_id)
        raw = self.secrets.get(str(meta.get("secretKey") or profile_id))
        if meta.get("format") != "mihomo":
            return {
                "yaml": legacy_mihomo_yaml(raw),
                "source": "local",
                "url": "",
                "headers": {},
                "wireguard": "",
            }
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise HostError("Could not read the encrypted profile") from exc

    @transactional
    def commit(
        self, profile_id: str, meta: dict[str, Any], bundle: dict[str, Any]
    ) -> dict[str, Any]:
        items = self._load_metadata()
        old = next((p for p in items if p["id"] == profile_id), None)
        key = f"mihomo-{profile_id}-{uuidlib.uuid4()}"
        self.secrets.put(key, json.dumps(bundle, ensure_ascii=False))
        meta = {**meta, "id": profile_id, "format": "mihomo", "secretKey": key}
        try:
            self._save_metadata([p for p in items if p["id"] != profile_id] + [meta])
        except Exception:
            self.secrets.delete(key)
            raise
        if old and old.get("secretKey"):
            with contextlib.suppress(Exception):
                self.secrets.delete(old["secretKey"])
        return self.public(meta)

    @staticmethod
    def public(meta: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v
            for k, v in meta.items()
            if k
            not in {"secretKey", "maskedUrl", "server", "uuid", "publicKey", "shortId"}
        }

    @transactional
    def list(self) -> list[dict[str, Any]]:
        items = super().list()
        upgraded = {
            p.get("legacySubscriptionId")
            for p in items
            if p.get("legacySubscriptionId") and not p.get("needsDownload")
        }
        return [
            self.public(p)
            for p in items
            if not p.get("subscriptionId") or p.get("subscriptionId") not in upgraded
        ]

    @transactional
    def save_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        pid = checked_profile_id(str(payload.get("profileId") or uuidlib.uuid4()))
        existing = self.get_meta(pid) if payload.get("profileId") else {}
        bundle = (
            self.bundle(pid)
            if existing
            else {"yaml": "", "url": "", "headers": {}, "wireguard": ""}
        )
        name = str(payload.get("name") or existing.get("name") or "Profile").strip()
        if not name or len(name) > 80:
            raise HostError("Enter a profile name up to 80 characters")
        source = str(payload.get("source") or bundle.get("source") or "local")
        if source not in {"local", "subscription"}:
            raise HostError("Unknown profile source")
        bundle["source"] = source
        if source == "subscription":
            url = str(payload.get("url") or bundle.get("url") or "").strip()
            if len(url) > 8192:
                raise HostError("Subscription URL exceeds 8 KB")
            _validate_subscription_url(url, "direct")
            bundle["url"] = url
            headers = payload.get("headers", bundle.get("headers", {}))
            if not isinstance(headers, dict) or len(headers) > 20:
                raise HostError("Headers must be a JSON object")
            for key, value in headers.items():
                if (
                    not isinstance(key, str)
                    or not re.fullmatch(r"[A-Za-z0-9-]+", key)
                    or not isinstance(value, str)
                    or "\r" in value
                    or "\n" in value
                ):
                    raise HostError("Invalid subscription headers")
                if key.lower() in {
                    "host",
                    "connection",
                    "content-length",
                    "accept-encoding",
                    "proxy-authorization",
                }:
                    raise HostError("This HTTP header cannot be overridden")
            if len(json.dumps(headers)) > 16384:
                raise HostError("HTTP headers exceed 16 KB")
            bundle["headers"] = headers
            mode = payload.get("tlsMode", bundle.get("tlsMode", "system"))
            if mode not in {"system", "custom", "insecure"}:
                raise HostError("Choose a valid HTTPS certificate verification mode")
            bundle["tlsMode"] = mode
            if "caCertificate" in payload:
                value = payload["caCertificate"]
                bundle["caCertificate"] = (
                    normalize_subscription_ca(value) if value else ""
                )
            if mode == "custom" and not bundle.get("caCertificate"):
                raise HostError("Import a CA certificate for this subscription")
            transport = str(
                payload.get("transport") or existing.get("transport") or "direct"
            )
            if transport not in {"direct", "wireguard"}:
                raise HostError("Choose Direct or WireGuard")
            if "wireguard" in payload:
                bundle["wireguard"] = str(payload["wireguard"])
            if transport == "wireguard":
                wireguard_proxy(bundle.get("wireguard", ""))
            interval = int(
                payload.get(
                    "updateIntervalMinutes", existing.get("updateIntervalMinutes", 1440)
                )
            )
            if interval not in {0, 360, 720, 1440}:
                raise HostError("Invalid update interval")
        else:
            transport, interval = "direct", 0
            bundle.update(
                {
                    "url": "",
                    "headers": {},
                    "wireguard": "",
                    "tlsMode": "system",
                    "caCertificate": "",
                }
            )
        content = payload.get("yaml")
        if content is not None and str(content).strip():
            bundle["yaml"] = str(content)
        if source == "subscription":
            content, usage = self.fetch(bundle, transport)
            bundle["yaml"] = content
        else:
            usage = None
        doc = with_custom_rules(profile_document(bundle.get("yaml", "")), bundle)
        self.validate(doc, pid)
        revision = profile_revision(bundle)
        now = int(time.time())
        if existing and revision != existing.get("revision"):
            bundle["previousYaml"] = self.bundle(pid).get("yaml")
        meta = {
            **existing,
            "id": pid,
            "name": name,
            "source": source,
            "transport": transport,
            "tlsMode": bundle.get("tlsMode", "system"),
            "updateIntervalMinutes": interval,
            "createdAt": existing.get("createdAt", now),
            "updatedAt": now,
            "lastUpdatedAt": now if source == "subscription" else None,
            "lastAttemptAt": now if source == "subscription" else None,
            "lastError": None,
            "revision": revision,
            "proxyCount": len(doc.get("proxies", [])),
            "groupCount": len(doc.get("proxy-groups", [])),
            "usage": usage,
            "hasRollback": bool(bundle.get("previousYaml")),
            "needsDownload": False,
            "needsTransportSetup": False,
        }
        return self.commit(pid, meta, bundle)

    @transactional
    def update(self, profile_id: str) -> dict[str, Any]:
        meta = self.get_meta(profile_id)
        if meta.get("source") != "subscription":
            raise HostError("This is a local profile")
        try:
            return self.save_source({"profileId": profile_id})
        except Exception:
            items = self._load_metadata()
            for item in items:
                if item["id"] == profile_id:
                    item.update(
                        lastAttemptAt=int(time.time()),
                        lastError="Subscription update failed. The saved profile is available.",
                    )
            self._save_metadata(items)
            raise

    @transactional
    def rollback(self, profile_id: str) -> dict[str, Any]:
        meta, bundle = self.get_meta(profile_id), self.bundle(profile_id)
        previous = bundle.get("previousYaml")
        if not previous:
            raise HostError("No previous revision is available")
        self.validate(with_custom_rules(profile_document(previous), bundle), profile_id)
        bundle["yaml"], bundle["previousYaml"] = previous, bundle["yaml"]
        meta.update(
            revision=profile_revision(bundle),
            updatedAt=int(time.time()),
            lastError=None,
        )
        return self.commit(profile_id, meta, bundle)

    @transactional
    def delete(self, profile_id: str) -> None:
        meta = self.get_meta(profile_id)
        source_id = meta.get("legacySubscriptionId")
        items = self._load_metadata()
        removed = [
            p
            for p in items
            if p["id"] == profile_id
            or source_id
            and p.get("subscriptionId") == source_id
        ]
        if any(self.is_active(p["id"]) for p in removed):
            raise HostError("Disconnect this profile before deleting it", "CONFLICT")
        if source_id and self.subscriptions_path.exists():
            subscriptions = json.loads(
                self.subscriptions_path.read_text(encoding="utf-8")
            )
            write_private_text(
                self.subscriptions_path,
                json.dumps([p for p in subscriptions if p.get("id") != source_id]),
            )
            with contextlib.suppress(HostError):
                self.secrets.delete("subscription:" + source_id)
        self._save_metadata([p for p in items if p not in removed])
        for item in removed:
            self.secrets.delete(str(item.get("secretKey") or item["id"]))
            with contextlib.suppress(HostError):
                self.secrets.delete("resources-" + item["id"])

#!/usr/bin/env python3
"""Fail early when a mhm source tree is not ready to publish."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM_ID = re.compile(r"^[a-p]{32}$")


def read_json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def source_version(relative: str, pattern: str) -> str | None:
    match = re.search(pattern, (ROOT / relative).read_text(encoding="utf-8"))
    return match.group(1) if match else None


def validate(release: bool) -> list[str]:
    errors: list[str] = []
    manifest_version = str(read_json("extension/manifest.json").get("version"))
    versions = {
        "product.json": str(read_json("product.json").get("version")),
        "native-host/mhm/version.py": source_version(
            "native-host/mhm/version.py", r'VERSION\s*=\s*"([^\"]+)"'
        ),
        "setup/_version.py": source_version(
            "setup/_version.py", r'VERSION\s*=\s*"([^\"]+)"'
        ),
        "extension/lib/version.js": source_version(
            "extension/lib/version.js", r'VERSION\s*=\s*"([^\"]+)"'
        ),
    }
    for path, version in versions.items():
        if version != manifest_version:
            errors.append(
                f"version mismatch: {path} is {version!r}, expected {manifest_version!r}"
            )

    try:
        schema = read_json("protocol/native-messaging.schema.json")
        if schema.get("$schema") is None or schema.get("oneOf") is None:
            errors.append("native messaging schema is incomplete")
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid native messaging schema: {exc}")

    channels = read_json("setup/browser_channels.json")
    for browser in ("chrome", "edge", "firefox", "opera"):
        item = channels.get(browser) if isinstance(channels, dict) else None
        if not isinstance(item, dict):
            errors.append(f"missing browser channel: {browser}")
            continue
        store_id = str(item.get("store_id") or "").strip()
        store_url = str(item.get("store_url") or "").strip()
        if release and (not store_id or not store_url):
            errors.append(f"release channel {browser} requires store_id and store_url")
            continue
        if store_url:
            parsed = urllib.parse.urlparse(store_url)
            if parsed.scheme != "https" or not parsed.hostname:
                errors.append(f"invalid HTTPS store URL for {browser}")
        if (
            store_id
            and browser in {"chrome", "edge", "opera"}
            and not CHROMIUM_ID.fullmatch(store_id)
        ):
            errors.append(f"invalid Chromium store ID for {browser}")
        if store_id and browser == "firefox" and "@" not in store_id:
            errors.append("invalid Firefox addon ID")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release",
        action="store_true",
        help="Require signed-store publication metadata.",
    )
    args = parser.parse_args()
    errors = validate(args.release)
    if errors:
        print("Release validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Release validation passed" + (" in release mode." if args.release else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

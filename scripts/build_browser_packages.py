#!/usr/bin/env python3
"""Build deterministic mhm extension packages for Chrome, Opera, and Firefox."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import zipfile
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension"
VERSION = json.loads((SOURCE / "manifest.json").read_text())["version"]
sys.path.insert(0, str(ROOT))
from setup.browsers import BROWSERS as BROWSER_SPECS
from setup._identity import FIREFOX_ID

FIREFOX_VERSION = json.loads((ROOT / "product.json").read_text())["firefoxVersion"]
BROWSERS = tuple(BROWSER_SPECS)

EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".DS_Store"}


def manifest_for(browser: str) -> dict:
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    if browser == "firefox":
        manifest["version"] = FIREFOX_VERSION
        manifest.pop("minimum_chrome_version", None)
        manifest["background"] = {
            "scripts": [
                "background/identity.js",
                "background/native.js",
                "background/proxy.js",
                "background/action.js",
                "service-worker.js",
            ]
        }
        manifest["incognito"] = "spanning"
        manifest["browser_specific_settings"] = {
            "gecko": {
                "id": FIREFOX_ID,
                "strict_min_version": "140.0",
                "data_collection_permissions": {"required": ["none"]},
            },
        }
    return manifest


def validate_manifest(browser: str, manifest: dict) -> None:
    if manifest.get("manifest_version") != 3:
        raise RuntimeError(f"{browser}: Manifest V3 is required")
    expected = FIREFOX_VERSION if browser == "firefox" else VERSION
    if manifest.get("version") != expected:
        raise RuntimeError(f"{browser}: manifest version must be {expected}")
    if browser == "firefox":
        gecko = manifest.get("browser_specific_settings", {}).get("gecko", {})
        if gecko.get("id") != FIREFOX_ID:
            raise RuntimeError("firefox: stable Gecko ID is required")
        if gecko.get("data_collection_permissions", {}).get("required") != ["none"]:
            raise RuntimeError(
                "firefox: data_collection_permissions must explicitly declare none"
            )
        if "scripts" not in manifest.get("background", {}):
            raise RuntimeError("firefox: background.scripts is required")
    elif "service_worker" not in manifest.get("background", {}):
        raise RuntimeError(f"{browser}: background.service_worker is required")


def should_include(path: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    return path.name != "package.json" and path.suffix not in {".pyc", ".pyo"}


def archive_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zf:
        for path in sorted(source.rglob("*")):
            if not path.is_file() or not should_include(path):
                continue
            info = zipfile.ZipInfo(path.relative_to(source).as_posix())
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())


def build(browser: str, output: Path, tag: str) -> Path:
    manifest = manifest_for(browser)
    validate_manifest(browser, manifest)
    suffix = "xpi" if browser == "firefox" else "zip"
    target = output / f"mhm-extension-{browser}-{tag}.{suffix}"
    with tempfile.TemporaryDirectory(prefix=f"mhm-{browser}-") as tmp:
        stage = Path(tmp) / "extension"
        shutil.copytree(SOURCE, stage)
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        archive_tree(stage, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag", default=VERSION)
    parser.add_argument("--browser", choices=(*BROWSERS, "all"), default="all")
    args = parser.parse_args()
    browsers = BROWSERS if args.browser == "all" else (args.browser,)
    for browser in browsers:
        print(build(browser, args.output, args.tag))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

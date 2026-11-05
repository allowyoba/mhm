#!/usr/bin/env python3
"""Exercise the frozen installer in an isolated user home on its build OS.

Uses a real local mihomo executable (PATH or --mihomo).
No proxy connection or browser installation is performed.
"""

from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def preserve_windows_registration():
    saved = []
    if sys.platform == "win32":
        import winreg

        for owner in (
            r"Software\Google\Chrome",
            r"Software\Chromium",
            r"Software\Microsoft\Edge",
            r"Software\Mozilla",
        ):
            name = owner + r"\NativeMessagingHosts\io.mhm.browser"
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name) as key:
                    value, kind = winreg.QueryValueEx(key, None)
                saved.append((name, value, kind))
            except FileNotFoundError:
                saved.append((name, None, None))
    try:
        yield
    finally:
        if sys.platform == "win32":
            import winreg

            for name, value, kind in saved:
                if value is None:
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)
                    except FileNotFoundError:
                        pass
                else:
                    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, name) as key:
                        winreg.SetValueEx(key, None, 0, kind, value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--mihomo", type=Path)
    args = parser.parse_args()
    version = json.loads((ROOT / "extension" / "manifest.json").read_text())["version"]
    candidates = sorted((ROOT / "dist").glob(f"mhm-setup-{version}-*"))
    installer = args.installer or (candidates[0] if len(candidates) == 1 else None)
    if installer is None:
        parser.error(
            "select --installer when dist contains zero or multiple platform builds"
        )
    installer = installer.resolve()
    with tempfile.TemporaryDirectory(
        prefix="mhm-smoke-"
    ) as temporary, preserve_windows_registration():
        root = Path(temporary)
        binary = (
            args.mihomo.resolve()
            if args.mihomo
            else root / ("mihomo.exe" if sys.platform == "win32" else "mihomo")
        )
        if args.mihomo is None:
            import shutil

            found = shutil.which("mihomo")
            if not found:
                parser.error(
                    "mihomo is required for the smoke check; install it or pass --mihomo"
                )
            binary = Path(found).resolve()
        for browser in ("chrome", "edge", "firefox", "opera"):
            home = root / browser
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "USERPROFILE": str(home),
                "LOCALAPPDATA": str(home / "local"),
                "XDG_DATA_HOME": str(home / "data"),
                "XDG_CONFIG_HOME": str(home / "config"),
                "XDG_CACHE_HOME": str(home / "cache"),
            }
            base = (
                home / "local" / "mhm"
                if sys.platform == "win32"
                else (
                    home / "Library" / "Application Support" / "mhm"
                    if sys.platform == "darwin"
                    else home / "data" / "mhm"
                )
            )

            def run(*arguments, expected=0):
                result = subprocess.run(
                    [str(installer), *arguments],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if result.returncode != expected:
                    raise RuntimeError(
                        f"{arguments}: exit {result.returncode}\n{result.stdout}\n{result.stderr}"
                    )
                return result.stdout

            identity = (
                "mhm@allowyoba.github.io"
                if browser == "firefox"
                else "abcdefghijklmnopabcdefghijklmnop"
            )
            install = [
                "install",
                "--browser",
                browser,
                "--extension-id",
                identity,
                "--extension-install",
                "skip",
                "--mihomo",
                str(binary),
                "--non-interactive",
                "--no-open-browser",
            ]
            run(*install)
            assert "channels" in json.loads(run("inspect"))
            sentinel = base / "acceptance-data.txt"
            sentinel.write_text("keep me")
            settings_path = base / "settings.json"
            settings = json.loads(settings_path.read_text())
            settings["acceptanceCustom"] = True
            settings_path.write_text(json.dumps(settings))
            run(*install)
            assert json.loads(settings_path.read_text())["acceptanceCustom"]
            pointer_before = (base / "native_host" / "current.json").read_bytes()
            invalid = install.copy()
            invalid[invalid.index("--extension-id") + 1] = "invalid"
            run(*invalid, expected=1)
            assert (
                base / "native_host" / "current.json"
            ).read_bytes() == pointer_before
            report = json.loads(run("diagnose", "--json"))
            assert not any(item["status"] == "error" for item in report["checks"])
            manifest = base / f"io.mhm.browser-{browser}.json"
            manifest.unlink()
            run("repair", "--json")
            assert manifest.is_file()
            run("uninstall")
            assert sentinel.read_text() == "keep me"
            assert not (base / "native_host").exists()
            run(*install)
            run("uninstall", "--remove-user-data")
            assert not base.exists()
            print(
                f"{browser}: install/upgrade/invalid-input/diagnose/repair/uninstall PASS",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

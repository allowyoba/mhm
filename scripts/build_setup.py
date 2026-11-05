#!/usr/bin/env python3
"""Build the Python installer and its native-host payload for the current OS."""

from __future__ import annotations

import argparse
import os
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build" / "setup"


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    expected_arch = (
        "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"
    )
    python = sys.executable
    host_dist = BUILD / "host-dist"
    host_work = BUILD / "host-work"
    setup_dist = BUILD / "setup-dist"
    setup_work = BUILD / "setup-work"
    for path in (host_dist, host_work, setup_dist, setup_work):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)

    run(
        [
            python,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            "native_host",
            "--distpath",
            str(host_dist),
            "--workpath",
            str(host_work),
            "--specpath",
            str(BUILD),
            "native-host/native_host.py",
        ]
    )
    archive = BUILD / "native_host.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        source = host_dist / "native_host"
        for path in sorted(source.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(source))

    separator = ";" if os.name == "nt" else ":"
    run(
        [
            python,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            "mhm-setup",
            "--distpath",
            str(setup_dist),
            "--workpath",
            str(setup_work),
            "--specpath",
            str(BUILD),
            "--add-data",
            f"{archive}{separator}payload",
            "--add-data",
            f"{ROOT / 'setup' / 'browser_channels.json'}{separator}payload",
            "setup/main.py",
        ]
    )
    suffix = ".exe" if os.name == "nt" else ""
    source = setup_dist / f"mhm-setup{suffix}"
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    version = json.loads((ROOT / "extension" / "manifest.json").read_text())["version"]
    system = (
        "windows"
        if os.name == "nt"
        else "macos" if sys.platform == "darwin" else "linux"
    )
    target = args.output / f"mhm-setup-{version}-{system}-{expected_arch}{suffix}"
    target.unlink(missing_ok=True)
    shutil.copy2(source, target)
    for path in (host_dist, host_work, setup_dist, setup_work):
        shutil.rmtree(path)
    for path in BUILD.glob("*.spec"):
        path.unlink()
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

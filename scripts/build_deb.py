#!/usr/bin/env python3
"""Build a Debian package from a Linux native-host build and verified mihomo."""

from __future__ import annotations
import argparse
import gzip
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import struct
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setup import runtime


def build(output, architecture=None, engine=None):
    if not sys.platform.startswith("linux"):
        raise RuntimeError(
            "Build the Debian package on Linux or through scripts/build_linux.sh"
        )
    architecture = architecture or (
        "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"
    )
    detected = (
        "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"
    )
    if architecture != detected:
        raise RuntimeError("Build each Debian architecture on a matching Linux runner")
    archive = ROOT / ".build/setup/native_host.zip"
    if not archive.exists():
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/build_setup.py")], check=True, cwd=ROOT
        )
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = json.loads((ROOT / "product.json").read_text())["version"]
    with tempfile.TemporaryDirectory(prefix="mhm-deb-") as temporary:
        stage = Path(temporary)
        lib = stage / "usr/lib/mhm"
        lib.mkdir(parents=True)
        host = lib / "native_host"
        host.mkdir()
        with zipfile.ZipFile(archive) as payload:
            for entry in payload.infolist():
                target = (host / entry.filename).resolve()
                if not target.is_relative_to(host.resolve()):
                    raise RuntimeError("Unsafe native-host payload path")
            payload.extractall(host)
        header = (host / "native_host").read_bytes()[:20]
        expected_machine = 183 if architecture == "arm64" else 62
        if (
            header[:4] != b"\x7fELF"
            or len(header) < 20
            or struct.unpack("<H", header[18:20])[0] != expected_machine
        ):
            raise RuntimeError(
                "Native-host payload does not match the Debian architecture. Rebuild it on Linux."
            )
        (host / "native_host").chmod(0o755)
        shutil.copytree(
            ROOT / "setup", lib / "setup", ignore=shutil.ignore_patterns("__pycache__")
        )
        if engine:
            if not runtime.probe_mihomo(Path(engine)):
                raise RuntimeError(
                    "The supplied mihomo executable does not match this build platform"
                )
            shutil.copy2(engine, lib / "mihomo")
        else:
            asset = runtime.resolve_mihomo_asset()
            compressed = stage / asset["name"]
            runtime._download_verified(asset["url"], compressed, asset["sha256"])
            runtime._extract_mihomo(compressed, asset["archiveType"], lib / "mihomo")
            compressed.unlink()
        (lib / "mihomo").chmod(0o755)
        executable = stage / "usr/bin/mhm-setup"
        executable.parent.mkdir(parents=True)
        executable.write_text("""#!/usr/bin/python3
import sys
from pathlib import Path
sys.path.insert(0, '/usr/lib/mhm')
from setup.main import main
args = sys.argv[1:]
if args and args[0] == 'install':
    if not any(arg == '--native-host' or arg.startswith('--native-host=') for arg in args):
        args += ['--native-host', '/usr/lib/mhm/native_host/native_host']
    if not any(arg == '--mihomo' or arg.startswith('--mihomo=') for arg in args) and '--skip-mihomo' not in args:
        args += ['--mihomo', '/usr/lib/mhm/mihomo']
sys.exit(main(args))
""")
        executable.chmod(0o755)
        doc = stage / "usr/share/doc/mhm"
        doc.mkdir(parents=True)
        shutil.copy2(ROOT / "LICENSE", doc / "copyright")
        shutil.copy2(ROOT / "README.md", doc / "README.md")
        with urllib.request.urlopen(
            "https://raw.githubusercontent.com/MetaCubeX/mihomo/v"
            + runtime.probe_mihomo(lib / "mihomo")["version"]
            + "/LICENSE",
            timeout=30,
        ) as response:
            (doc / "mihomo.LICENSE").write_bytes(response.read())
        (doc / "third-party.txt").write_text(
            "mihomo is distributed under its included license.\nCorresponding source: https://github.com/MetaCubeX/mihomo\nThe installed binary identifies its exact release with /usr/lib/mhm/mihomo -v.\n"
        )
        control = stage / "DEBIAN"
        control.mkdir()
        size = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024
        (control / "control").write_text(f"""Package: mhm
Version: {version}
Section: net
Priority: optional
Architecture: {architecture}
Maintainer: mhm maintainers <maintainers@mhm.invalid>
Depends: python3 (>= 3.9), libc6 (>= 2.36), libgcc-s1, zlib1g, libsecret-tools, ca-certificates
Recommends: gnome-keyring
Installed-Size: {size}
Description: Browser proxy profiles and subscriptions powered by mihomo
 Native Messaging companion for Chrome, Edge, Firefox and Opera.
 Profiles and credentials are encrypted with a key in Secret Service.
 Run mhm-setup install as the desktop user after installing the extension.
""")
        for directory in stage.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        target = output / f"mhm_{version}_{architecture}.deb"
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(stage), str(target)],
            check=True,
        )
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--architecture", choices=["amd64", "arm64"])
    parser.add_argument("--mihomo", type=Path)
    args = parser.parse_args()
    print(build(args.output, args.architecture, args.mihomo))

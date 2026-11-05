#!/usr/bin/env python3
"""Create APT metadata; optional signing uses an existing GPG key."""

from pathlib import Path
import argparse
import gzip
import hashlib
import shutil
import subprocess
import datetime as dt


def build(packages, output, signing_key=None):
    output = Path(output).resolve()
    pool = output / "pool/main/m/mhm"
    pool.mkdir(parents=True, exist_ok=True)
    architectures = set()
    for package in packages:
        package = Path(package).resolve()
        architecture = subprocess.check_output(
            ["dpkg-deb", "-f", str(package), "Architecture"], text=True
        ).strip()
        architectures.add(architecture)
        shutil.copy2(package, pool / package.name)
    suite = output / "dists/stable"
    hashes = []
    for architecture in sorted(architectures):
        directory = suite / f"main/binary-{architecture}"
        directory.mkdir(parents=True, exist_ok=True)
        data = subprocess.check_output(
            ["dpkg-scanpackages", "--arch", architecture, "--multiversion", "pool"],
            cwd=output,
        )
        for name, content in [
            ("Packages", data),
            ("Packages.gz", gzip.compress(data, mtime=0)),
        ]:
            path = directory / name
            path.write_bytes(content)
            hashes.append(
                f" {hashlib.sha256(content).hexdigest()} {len(content)} {path.relative_to(suite).as_posix()}"
            )
    release = suite / "Release"
    release.write_text(
        "Origin: mhm\nLabel: mhm\nSuite: stable\nCodename: stable\nComponents: main\nArchitectures: "
        + " ".join(sorted(architectures))
        + "\nDate: "
        + dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        + "\nSHA256:\n"
        + "\n".join(hashes)
        + "\n"
    )
    if signing_key:
        for filename, flags in [
            ("InRelease", ["--clearsign"]),
            ("Release.gpg", ["--armor", "--detach-sign"]),
        ]:
            subprocess.run(
                [
                    "gpg",
                    "--batch",
                    "--yes",
                    "--local-user",
                    signing_key,
                    "--output",
                    str(suite / filename),
                    *flags,
                    str(release),
                ],
                check=True,
            )
        with (output / "mhm-archive-keyring.gpg").open("wb") as key:
            subprocess.run(
                ["gpg", "--batch", "--export", signing_key], stdout=key, check=True
            )
    for path in output.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("packages", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, default=Path("dist/apt"))
    parser.add_argument("--signing-key")
    args = parser.parse_args()
    print(build(args.packages, args.output, args.signing_key))

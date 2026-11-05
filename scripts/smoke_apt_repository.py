#!/usr/bin/env python3
"""Verify signed repository metadata with an isolated, disposable GPG key."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from build_apt_repository import build

if os.environ.get("MHM_CONTAINER_TEST") != "1":
    raise SystemExit("Run in the disposable Linux build container.")
with tempfile.TemporaryDirectory(prefix="mhm-apt-test-") as temporary:
    root = Path(temporary)
    root.chmod(0o755)
    key_home = root / "gnupg"
    key_home.mkdir(mode=0o700)
    environment = {**os.environ, "GNUPGHOME": str(key_home)}
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            "mhm Test <test@mhm.invalid>",
            "ed25519",
            "sign",
            "1d",
        ],
        env=environment,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    previous = os.environ.get("GNUPGHOME")
    os.environ["GNUPGHOME"] = str(key_home)
    try:
        repository = build(
            sorted(Path("/out").glob("*.deb")), root / "repo", "test@mhm.invalid"
        )
    finally:
        if previous is None:
            os.environ.pop("GNUPGHOME", None)
        else:
            os.environ["GNUPGHOME"] = previous
    sources = root / "sources.list"
    sources.write_text(
        f"deb [signed-by={repository}/mhm-archive-keyring.gpg] file:{repository} stable main\n"
    )
    lists = root / "lists"
    lists.mkdir()
    (lists / "partial").mkdir()
    options = [
        "-o",
        f"Dir::Etc::sourcelist={sources}",
        "-o",
        "Dir::Etc::sourceparts=-",
        "-o",
        f"Dir::State::lists={lists}",
        "-o",
        "APT::Get::List-Cleanup=0",
    ]
    subprocess.run(["apt-get", *options, "update"], check=True)
    subprocess.run(
        ["apt-get", *options, "install", "-y", "mhm"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(["mhm-setup", "--version"], check=True)
    subprocess.run(
        ["apt-get", "remove", "-y", "-qq", "mhm"], check=True, stdout=subprocess.DEVNULL
    )
print(
    "Signed APT repository PASS: InRelease verification and install without trusted=yes"
)

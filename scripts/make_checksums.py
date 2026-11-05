#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def main() -> int:
    artifacts = sorted(
        path for path in DIST.iterdir() if path.is_file() and path.name != "SHA256SUMS"
    )
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in artifacts
    ]
    (DIST / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote checksums for {len(artifacts)} artifact(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

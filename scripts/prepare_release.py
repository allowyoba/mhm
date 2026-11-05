#!/usr/bin/env python3
"""Collect platform artifacts without replacing conflicting files."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for path in sorted(args.source.rglob('*')):
        if not path.is_file() or path.name == 'SHA256SUMS' or 'apt' in path.relative_to(args.source).parts:
            continue
        if not path.name.startswith('mhm'):
            continue
        target = args.output / path.name
        if target.exists() and target.read_bytes() != path.read_bytes():
            raise RuntimeError(f'conflicting artifact: {path.name}')
        shutil.copy2(path, target)
    repositories = sorted(args.source.glob('mhm-linux-*/apt'))
    for repository in repositories:
        target = args.output / f'{repository.parent.name}-apt.tar.gz'
        with tarfile.open(target, 'w:gz') as archive:
            archive.add(repository, arcname='apt')
    files = sorted(p for p in args.output.iterdir() if p.is_file() and p.name != 'SHA256SUMS')
    if not files:
        raise RuntimeError('no release artifacts found')
    (args.output / 'SHA256SUMS').write_text(''.join(
        f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in files
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

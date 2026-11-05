#!/usr/bin/env python3
"""Submit the Firefox package to Mozilla Add-ons."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'mozilla')
    args = parser.parse_args()
    env = os.environ.copy()
    if args.env_file:
        for line in args.env_file.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                env.setdefault(key.strip(), value.strip().strip('\"\''))
    for key in ('AMO_JWT_ISSUER', 'AMO_JWT_SECRET'):
        if not env.get(key):
            parser.error(f'{key} is required')
    env['WEB_EXT_API_KEY'] = env['AMO_JWT_ISSUER']
    env['WEB_EXT_API_SECRET'] = env['AMO_JWT_SECRET']
    from build_browser_packages import build, VERSION
    with tempfile.TemporaryDirectory(prefix='mhm-amo-') as temp:
        stage = Path(temp)
        archive = build('firefox', stage, VERSION)
        source = stage / 'source'
        with zipfile.ZipFile(archive) as package:
            package.extractall(source)
        args.output.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            'npx', '--yes', 'web-ext@10.6.0', 'sign', '--channel', 'listed',
            '--source-dir', str(source), '--artifacts-dir', str(args.output.resolve()),
            '--amo-metadata', str(ROOT / 'docs' / 'amo-metadata.json'),
            '--approval-timeout', '0', '--no-input', '--no-config-discovery',
        ], env=env, check=True, cwd=ROOT)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

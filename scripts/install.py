#!/usr/bin/env python3
"""Validate installation inputs before building a missing native-host payload."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from setup import main as cli, runtime


def payload_is_current() -> bool:
    archive = ROOT / ".build" / "setup" / "native_host.zip"
    if not archive.is_file():
        return False
    inputs = [
        ROOT / "native-host" / name
        for name in (
            "native_host.py",
            "requirements-build.txt",
            "requirements-runtime.txt",
        )
    ]
    inputs.extend((ROOT / "native-host" / "mhm").glob("*.py"))
    inputs.append(ROOT / "scripts" / "build_setup.py")
    return all(path.stat().st_mtime_ns <= archive.stat().st_mtime_ns for path in inputs)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = runtime.build_parser().parse_args(arguments)
    if args.inspect or args.diagnose or args.repair or args.uninstall:
        return cli.main(["install", *arguments])
    try:
        identity, url = runtime.resolve_extension_id(
            args.extension_id,
            args.extension_url,
            args.extension_manifest,
            interactive=False,
            browser=args.browser,
        )
        if args.mihomo and not runtime.probe_mihomo(args.mihomo.expanduser()):
            raise runtime.SetupError(
                "--mihomo must point to a working mihomo executable."
            )
    except runtime.SetupError as error:
        print(f"Installation preflight failed: {error}", file=sys.stderr)
        return 1
    if not args.native_host and not payload_is_current():
        print("Building native-host payload before installation…", flush=True)
        try:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "build_setup.py")],
                cwd=ROOT,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            return error.returncode
    arguments += ["--extension-id", identity]
    if url:
        arguments += ["--extension-url", url]
    if "--extension-install" not in arguments and not any(
        x.startswith("--extension-install=") for x in arguments
    ):
        arguments += ["--extension-install", "skip"]
    return cli.main(["install", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())

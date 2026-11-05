#!/usr/bin/env python3
"""Command-line entry point for installation and maintenance."""

from __future__ import annotations
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setup import runtime


def cli_args(args: list[str]) -> list[str]:
    commands = {
        "install": None,
        "diagnose": "--diagnose",
        "repair": "--repair",
        "uninstall": "--uninstall",
        "inspect": "--inspect",
    }
    if args and args[0] in commands:
        flag = commands[args[0]]
        return ([flag] if flag else []) + args[1:]
    return args


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(runtime.PRODUCT_VERSION)
        return 0
    if not args or args in (["--help"], ["-h"]):
        print(
            "mhm CLI · install / diagnose / repair / uninstall / inspect\n"
            "Example: mhm-setup install --browser chrome --extension-id ID --extension-install skip\n"
        )
        args = ["--help"]
    else:
        args = cli_args(args)
        if "--non-interactive" not in args:
            args.append("--non-interactive")
    return runtime.main(args)


if __name__ == "__main__":
    raise SystemExit(main())

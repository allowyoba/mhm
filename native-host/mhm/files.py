"""Files domain services."""

from __future__ import annotations
import os
import sys
import tempfile
import contextlib
from pathlib import Path


def app_data_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / "mhm"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mhm"
    root = os.environ.get("XDG_DATA_HOME")
    return Path(root) / "mhm" if root else Path.home() / ".local" / "share" / "mhm"


def ensure_private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


def write_private_text(path: Path, text: str) -> None:
    ensure_private_parent(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)

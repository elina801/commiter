from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CommiterError(Exception):
    """An actionable error safe to show on the terminal."""


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_dir() -> Path:
    if os.environ.get("COMMITER_STATE_DIR"):
        return Path(os.environ["COMMITER_STATE_DIR"])
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "commiter"


def read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CommiterError(f"Cannot read JSON file {path}: {exc}") from None
    if not isinstance(result, dict):
        raise CommiterError(f"{path} must contain a JSON object.")
    return result


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Replace a private JSON file atomically, without following its symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def display(value: str) -> str:
    """Escape filenames and API values that could inject terminal controls."""
    return value.encode("unicode_escape").decode("ascii") if any(ord(c) < 32 or ord(c) == 127 for c in value) else value


def redact(value: str) -> str:
    for key, secret in os.environ.items():
        if ("TOKEN" in key.upper() or "SECRET" in key.upper()) and len(secret) >= 6:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[redacted]", value)
    return value

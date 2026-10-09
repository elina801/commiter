from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterator

from dotenv import dotenv_values

from .common import CommiterError


def find_env_file(repo: Path, explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise CommiterError(f"The --env-file does not exist or is not a file: {path}")
        return path
    for path in (Path.cwd() / ".env", repo / ".env"):
        if path.is_file():
            return path.resolve()
    return None


@contextlib.contextmanager
def token_environment(path: Path | None, names: list[str], *, override: bool = False,
                      state_path: Path | None = None) -> Iterator[None]:
    """Temporarily load configured tokens and restore the caller's environment."""
    values = {}
    if path is not None:
        try:
            with path.open(encoding="utf-8-sig") as stream:
                values = dotenv_values(stream=stream, interpolate=False)
        except (OSError, UnicodeError):
            raise CommiterError(f"Cannot read the .env file: {path}") from None
    added = {}
    for name in names:
        value = values.get(name)
        if (override or name not in os.environ) and value is not None:
            if "\x00" in value:
                raise CommiterError(f"Invalid NUL character in .env variable {name}.")
            added[name] = value
    if state_path is not None:
        added["COMMITER_STATE_DIR"] = str(state_path)
    original = {name: os.environ.get(name) for name in added}
    os.environ.update(added)
    try:
        yield
    except CommiterError as exc:
        # Sanitize before restoring the environment used by the CLI error handler.
        message = str(exc)
        for value in added.values():
            if value:
                message = message.replace(value, "[redacted]")
        raise CommiterError(message) from None
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

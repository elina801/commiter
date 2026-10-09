from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .common import CommiterError, state_dir
from .config import Config, config_from_data
from .git import Repo


@dataclass(frozen=True)
class Settings:
    config: Config
    repo: Repo
    source: str
    split: str
    count: int | None
    lines_per_commit: int
    jitter: float
    seed: int | None
    push: bool
    dry_run: bool
    state_path: Path


def discover_settings() -> Path:
    for directory in (Path.cwd(), *Path.cwd().parents):
        path = directory / ".env"
        if path.is_file():
            return path.resolve()
    raise CommiterError("No .env found in the current directory or its parents. Copy .env.example to .env and fill in your settings.")


def load_settings(path: Path) -> Settings:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except (OSError, UnicodeError):
        raise CommiterError(f"Cannot read the settings file: {path}") from None

    def value(key: str, default: str = "") -> str:
        return (values.get(key) or default).strip()

    def integer(key: str, default: str = "", positive: bool = False) -> int | None:
        raw = value(key, default)
        if not raw:
            return None
        try:
            result = int(raw)
        except ValueError:
            raise CommiterError(f"{key} in .env must be an integer.") from None
        if positive and result <= 0:
            raise CommiterError(f"{key} in .env must be positive.")
        return result

    def boolean(key: str, default: str) -> bool:
        raw = value(key, default).lower()
        if raw not in {"true", "false"}:
            raise CommiterError(f"{key} in .env must be true or false.")
        return raw == "true"

    def local_path(raw: str) -> Path:
        result = Path(raw).expanduser()
        return (result if result.is_absolute() else path.parent / result).resolve()

    known = {"COMMITER_REPO", "COMMITER_GITHUB", "COMMITER_WEIGHTS", "COMMITER_COMMITS",
             "COMMITER_LINES_PER_COMMIT", "COMMITER_JITTER", "COMMITER_SEED", "COMMITER_SOURCE",
             "COMMITER_SPLIT", "COMMITER_PUSH", "COMMITER_DRY_RUN", "COMMITER_STATE_DIR"}
    for key in values:
        if key.startswith("COMMITER_") and key not in known:
            raise CommiterError(f"Unknown .env setting {key}. See .env.example for supported settings.")
    source, split = value("COMMITER_SOURCE", "worktree"), value("COMMITER_SPLIT", "files")
    if source not in {"worktree", "staged"}:
        raise CommiterError("COMMITER_SOURCE in .env must be worktree or staged.")
    if split not in {"files", "hunks"}:
        raise CommiterError("COMMITER_SPLIT in .env must be files or hunks.")
    count = integer("COMMITER_COMMITS", positive=True)
    size = integer("COMMITER_LINES_PER_COMMIT", "200", positive=True)
    seed = integer("COMMITER_SEED")
    push, dry_run = boolean("COMMITER_PUSH", "true"), boolean("COMMITER_DRY_RUN", "false")
    try:
        jitter = float(value("COMMITER_JITTER", "0.15"))
    except ValueError:
        raise CommiterError("COMMITER_JITTER in .env must be a number between 0 and 0.5.") from None
    if not math.isfinite(jitter) or not 0 <= jitter <= 0.5:
        raise CommiterError("COMMITER_JITTER in .env must be a number between 0 and 0.5.")
    try:
        weights = [float(item.strip()) for item in value("COMMITER_WEIGHTS", "40,30,30").split(",")]
    except ValueError:
        raise CommiterError("COMMITER_WEIGHTS in .env must contain three positive numbers separated by commas.") from None
    if len(weights) != 3:
        raise CommiterError("COMMITER_WEIGHTS in .env must contain exactly three weights.")
    accounts = []
    for index, weight in enumerate(weights, 1):
        login = value(f"GITHUB_ACCOUNT_{index}")
        if not login:
            raise CommiterError(f"Set GITHUB_ACCOUNT_{index} in .env to that account's GitHub login.")
        accounts.append({"login": login, "token_env": f"GITHUB_TOKEN_{index}", "weight": weight,
                         "name": value(f"GITHUB_NAME_{index}") or None, "email": value(f"GITHUB_EMAIL_{index}") or None})
        if not dry_run and not value(f"GITHUB_TOKEN_{index}"):
            raise CommiterError(f"Set GITHUB_TOKEN_{index} in .env to the token for account {index}.")
    repo = Repo(local_path(value("COMMITER_REPO", ".")))
    repository = value("COMMITER_GITHUB") or repo.run("remote", "get-url", "origin", check=False).decode().strip()
    if not repository:
        raise CommiterError("Set COMMITER_GITHUB=owner/repository in .env, or configure the target repo's origin remote.")
    config = config_from_data({"version": 1, "repository": repository, "accounts": accounts}, path)
    storage = local_path(value("COMMITER_STATE_DIR")) if value("COMMITER_STATE_DIR") else state_dir().resolve()
    if storage == repo.root:
        raise CommiterError("COMMITER_STATE_DIR must be a separate folder, not the target repository root.")
    return Settings(config, repo, source, split, count, size, jitter, seed, push, dry_run, storage)

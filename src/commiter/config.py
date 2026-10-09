from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .common import CommiterError, read_json, write_json


LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
REPOSITORY = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]+)\Z")


@dataclass(frozen=True)
class Account:
    login: str
    token_env: str
    weight: float
    name: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class Config:
    repository: str
    accounts: tuple[Account, ...]
    path: Path

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repository}.git"


def parse_repository(value: str) -> str:
    value = value.strip()
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif "://" in value:
        url = urlparse(value)
        if url.hostname != "github.com" or url.scheme not in {"https", "ssh"}:
            raise CommiterError("Use an OWNER/REPO or a GitHub.com HTTPS/SSH repository URL.")
        if url.password or (url.scheme == "https" and url.username) or url.query or url.fragment or url.port:
            raise CommiterError("Repository URLs must not contain credentials, ports, queries, or fragments.")
        value = url.path.lstrip("/")
    value = value.rstrip("/").removesuffix(".git")
    match = REPOSITORY.fullmatch(value)
    if not match or match.group(2) in {".", ".."}:
        raise CommiterError("Repository must have the form OWNER/REPO on GitHub.com.")
    return value


def config_path(repo: Path) -> Path:
    key = hashlib.sha256(os.fsencode(str(repo.resolve()))).hexdigest()[:20]
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "commiter/repos" / f"{key}.json"


def validate_identity(value: Any, field: str, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\n\r\x00<>"):
        raise CommiterError(f"Invalid account {field}.")
    if field == "email" and (value.count("@") != 1 or any(c.isspace() for c in value)):
        raise CommiterError("An account email must be a valid commit email address.")
    return value


def load_config(path: Path) -> Config:
    return config_from_data(read_json(path), path)


def config_from_data(data: dict[str, Any], path: Path) -> Config:
    if data.get("version") != 1:
        raise CommiterError("Unsupported configuration version.")
    repository = data.get("repository")
    if not isinstance(repository, str):
        raise CommiterError("The configuration needs a repository.")
    repository = parse_repository(repository)
    rows = data.get("accounts")
    if not isinstance(rows, list) or len(rows) != 3:
        raise CommiterError("Configure exactly three accounts.")
    accounts = []
    for row in rows:
        if not isinstance(row, dict):
            raise CommiterError("Every account must be a JSON object.")
        if "token" in row:
            raise CommiterError("Use token_env, with the token in your environment, instead of storing a token in JSON.")
        login, token_env, weight = row.get("login"), row.get("token_env"), row.get("weight")
        if not isinstance(login, str) or not LOGIN.fullmatch(login):
            raise CommiterError("Invalid GitHub account login.")
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise CommiterError(f"Invalid token environment variable for {login}.")
        if isinstance(weight, bool) or not isinstance(weight, (float, int)) or not math.isfinite(weight) or weight <= 0:
            raise CommiterError(f"The weight for {login} must be positive and finite.")
        accounts.append(Account(login, token_env, float(weight), validate_identity(row.get("name"), "name"), validate_identity(row.get("email"), "email")))
    if len({a.login.lower() for a in accounts}) != 3 or len({a.token_env for a in accounts}) != 3:
        raise CommiterError("Use three distinct logins and three distinct token environment variables.")
    if not math.isfinite(sum(a.weight for a in accounts)):
        raise CommiterError("Account weights are too large.")
    return Config(repository, tuple(accounts), path.resolve())


def save_config(path: Path, repository: str, accounts: list[Account]) -> Config:
    data = {"version": 1, "repository": parse_repository(repository), "accounts": [asdict(a) for a in accounts]}
    with tempfile.TemporaryDirectory(prefix="commiter-config-") as directory:
        temporary = Path(directory) / "config.json"
        write_json(temporary, data)
        load_config(temporary)
    write_json(path, data)
    return load_config(path)

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .common import CommiterError, redact
from .config import Config, validate_identity
from .git import Repo


@dataclass(frozen=True)
class Identity:
    login: str
    name: str
    email: str
    token: str = field(repr=False)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_get(endpoint: str, token: str):
    request = urllib.request.Request(
        "https://api.github.com" + endpoint,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "commiter-cli/0.1.0"},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        reasons = {401: "invalid or expired token", 403: "permission, organization SSO, or rate limit restriction",
                   404: "repository unavailable to this token", 429: "rate limit reached"}
        raise CommiterError(f"GitHub {endpoint}: HTTP {exc.code} ({reasons.get(exc.code, 'request failed')}).") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise CommiterError(f"GitHub request failed: {redact(str(exc))}") from None


def identities(config: Config) -> dict[str, Identity]:
    result = {}
    seen_ids = set()
    for account in config.accounts:
        token = os.environ.get(account.token_env, "").strip()
        if not token:
            raise CommiterError(f"Set {account.token_env} in .env (or your shell) to the token for {account.login}.")
        if any(c.isspace() for c in token):
            raise CommiterError(f"{account.token_env} contains whitespace; provide a valid personal access token.")
        user = api_get("/user", token)
        if not isinstance(user, dict) or str(user.get("login", "")).lower() != account.login.lower():
            raise CommiterError(f"{account.token_env} does not belong to {account.login}.")
        user_id = user.get("id")
        if not isinstance(user_id, int) or user_id <= 0 or user_id in seen_ids:
            raise CommiterError("The tokens must authenticate three distinct GitHub users.")
        seen_ids.add(user_id)
        details = api_get(f"/repos/{config.repository}", token)
        if not isinstance(details, dict) or not details.get("permissions", {}).get("push"):
            raise CommiterError(f"{account.login} does not have push access to {config.repository}.")
        if details.get("archived") or details.get("disabled"):
            raise CommiterError(f"{config.repository} is archived or disabled.")
        if str(details.get("full_name", "")).lower() != config.repository.lower():
            raise CommiterError("GitHub returned a different repository. Update the configuration after a rename.")
        login = user["login"]
        email = account.email or f"{user_id}+{login}@users.noreply.github.com"
        if account.email and email.lower() not in {
            f"{user_id}+{login}@users.noreply.github.com".lower(), f"{login}@users.noreply.github.com".lower()
        }:
            emails = api_get("/user/emails?per_page=100", token)
            if not isinstance(emails, list) or not any(
                isinstance(row, dict) and str(row.get("email", "")).lower() == email.lower() and row.get("verified") for row in emails
            ):
                raise CommiterError(f"The configured email for {account.login} is not verified on that account.")
        name = account.name or user.get("name") or login
        validate_identity(name, "name", optional=False)
        validate_identity(email, "email", optional=False)
        result[account.login] = Identity(login, name, email, token)
    return result


def authenticated_git(repo: Repo, config: Config, identity: Identity, *args: str) -> bytes:
    """Pass credentials through an ephemeral askpass environment, never argv/URL."""
    with tempfile.TemporaryDirectory(prefix="commiter-askpass-") as directory:
        helper = Path(directory) / "askpass"
        # A shell wrapper works even when the Python interpreter path contains spaces.
        helper.write_text(
            "#!/bin/sh\nexec \"$COMMITER_PYTHON\" -c 'import os,sys; "
            "print(os.environ[\"COMMITER_PUSH_LOGIN\"] if \"username\" in sys.argv[1].lower() "
            "else os.environ[\"COMMITER_PUSH_TOKEN\"])' \"$1\"\n", encoding="utf-8",
        )
        helper.chmod(0o700)
        env = {"GIT_ASKPASS": str(helper), "COMMITER_PYTHON": sys.executable,
               "COMMITER_PUSH_LOGIN": identity.login, "COMMITER_PUSH_TOKEN": identity.token}
        try:
            return repo.run(
                "-c", "credential.helper=", "-c", "credential.useHttpPath=true",
                "-c", "http.extraHeader=", "-c", "http.followRedirects=false",
                *args, env=env, timeout=120,
            )
        except CommiterError as exc:
            raise CommiterError(str(exc).replace(identity.token, "[redacted]")) from None

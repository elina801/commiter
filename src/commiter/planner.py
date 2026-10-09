from __future__ import annotations

import hashlib
import math
import os
import random
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import CommiterError, state_dir, timestamp, write_json
from .config import Config
from .git import Repo, excluded_paths


@dataclass(frozen=True)
class Unit:
    id: str
    path: str
    kind: str
    hunk: int | None
    lines: int
    patch: bytes
    mode: str
    oid: str

    def metadata(self) -> dict[str, Any]:
        return {"id": self.id, "path": self.path, "kind": self.kind, "hunk": self.hunk,
                "lines": self.lines, "patch_sha256": hashlib.sha256(self.patch).hexdigest()}


def units_for(repo: Repo, base: str, target: str, split: str) -> list[Unit]:
    stats = {}
    for row in repo.diff(base, target, None, "--numstat", "-z").split(b"\0"):
        if row:
            added, deleted, path = row.split(b"\t", 2)
            stats[path] = (0 if added == b"-" else int(added)) + (0 if deleted == b"-" else int(deleted))
    raw = repo.diff(base, target, None, "--raw", "--no-abbrev", "-z").split(b"\0")
    changes = []
    for index in range(0, len(raw) - 1, 2):
        old_mode, mode, _, oid, status = raw[index].lstrip(b":").split()
        if b"160000" in {old_mode, mode}:
            raise CommiterError("Submodule changes cannot be split by this version.")
        changes.append((status, raw[index + 1], old_mode, mode, oid))
    # Deletions first handle directory-to-file and file-to-directory replacements.
    changes.sort(key=lambda row: (row[0] != b"D", row[1]))
    units = []
    for status, encoded_path, old_mode, mode, oid in changes:
        path = os.fsdecode(encoded_path)
        patch = repo.diff(base, target, path, "--binary", "--full-index", "--unified=0",
                          "--diff-algorithm=myers", "--no-indent-heuristic")
        starts = [match.start() for match in re.finditer(rb"^@@ ", patch, re.MULTILINE)]
        can_split = (split == "hunks" and status == b"M" and old_mode == mode
                     and mode in {b"100644", b"100755"} and len(starts) > 1)
        if can_split:
            header = patch[:starts[0]]
            # Lower changes first: their edits leave the original upper line numbers valid.
            for hunk in reversed(range(len(starts))):
                body = patch[starts[hunk]:starts[hunk + 1] if hunk + 1 < len(starts) else len(patch)]
                lines = sum(line.startswith((b"+", b"-")) for line in body.splitlines())
                units.append(Unit(f"u{len(units) + 1:05d}", path, "hunk", hunk, max(1, lines),
                                  header + body, mode.decode(), oid.decode()))
        else:
            units.append(Unit(f"u{len(units) + 1:05d}", path, "file", None, max(1, stats[encoded_path]),
                              patch, mode.decode(), oid.decode()))
    return units


def partition(units: list[Unit], count: int) -> list[list[Unit]]:
    """Keep unit order and approximate equally sized commits without cutting units."""
    groups = []
    start = 0
    remaining_lines = sum(unit.lines for unit in units)
    for position in range(count):
        remaining_groups = count - position
        if remaining_groups == 1:
            groups.append(units[start:])
            break
        target = remaining_lines / remaining_groups
        end = start + 1
        size = units[start].lines
        limit = len(units) - (remaining_groups - 1)
        while end < limit and abs(size + units[end].lines - target) <= abs(size - target):
            size += units[end].lines
            end += 1
        groups.append(units[start:end])
        start = end
        remaining_lines -= size
    return groups


def allocate(count: int, weights: list[float], jitter: float, rng: random.Random) -> tuple[list[int], list[float]]:
    # Normalize before multiplying so extreme but finite weights do not overflow.
    scale = max(weights)
    varied = [(weight / scale) * rng.uniform(1 - jitter, 1 + jitter) for weight in weights]
    total = sum(varied)
    proportions = [value / total for value in varied]
    quotas = [count * value for value in proportions]
    counts = [math.floor(value) for value in quotas]
    rank = sorted(range(3), key=lambda i: (quotas[i] - counts[i], rng.random()), reverse=True)
    for index in rank[:count - sum(counts)]:
        counts[index] += 1
    if count >= 3:
        for index in range(3):
            if counts[index] == 0:
                donor = max(range(3), key=lambda i: counts[i])
                counts[donor] -= 1
                counts[index] += 1
    return counts, proportions


def apply_unit(repo: Repo, unit: Unit, env: dict[str, str]) -> None:
    if unit.kind == "hunk":
        repo.run("apply", "--cached", "--unidiff-zero", "--whitespace=nowarn", input=unit.patch, env=env)
    elif unit.mode == "000000":
        repo.run("update-index", "--force-remove", "--", unit.path, env=env)
    else:
        repo.run("update-index", "--add", "--cacheinfo", unit.mode, unit.oid, unit.path, env=env)


def create_plan(repo: Repo, config: Config, *, source: str = "worktree", split: str = "files",
                count: int | None = None, lines_per_commit: int = 200, jitter: float = 0.15,
                seed: int | None = None, message: str = "Update", output: Path | None = None,
                env_files: list[Path] | None = None, account_messages: list[str] | None = None) -> tuple[dict, Path]:
    repo.ready()
    if source not in {"staged", "worktree"} or split not in {"files", "hunks"}:
        raise CommiterError("Invalid change source or split mode.")
    if count is not None and (isinstance(count, bool) or count < 1):
        raise CommiterError("COMMITER_COMMITS must be a positive integer.")
    if lines_per_commit < 1 or not math.isfinite(jitter) or not 0 <= jitter <= 0.5:
        raise CommiterError("COMMITER_LINES_PER_COMMIT must be positive; COMMITER_JITTER must be between 0 and 0.5.")
    if not message.strip() or "\x00" in message:
        raise CommiterError("The commit message cannot be empty or contain a NUL character.")
    if account_messages is not None and (len(account_messages) != 3 or any(
        not isinstance(text, str) or not text.strip() or "\x00" in text for text in account_messages
    )):
        raise CommiterError("Provide exactly three nonempty commit messages, one per account.")
    plan_id = uuid.uuid4().hex
    output = (output or state_dir() / "plans" / f"{plan_id}.json").expanduser().resolve()
    if output.exists():
        raise CommiterError(f"The plan file already exists: {output}. Choose a new --output path.")
    credentials = excluded_paths(repo, [repo.root / ".env", Path.cwd() / ".env", *(env_files or [])])
    excluded = sorted(set(excluded_paths(repo, [config.path, output, state_dir()]) + credentials))
    original_head, branch, index_tree = repo.head(), repo.branch(), repo.index_tree()
    base = original_head or repo.empty_tree()
    target = repo.snapshot(source, excluded)
    reject_env_changes(repo, base, target, credentials)
    units = units_for(repo, base, target, split)
    if not units:
        raise CommiterError("There are no changes to commit for the selected source.")
    if count is None:
        count = min(len(units), max(3, math.ceil(sum(unit.lines for unit in units) / lines_per_commit)))
    if count > len(units):
        raise CommiterError(f"Requested {count} commits, but there are only {len(units)} indivisible change units. "
                            "Reduce COMMITER_COMMITS in .env or set COMMITER_SPLIT=hunks. New files, binary files, and single hunks stay whole.")
    groups = partition(units, count)
    seed = seed if seed is not None else secrets.randbits(64)
    rng = random.Random(seed)
    counts, varied = allocate(count, [account.weight for account in config.accounts], jitter, rng)
    schedule = [account.login for account, amount in zip(config.accounts, counts) for _ in range(amount)]
    rng.shuffle(schedule)
    commits = []
    messages_by_account = dict(zip([account.login for account in config.accounts], account_messages or []))
    for index, (group, login) in enumerate(zip(groups, schedule)):
        paths = list(dict.fromkeys(unit.path for unit in group))
        label = paths[0] if len(paths) == 1 else f"{len(paths)} files"
        commits.append({"account": login, "units": [unit.id for unit in group],
                        "message": messages_by_account.get(login, f"{message.strip()}: {label} ({index + 1}/{count})")})
    plan = {"version": 1, "kind": "plan", "id": plan_id, "created_at": timestamp(), "repo": str(repo.root),
            "repository": config.repository, "branch": branch, "head": original_head, "base_tree": base,
            "index_tree": index_tree, "target_tree": target, "source": source, "split": split,
            "excluded": excluded, "credential_files": credentials, "seed": seed, "jitter": jitter,
            "accounts": [{"login": account.login, "weight": account.weight, "varied_share": proportion}
                         for account, proportion in zip(config.accounts, varied)],
            "units": [unit.metadata() for unit in units], "commits": commits}
    # Validate every intermediate index before saving a usable plan.
    with repo.temporary_index(base) as (env, _):
        for group in groups:
            for unit in group:
                apply_unit(repo, unit, env)
            repo.index_tree(env)
        if repo.index_tree(env) != target:
            raise CommiterError("The generated split does not reproduce the complete change. No plan was saved.")
    if repo.head() != original_head or repo.branch() != branch or repo.index_tree() != index_tree or repo.snapshot(source, excluded) != target:
        raise CommiterError("The repository changed while planning. Run plan again.")
    repo.run("update-ref", f"refs/commiter/plans/{plan_id}", target)
    write_json(output, plan)
    return plan, output


def reject_env_changes(repo: Repo, base: str, target: str, paths: list[str]) -> None:
    for path in paths:
        if repo.diff(base, target, path, "--name-only", "-z"):
            raise CommiterError(f"The selected changes include a token .env file ({path}). "
                                "Unstage it before planning; credential files cannot be distributed.")


def validate_plan(repo: Repo, config: Config, plan: dict) -> tuple[list[Unit], list[dict]]:
    if plan.get("version") != 1 or plan.get("kind") != "plan":
        raise CommiterError("This is not a supported Commiter plan.")
    if plan.get("repo") != str(repo.root) or plan.get("repository") != config.repository:
        raise CommiterError("The plan belongs to a different local or GitHub repository.")
    accounts = plan.get("accounts")
    if not isinstance(accounts, list) or [row.get("login") for row in accounts if isinstance(row, dict)] != [a.login for a in config.accounts]:
        raise CommiterError("The configured accounts changed. Create a new plan.")
    if plan.get("source") not in {"staged", "worktree"} or plan.get("split") not in {"files", "hunks"}:
        raise CommiterError("Invalid plan source or split mode.")
    for field in ("base_tree", "index_tree", "target_tree"):
        if not isinstance(plan.get(field), str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", plan[field]):
            raise CommiterError(f"Invalid plan {field}.")
    if plan.get("head") is not None and (not isinstance(plan["head"], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", plan["head"])):
        raise CommiterError("Invalid plan head.")
    expected_base = plan["head"] or repo.empty_tree()
    if expected_base != plan["base_tree"]:
        raise CommiterError("The plan base does not match its original HEAD.")
    excluded = plan.get("excluded")
    if not isinstance(excluded, list) or not all(isinstance(path, str) and path and not Path(path).is_absolute() and ".." not in Path(path).parts for path in excluded):
        raise CommiterError("Invalid plan exclusions.")
    credentials = plan.get("credential_files", [])
    if not isinstance(credentials, list) or not all(isinstance(path, str) and path in excluded for path in credentials):
        raise CommiterError("Invalid plan credential file exclusions.")
    reject_env_changes(repo, plan["base_tree"], plan["target_tree"], sorted(set([".env", *credentials])))
    units = units_for(repo, plan["base_tree"], plan["target_tree"], plan["split"])
    if not units or [unit.metadata() for unit in units] != plan.get("units"):
        raise CommiterError("The plan's change units do not match its Git snapshot. Create a new plan.")
    commits = plan.get("commits")
    if not isinstance(commits, list) or not commits:
        raise CommiterError("The plan has no commits.")
    flattened = []
    logins = {a.login for a in config.accounts}
    for commit in commits:
        if not isinstance(commit, dict) or commit.get("account") not in logins:
            raise CommiterError("A planned commit has an invalid account.")
        ids = commit.get("units")
        message = commit.get("message")
        if not isinstance(ids, list) or not ids or not all(isinstance(unit_id, str) for unit_id in ids):
            raise CommiterError("Each planned commit must contain change units.")
        if not isinstance(message, str) or not message.strip() or "\x00" in message:
            raise CommiterError("Each planned commit needs a valid message.")
        flattened.extend(ids)
    if flattened != [unit.id for unit in units]:
        raise CommiterError("The plan must include each change unit exactly once, in its original order.")
    return units, commits

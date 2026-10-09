from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Callable

from .common import CommiterError, state_dir, timestamp, write_json
from .config import Config
from .git import Repo
from .github import Identity, authenticated_git, identities
from .planner import apply_unit, validate_plan


def assert_fresh(repo: Repo, plan: dict) -> None:
    repo.ready()
    if repo.head() != plan["head"] or repo.branch() != plan.get("branch"):
        raise CommiterError("HEAD or the checked-out branch changed after planning. Create a new plan.")
    if repo.index_tree() != plan["index_tree"]:
        raise CommiterError("The staged changes changed after planning. Create a new plan.")
    if repo.snapshot(plan["source"], plan["excluded"]) != plan["target_tree"]:
        raise CommiterError("The selected changes changed after planning. Create a new plan.")


def apply_plan(repo: Repo, config: Config, plan: dict, *,
               resolved: dict[str, Identity] | None = None) -> tuple[dict, Path]:
    units, commits = validate_plan(repo, config, plan)
    assert_fresh(repo, plan)
    resolved = resolved if resolved is not None else identities(config)
    by_id = {unit.id: unit for unit in units}
    receipt_id = uuid.uuid4().hex
    receipt_path = state_dir() / "receipts" / f"{receipt_id}.json"
    receipt = {"version": 1, "kind": "receipt", "id": receipt_id, "plan_id": plan.get("id"),
               "created_at": timestamp(), "repo": str(repo.root), "repository": config.repository,
               "branch": plan["branch"], "base_head": plan["head"], "target_tree": plan["target_tree"],
               "status": "prepared", "commits": [], "pushed": []}
    # Build the complete commit chain privately before publishing any local ref.
    with repo.temporary_index(plan["base_tree"]) as (env, private_index):
        parent = plan["head"]
        for commit in commits:
            for unit_id in commit["units"]:
                apply_unit(repo, by_id[unit_id], env)
            tree = repo.index_tree(env)
            if parent and tree == repo.run("rev-parse", f"{parent}^{{tree}}").decode().strip():
                raise CommiterError("A planned commit has no actual changes. No local branch was updated.")
            identity = resolved[commit["account"]]
            author_env = {**env, "GIT_AUTHOR_NAME": identity.name, "GIT_AUTHOR_EMAIL": identity.email,
                          "GIT_COMMITTER_NAME": identity.name, "GIT_COMMITTER_EMAIL": identity.email}
            args = ["-c", "commit.gpgsign=false", "commit-tree", tree]
            if parent:
                args += ["-p", parent]
            oid = repo.run(*args, input=(commit["message"] + "\n").encode("utf-8", "surrogateescape"), env=author_env).decode().strip()
            receipt["commits"].append({"oid": oid, "account": commit["account"], "name": identity.name,
                                       "email": identity.email, "message": commit["message"]})
            parent = oid
        if repo.index_tree(env) != plan["target_tree"]:
            raise CommiterError("The final commit does not match the planned changes. No branch was updated.")
        if plan["source"] == "worktree":
            # Excluded configuration/state files may already have staged edits.
            # Keep those edits staged rather than replacing them with HEAD's copy.
            for path in plan["excluded"]:
                pathspec = f":(top,literal){path}"
                in_final = repo.run("ls-files", "-z", "--", pathspec, env=env)
                in_original = repo.run("ls-tree", "-r", "--name-only", "-z", plan["index_tree"], "--", path,
                                       env={"GIT_LITERAL_PATHSPECS": "1"})
                if in_final or in_original:
                    repo.run("restore", "--staged", f"--source={plan['index_tree']}", "--", pathspec, env=env)
        assert_fresh(repo, plan)
        index_path = repo.git_path("index")
        lock_path = index_path.with_name(index_path.name + ".lock")
        try:
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise CommiterError(f"Git's index is locked at {lock_path}. Finish the other Git operation and retry.") from None
        published = False
        try:
            with os.fdopen(lock_fd, "wb") as stream:
                stream.write(private_index.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            assert_fresh(repo, plan)
            write_json(receipt_path, receipt)
            repo.run("update-ref", "-m", "commiter: distribute pending changes", plan["branch"], parent,
                     plan["head"] or "0" * len(parent))
            published = True
            os.replace(lock_path, index_path)
            receipt["status"] = "applied"
            write_json(receipt_path, receipt)
        except BaseException:
            if published:
                # Preserve the prepared index if installing it failed, for manual recovery.
                raise CommiterError(f"The commits were created, but final bookkeeping was interrupted. "
                                    f"Receipt: {receipt_path}. HEAD should be {parent}. "
                                    "Inspect Git status before continuing; do not apply the plan again.") from None
            raise
        finally:
            if not published and lock_path.exists():
                lock_path.unlink()
    return receipt, receipt_path


def validate_receipt(repo: Repo, config: Config, receipt: dict) -> list[dict]:
    if receipt.get("version") != 1 or receipt.get("kind") != "receipt" or receipt.get("status") != "applied":
        raise CommiterError("Use a successfully applied Commiter receipt for pushing.")
    if receipt.get("repo") != str(repo.root) or receipt.get("repository") != config.repository:
        raise CommiterError("The receipt belongs to a different local or GitHub repository.")
    branch = receipt.get("branch")
    if not isinstance(branch, str) or not branch.startswith("refs/heads/"):
        raise CommiterError("Invalid receipt branch.")
    repo.run("check-ref-format", branch)
    commits = receipt.get("commits")
    if not isinstance(commits, list) or not commits:
        raise CommiterError("The receipt has no commits.")
    parent = receipt.get("base_head")
    if parent is not None and (not isinstance(parent, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", parent)):
        raise CommiterError("Invalid receipt base commit.")
    logins = {account.login for account in config.accounts}
    for commit in commits:
        if not isinstance(commit, dict) or commit.get("account") not in logins or not isinstance(commit.get("oid"), str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit["oid"]):
            raise CommiterError("Invalid receipt commit or account.")
        actual = repo.run("show", "-s", "--format=%P%x00%an%x00%ae%x00%cn%x00%ce", commit["oid"]).decode("utf-8", "surrogateescape").rstrip("\n").split("\0")
        if actual != [parent or "", commit.get("name"), commit.get("email"), commit.get("name"), commit.get("email")]:
            raise CommiterError("The receipt does not match the local commit chain and identities.")
        parent = commit["oid"]
    target = repo.run("rev-parse", f"{parent}^{{tree}}").decode().strip()
    if target != receipt.get("target_tree"):
        raise CommiterError("The receipt's final tree does not match its commits.")
    return commits


def push_receipt(repo: Repo, config: Config, receipt: dict, receipt_path: Path, *,
                 resolved: dict[str, Identity] | None = None,
                 progress: Callable[[str], None] = print, resume_command: str | None = None) -> None:
    commits = validate_receipt(repo, config, receipt)
    resolved = resolved if resolved is not None else identities(config)
    # Confirm each recorded identity still belongs to its authenticated account.
    for commit in commits:
        if resolved[commit["account"]].email.lower() != commit["email"].lower():
            raise CommiterError(f"The commit email for {commit['account']} differs from its current configuration.")
    probe = resolved[commits[0]["account"]]
    remote = authenticated_git(repo, config, probe, "ls-remote", "--heads", config.url, receipt["branch"]).splitlines()
    remote_head = remote[0].split(b"\t")[0].decode() if remote else None
    oids = [commit["oid"] for commit in commits]
    start = 0
    if remote_head in oids:
        start = oids.index(remote_head) + 1
    elif remote_head:
        base = receipt.get("base_head")
        if not base or not repo.ancestor(remote_head, base):
            raise CommiterError("The remote branch has changes outside this receipt's history. "
                                "Fetch and reconcile the branch before pushing. No force push was attempted.")
    receipt["pushed"] = oids[:start]
    write_json(receipt_path, receipt)
    if start == len(commits):
        progress("All receipt commits are already on the remote branch.")
        return
    for index in range(start, len(commits)):
        commit = commits[index]
        progress(f"Pushing {index + 1}/{len(commits)} as {commit['account']} ({commit['oid'][:10]})...")
        try:
            authenticated_git(repo, config, resolved[commit["account"]], "push", "--porcelain", "--no-follow-tags",
                              config.url, f"{commit['oid']}:{receipt['branch']}")
            confirmed = authenticated_git(repo, config, resolved[commit["account"]], "ls-remote", "--heads", config.url, receipt["branch"]).splitlines()
            if not confirmed or confirmed[0].split(b"\t")[0].decode() != commit["oid"]:
                raise CommiterError("The remote branch moved during publishing. Re-run push to inspect its current position.")
        except CommiterError as exc:
            suggestion = resume_command or f"commiter push {receipt_path}"
            raise CommiterError(f"{exc}\nLocal commits are saved. Resume with: {suggestion}") from None
        receipt["pushed"] = oids[:index + 1]
        write_json(receipt_path, receipt)
    receipt["pushed_at"] = timestamp()
    write_json(receipt_path, receipt)

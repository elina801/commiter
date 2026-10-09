from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

from .common import CommiterError, redact


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    if extra:
        env.update(extra)
    return env


class Repo:
    def __init__(self, path: Path):
        if not path.is_dir():
            raise CommiterError(f"Repository directory does not exist: {path}")
        self.root = path.resolve()
        try:
            self.root = Path(os.fsdecode(self.run("rev-parse", "--show-toplevel").rstrip(b"\n"))).resolve()
        except CommiterError:
            raise CommiterError(f"{path} must be an existing Git working directory. Clone your target repo first.") from None

    def run(self, *args: str, input: bytes | None = None, env: dict[str, str] | None = None,
            check: bool = True, timeout: int = 120) -> bytes:
        command = ["git", "-c", "core.quotePath=true", "-c", "color.ui=false", "-c", "diff.external=", *args]
        try:
            result = subprocess.run(command, cwd=self.root, env=git_env(env), input=input,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except FileNotFoundError:
            raise CommiterError("Git is not installed or is not on PATH.") from None
        except subprocess.TimeoutExpired:
            raise CommiterError(f"Git {args[0]} timed out after {timeout} seconds.") from None
        if check and result.returncode:
            detail = redact(result.stderr.decode("utf-8", "replace").strip())
            raise CommiterError(f"Git {args[0]} failed: {detail or 'exit status ' + str(result.returncode)}")
        return result.stdout

    def head(self) -> str | None:
        value = self.run("rev-parse", "--verify", "HEAD", check=False).decode().strip()
        return value or None

    def branch(self) -> str:
        branch = self.run("symbolic-ref", "-q", "HEAD", check=False).decode().strip()
        if not branch.startswith("refs/heads/"):
            raise CommiterError("Check out a branch first; detached HEAD is not supported.")
        return branch

    def git_path(self, name: str) -> Path:
        return Path(os.fsdecode(self.run("rev-parse", "--path-format=absolute", "--git-path", name).rstrip(b"\n")))

    def ready(self) -> None:
        self.branch()
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
            if self.git_path(marker).exists():
                raise CommiterError(f"Finish the current Git operation ({marker}) before making a plan.")
        if self.run("config", "--bool", "core.sparseCheckout", check=False).strip() == b"true":
            raise CommiterError("Sparse checkouts are not supported. Use a full working directory.")
        if self.run("ls-files", "--unmerged", "-z"):
            raise CommiterError("Resolve the unmerged index entries first.")
        if any(row.startswith(b"160000 ") for row in self.run("ls-files", "--stage", "-z").split(b"\0")):
            raise CommiterError("This version does not support repositories containing submodules.")

    def empty_tree(self) -> str:
        return self.run("hash-object", "-w", "-t", "tree", "--stdin", input=b"").decode().strip()

    def index_tree(self, env: dict[str, str] | None = None) -> str:
        if env is not None:
            return self.run("write-tree", env=env).decode().strip()
        # write-tree may refresh cache-tree data. Copy the index to keep planning
        # genuinely read-only with respect to the user's staging area.
        with tempfile.TemporaryDirectory(prefix="commiter-staged-") as directory:
            index = Path(directory) / "index"
            original = self.git_path("index")
            staged_env = {"GIT_INDEX_FILE": str(index)}
            if original.exists():
                shutil.copyfile(original, index)
                self.run("-c", "core.splitIndex=false", "update-index", "--no-split-index", env=staged_env)
            else:
                self.run("read-tree", "--empty", env=staged_env)
            return self.run("write-tree", env=staged_env).decode().strip()

    @contextlib.contextmanager
    def temporary_index(self, tree: str | None) -> Iterator[tuple[dict[str, str], Path]]:
        with tempfile.TemporaryDirectory(prefix="commiter-index-") as directory:
            index = Path(directory) / "index"
            env = {"GIT_INDEX_FILE": str(index)}
            self.run("-c", "core.splitIndex=false", "read-tree", tree or "--empty", env=env)
            yield env, index

    def snapshot(self, source: str, excluded: list[str]) -> str:
        if source == "staged":
            return self.index_tree()
        with self.temporary_index(self.head()) as (env, _):
            args = ["add", "-A", "--", "."]
            args.extend(f":(top,literal,exclude){path}" for path in excluded)
            self.run(*args, env=env)
            return self.index_tree(env)

    def diff(self, base: str, target: str, path: str | None = None, *flags: str) -> bytes:
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/", *flags, base, target, "--"]
        if path is not None:
            args.append(f":(top,literal){path}")
        return self.run(*args)

    def ancestor(self, old: str, new: str) -> bool:
        try:
            self.run("merge-base", "--is-ancestor", old, new)
            return True
        except CommiterError:
            return False


def excluded_paths(repo: Repo, paths: list[Path]) -> list[str]:
    result = []
    for path in paths:
        try:
            relative = path.resolve().relative_to(repo.root).as_posix()
        except ValueError:
            continue
        if relative != ".":
            result.append(relative)
    return sorted(set(result))

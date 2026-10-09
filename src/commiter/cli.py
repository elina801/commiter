from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .common import CommiterError, display, read_json, redact, state_dir, write_json
from .environment import token_environment
from .executor import apply_plan, push_receipt
from .github import identities
from .planner import create_plan
from .settings import discover_settings, load_settings


def show_plan(plan: dict) -> None:
    units = {row["id"]: row for row in plan["units"]}
    counts = Counter(commit["account"] for commit in plan["commits"])
    print(f"Repository: {plan['repository']}  Branch: {display(plan['branch'].removeprefix('refs/heads/'))}")
    print(f"Source: {plan['source']}  Split: {plan['split']}  Commits: {len(plan['commits'])}")
    for account in plan["accounts"]:
        login = account["login"]
        percentage = 100 * counts[login] / len(plan["commits"])
        print(f"  {login}: {counts[login]} commits ({percentage:.1f}%)")
    for index, commit in enumerate(plan["commits"], 1):
        print(f"\n{index}. {commit['account']} — {display(commit['message'])}")
        for unit_id in commit["units"]:
            unit = units[unit_id]
            suffix = f" [hunk {unit['hunk'] + 1}]" if unit["kind"] == "hunk" else ""
            print(f"   {display(unit['path'])}{suffix}")
    if len(plan["commits"]) < 3:
        print("\nFewer than three change units: some accounts cannot receive a commit.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="commiter", usage='commiter "Message 1" "Message 2" "Message 3"',
        description="Split pending changes across three GitHub accounts and publish them. All settings come from .env.",
        epilog="Messages map to GITHUB_ACCOUNT_1, GITHUB_ACCOUNT_2, and GITHUB_ACCOUNT_3. Set COMMITER_DRY_RUN=true in .env to preview.",
    )
    result.add_argument("--version", action="version", version=f"commiter {__version__}")
    result.add_argument("messages", nargs="*", metavar="MESSAGE", help="one commit message per account")
    return result


def normalize_messages(messages: list[str]) -> list[str]:
    # Accept both shell syntax forms: "one", "two", "three" and "one" , "two" , "three".
    if len(messages) == 5 and messages[1] == messages[3] == ",":
        messages = messages[::2]
    if len(messages) == 3 and messages[0].endswith(",") and messages[1].endswith(","):
        messages = [messages[0][:-1], messages[1][:-1], messages[2]]
    if len(messages) != 3 or any(not text.strip() or "\x00" in text for text in messages):
        raise CommiterError('Use exactly three nonempty messages: commiter "Message 1" "Message 2" "Message 3"')
    return messages


def run(messages: list[str]) -> None:
    messages = normalize_messages(messages)
    env_file = discover_settings()
    settings = load_settings(env_file)
    config, repo = settings.config, settings.repo
    resume_command = "commiter " + " ".join(shlex.quote(message) for message in messages)
    with token_environment(env_file, [account.token_env for account in config.accounts], override=True,
                           state_path=settings.state_path):
        repo.ready()
        key = hashlib.sha256(os.fsencode(str(repo.root))).hexdigest()[:20]
        run_path = state_dir() / "runs" / f"{key}.json"
        previous = read_json(run_path) if run_path.exists() and not settings.dry_run else None
        if previous and previous.get("status") == "pending":
            if previous.get("messages") != messages or previous.get("repository") != config.repository:
                original = "commiter " + " ".join(shlex.quote(text) for text in previous["messages"])
                raise CommiterError(f"A previous run still has saved commits awaiting publication. Resume it with: {original}")
            if not settings.push:
                print("These commits are already created locally. Set COMMITER_PUSH=true in .env and rerun to publish them.")
                return
            receipt_path = Path(previous["receipt"])
            receipt = read_json(receipt_path)
            print("Resuming publication of the saved commits...")
            push_receipt(repo, config, receipt, receipt_path, resolved=identities(config), resume_command=resume_command)
            previous["status"] = "complete"
            write_json(run_path, previous)
            print(f"Published {len(receipt['commits'])} commits to {config.repository}.")
            return
        plan, plan_path = create_plan(repo, config, source=settings.source, split=settings.split,
                                      count=settings.count, lines_per_commit=settings.lines_per_commit,
                                      jitter=settings.jitter, seed=settings.seed, env_files=[env_file],
                                      account_messages=messages)
        show_plan(plan)
        print(f"\nPlan: {plan_path}")
        if settings.dry_run:
            print("Dry run: preview saved. Set COMMITER_DRY_RUN=false in .env to create the commits.")
            return
        resolved = identities(config)
        receipt, receipt_path = apply_plan(repo, config, plan, resolved=resolved)
        print(f"\nCreated {len(receipt['commits'])} local commits. Receipt: {receipt_path}")
        record = {"status": "pending", "repository": config.repository, "messages": messages, "receipt": str(receipt_path)}
        write_json(run_path, record)
        if settings.push:
            push_receipt(repo, config, receipt, receipt_path, resolved=resolved, resume_command=resume_command)
            record["status"] = "complete"
            write_json(run_path, record)
            print(f"Published {len(receipt['commits'])} commits to {config.repository}.")
        else:
            print("Publication disabled by COMMITER_PUSH=false. Enable it and rerun these messages to publish.")


def main(argv: list[str] | None = None) -> int:
    command = parser()
    args = command.parse_args(argv)
    if not args.messages:
        command.print_help()
        return 0
    try:
        run(args.messages)
    except CommiterError as exc:
        detail = "\n".join(display(line) for line in redact(str(exc)).split("\n"))
        print(f"commiter: {detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"commiter: {display(redact(str(exc)))}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ncommiter: interrupted.", file=sys.stderr)
        return 130
    return 0

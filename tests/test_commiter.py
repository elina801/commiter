from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from commiter.cli import main
from commiter.common import CommiterError, read_json
from commiter.config import Account, load_config, parse_repository, save_config
from commiter.executor import apply_plan, push_receipt
from commiter.environment import find_env_file, token_environment
from commiter.git import Repo, git_env
from commiter.github import Identity, identities
from commiter.planner import create_plan
from commiter.settings import load_settings


class GitWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="commiter-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.directory / "state"),
            "XDG_CONFIG_HOME": str(self.directory / "config"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.root = self.directory / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], env=git_env(), check=True)
        self.repo = Repo(self.root)
        self.repo.run("config", "user.name", "Original User")
        self.repo.run("config", "user.email", "original@example.com")
        self.write("tracked.txt", "".join(f"original line {i}\n" for i in range(100)))
        self.write("delete.txt", "delete me\n")
        self.repo.run("add", "-A")
        self.repo.run("commit", "-q", "-m", "Baseline")
        self.config = save_config(self.directory / "config.json", "owner/project", [
            Account("alice", "TEST_TOKEN_1", 40), Account("bob", "TEST_TOKEN_2", 30), Account("charlie", "TEST_TOKEN_3", 30),
        ])
        self.resolved = {account.login: Identity(account.login, account.login.title(), f"{i + 1}+{account.login}@users.noreply.github.com", "test-token")
                         for i, account in enumerate(self.config.accounts)}

    def write(self, path: str, contents: str | bytes):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, bytes):
            target.write_bytes(contents)
        else:
            target.write_text(contents, encoding="utf-8")

    def changes(self, count=12):
        for index in range(count):
            self.write(f"src/module{index:02}.py", f"value = {index}\n" * (index + 1))

    def test_ten_commits_keep_index_and_worktree_then_exact_final_tree(self):
        self.changes()
        self.repo.run("add", "src/module00.py")
        original_head = self.repo.head()
        original_index = self.repo.git_path("index").read_bytes()
        plan, path = create_plan(self.repo, self.config, count=10, jitter=0, seed=42)
        self.assertEqual(self.repo.head(), original_head)
        self.assertEqual(self.repo.git_path("index").read_bytes(), original_index)
        self.assertEqual(Counter(commit["account"] for commit in plan["commits"]), {"alice": 4, "bob": 3, "charlie": 3})
        self.assertEqual(read_json(path), plan)
        receipt, receipt_path = apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(len(receipt["commits"]), 10)
        self.assertEqual(self.repo.run("rev-parse", "HEAD^{tree}").decode().strip(), plan["target_tree"])
        self.assertEqual(self.repo.run("status", "--porcelain"), b"")
        self.assertEqual(self.repo.run("rev-list", "--count", f"{original_head}..HEAD").strip(), b"10")
        self.assertEqual(read_json(receipt_path)["status"], "applied")
        for commit in receipt["commits"]:
            actual = self.repo.run("show", "-s", "--format=%an|%ae|%cn|%ce", commit["oid"]).decode().strip()
            identity = self.resolved[commit["account"]]
            self.assertEqual(actual, f"{identity.name}|{identity.email}|{identity.name}|{identity.email}")
        with self.assertRaisesRegex(CommiterError, "HEAD"):
            apply_plan(self.repo, self.config, plan, resolved=self.resolved)

    def test_staged_source_preserves_unstaged_and_untracked_changes(self):
        self.changes(3)
        self.repo.run("add", "src")
        self.write("src/module00.py", "unstaged content\n")
        self.write("untracked.txt", "keep me\n")
        plan, _ = create_plan(self.repo, self.config, source="staged", count=3)
        apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(self.repo.run("show", "HEAD:src/module00.py"), b"value = 0\n")
        self.assertEqual((self.root / "src/module00.py").read_text(), "unstaged content\n")
        status = self.repo.run("status", "--porcelain").decode()
        self.assertIn(" M src/module00.py", status)
        self.assertIn("?? untracked.txt", status)
        self.assertEqual(self.repo.diff("HEAD", self.repo.index_tree(), None, "--name-only"), b"")

    def test_hunks_with_insert_delete_replace_and_no_final_newline(self):
        lines = (self.root / "tracked.txt").read_text().splitlines(keepends=True)
        lines[80:82] = []
        lines[60:60] = ["inserted 1\n", "inserted 2\n", "inserted 3\n"]
        lines[40] = "changed\n"
        lines[10] = "other change\n"
        lines[-1] = "final without newline"
        self.write("tracked.txt", "".join(lines))
        plan, _ = create_plan(self.repo, self.config, split="hunks", count=5, seed=7)
        self.assertEqual(len(plan["units"]), 5)
        self.assertTrue(all(unit["kind"] == "hunk" for unit in plan["units"]))
        apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(self.repo.run("status", "--porcelain"), b"")

    def test_whole_new_file_cannot_make_fake_extra_commits(self):
        self.write("big.py", "print('hello')\n" * 2000)
        with self.assertRaisesRegex(CommiterError, "indivisible"):
            create_plan(self.repo, self.config, split="hunks", count=3)
        plan, _ = create_plan(self.repo, self.config, split="hunks")
        self.assertEqual(len(plan["commits"]), 1)

    def test_binary_symlink_deletion_mode_change_and_special_filenames(self):
        self.write("binary.bin", b"\0\xff\x02\x03" * 100)
        self.write("space and \"quote\".txt", "special\n")
        self.write("colon:*.txt", "literal path\n")
        self.write("line\nbreak.txt", "new line filename\n")
        self.write("नेपाली.txt", "नमस्ते\n")
        (self.root / "link").symlink_to("tracked.txt")
        (self.root / "tracked.txt").chmod(0o755)
        (self.root / "delete.txt").unlink()
        plan, _ = create_plan(self.repo, self.config, count=6)
        apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(self.repo.run("status", "--porcelain"), b"")
        self.assertEqual((self.root / "binary.bin").read_bytes(), b"\0\xff\x02\x03" * 100)
        self.assertTrue((self.root / "link").is_symlink())
        self.assertFalse((self.root / "delete.txt").exists())

    def test_file_directory_replacements(self):
        self.write("folder/file.txt", "original\n")
        self.write("other", "original\n")
        self.repo.run("add", "-A")
        self.repo.run("commit", "-q", "-m", "More baseline")
        (self.root / "folder/file.txt").unlink()
        (self.root / "folder").rmdir()
        self.write("folder", "replacement file\n")
        (self.root / "other").unlink()
        self.write("other/nested.txt", "replacement directory\n")
        plan, _ = create_plan(self.repo, self.config, count=4)
        apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(self.repo.run("status", "--porcelain"), b"")

    def test_initial_repository_has_real_root_commit(self):
        fresh = self.directory / "fresh"
        fresh.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(fresh)], env=git_env(), check=True)
        repo = Repo(fresh)
        for index in range(3):
            (fresh / f"file{index}.txt").write_text(f"content {index}\n")
        plan, _ = create_plan(repo, self.config, count=3)
        receipt, _ = apply_plan(repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(repo.run("rev-list", "--count", "HEAD").strip(), b"3")
        self.assertEqual(repo.run("show", "-s", "--format=%P", receipt["commits"][0]["oid"]).strip(), b"")
        self.assertEqual(repo.run("status", "--porcelain"), b"")

    def test_changed_worktree_index_or_branch_rejects_stale_plan(self):
        self.changes(3)
        plan, _ = create_plan(self.repo, self.config, count=3)
        original_head = self.repo.head()
        self.write("src/module00.py", "changed since planning\n")
        with self.assertRaisesRegex(CommiterError, "selected changes"):
            apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(self.repo.head(), original_head)
        self.repo.run("add", "-A")
        with self.assertRaisesRegex(CommiterError, "staged changes"):
            apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.repo.run("switch", "-q", "-c", "other-branch")
        with self.assertRaisesRegex(CommiterError, "branch"):
            apply_plan(self.repo, self.config, plan, resolved=self.resolved)

    def test_plan_tampering_and_index_lock_fail_without_branch_changes(self):
        self.changes(3)
        plan, _ = create_plan(self.repo, self.config, count=3)
        tampered = copy.deepcopy(plan)
        tampered["commits"][1]["units"] = tampered["commits"][0]["units"]
        with self.assertRaisesRegex(CommiterError, "exactly once"):
            apply_plan(self.repo, self.config, tampered, resolved=self.resolved)
        old = self.repo.head()
        lock = self.repo.git_path("index.lock")
        lock.write_text("someone else owns this lock")
        with self.assertRaisesRegex(CommiterError, "locked"):
            apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(self.repo.head(), old)
        self.assertEqual(lock.read_text(), "someone else owns this lock")

    def test_plan_output_and_config_inside_worktree_are_excluded(self):
        self.changes(3)
        config = save_config(self.root / "custom.json", "owner/project", list(self.config.accounts))
        plan, _ = create_plan(self.repo, config, count=3, output=self.root / "plan.json")
        self.assertNotIn("custom.json", [unit["path"] for unit in plan["units"]])
        apply_plan(self.repo, config, plan, resolved=self.resolved)
        self.assertNotIn(b"plan.json", self.repo.run("ls-tree", "-r", "--name-only", "HEAD"))

    def test_excluded_config_keeps_its_existing_staged_edits(self):
        config = save_config(self.root / "custom.json", "owner/project", list(self.config.accounts))
        self.repo.run("add", "custom.json")
        self.repo.run("commit", "-q", "-m", "Track config")
        original = (self.root / "custom.json").read_text()
        (self.root / "custom.json").write_text(original + "\n")
        self.repo.run("add", "custom.json")
        self.changes(3)
        plan, _ = create_plan(self.repo, config, count=3)
        apply_plan(self.repo, config, plan, resolved=self.resolved)
        self.assertEqual(self.repo.run("show", "HEAD:custom.json").decode(), original)
        self.assertEqual(self.repo.run("show", ":custom.json").decode(), original + "\n")
        self.assertEqual(self.repo.run("status", "--porcelain").decode(), "M  custom.json\n")

    def test_ignored_files_are_excluded_but_tracked_ignored_files_remain(self):
        self.write(".gitignore", "*.secret\n")
        self.write("secret.secret", "do not add me\n")
        self.write("tracked.txt", "a change\n")
        plan, _ = create_plan(self.repo, self.config)
        self.assertNotIn("secret.secret", [unit["path"] for unit in plan["units"]])
        self.assertIn("tracked.txt", [unit["path"] for unit in plan["units"]])

    def test_split_index_can_be_planned_without_mutating_original(self):
        self.changes(3)
        self.repo.run("add", "src/module00.py")
        self.repo.run("update-index", "--split-index")
        before = self.repo.git_path("index").read_bytes()
        plan, _ = create_plan(self.repo, self.config, count=3)
        self.assertEqual(before, self.repo.git_path("index").read_bytes())
        apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        self.assertEqual(self.repo.run("status", "--porcelain"), b"")

    def test_jitter_and_seed_reproducible_and_all_three_participate(self):
        self.changes(12)
        first, _ = create_plan(self.repo, self.config, count=12, seed=93, jitter=0.3)
        second, _ = create_plan(self.repo, self.config, count=12, seed=93, jitter=0.3)
        self.assertEqual(first["commits"], second["commits"])
        self.assertEqual(len({commit["account"] for commit in first["commits"]}), 3)
        self.assertNotEqual(first["accounts"][0]["varied_share"], 0.4)

    def test_pushes_with_each_identity_and_resumes_actual_remote_state(self):
        self.changes(6)
        plan, _ = create_plan(self.repo, self.config, count=6, seed=1)
        receipt, path = apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        remote = self.directory / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], env=git_env(), check=True)
        actors = []
        push_calls = 0

        def local_transport(repo, config, identity, *args):
            nonlocal push_calls
            translated = [str(remote) if value == config.url else value for value in args]
            if args[0] == "push":
                push_calls += 1
                if push_calls == 2:
                    raise CommiterError("Simulated transport failure")
                actors.append(identity.login)
            return repo.run(*translated)

        with patch("commiter.executor.authenticated_git", side_effect=local_transport):
            with self.assertRaisesRegex(CommiterError, "Resume with"):
                push_receipt(self.repo, self.config, receipt, path, resolved=self.resolved, progress=lambda _: None)
            self.assertEqual(len(read_json(path)["pushed"]), 1)
            receipt["pushed"] = []  # stale bookkeeping must not cause an older commit to be pushed.
            push_receipt(self.repo, self.config, receipt, path, resolved=self.resolved, progress=lambda _: None)
            self.assertEqual(actors, [commit["account"] for commit in receipt["commits"]])
            self.assertEqual(len(read_json(path)["pushed"]), 6)
            self.assertEqual(self.repo.run("ls-remote", str(remote), "refs/heads/main").split()[0].decode(), receipt["commits"][-1]["oid"])
            previous = len(actors)
            push_receipt(self.repo, self.config, receipt, path, resolved=self.resolved, progress=lambda _: None)
            self.assertEqual(len(actors), previous)

    def test_remote_divergence_is_rejected_before_any_push(self):
        self.changes(3)
        plan, _ = create_plan(self.repo, self.config, count=3)
        receipt, path = apply_plan(self.repo, self.config, plan, resolved=self.resolved)
        with patch("commiter.executor.authenticated_git", return_value=b"f" * 40 + b"\trefs/heads/main\n") as transport:
            with self.assertRaisesRegex(CommiterError, "remote branch"):
                push_receipt(self.repo, self.config, receipt, path, resolved=self.resolved, progress=lambda _: None)
            self.assertEqual(transport.call_count, 1)

    def write_settings(self, **overrides):
        values = {"COMMITER_REPO": str(self.root), "COMMITER_GITHUB": "owner/project",
                  "COMMITER_WEIGHTS": "40,30,30", "COMMITER_COMMITS": "6", "COMMITER_JITTER": "0",
                  "COMMITER_PUSH": "true", "COMMITER_DRY_RUN": "false",
                  "COMMITER_STATE_DIR": str(self.directory / "new-state")}
        for index, account in enumerate(self.config.accounts, 1):
            values[f"GITHUB_ACCOUNT_{index}"] = account.login
            values[f"GITHUB_TOKEN_{index}"] = f"dotenv-secret-{index}"
        values.update(overrides)
        path = self.directory / ".env"
        path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        original_directory = Path.cwd()
        self.addCleanup(os.chdir, original_directory)
        os.chdir(self.directory)
        return path

    def test_single_command_creates_account_messages_and_pushes_without_json_config(self):
        self.changes(6)
        self.write_settings()
        messages = ["Build dashboard", "Add settings", "Update styles"]
        remote = self.directory / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], env=git_env(), check=True)
        actors = []

        def api(endpoint, token):
            index = int(token[-1]) - 1
            if endpoint == "/user":
                return {"id": index + 1, "login": self.config.accounts[index].login}
            return {"full_name": "owner/project", "permissions": {"push": True}}

        credentials = []

        def local_transport(repo, config, identity, *args):
            if args[0] == "push":
                actors.append(identity.login)
                credentials.append((identity.login, identity.token))
            return repo.run(*[str(remote) if value == config.url else value for value in args])

        output, error = io.StringIO(), io.StringIO()
        with patch("commiter.github.api_get", side_effect=api), patch("commiter.executor.authenticated_git", side_effect=local_transport), redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(messages), 0)
        self.assertEqual(error.getvalue(), "")
        self.assertIn("Published 6 commits", output.getvalue())
        self.assertNotIn("dotenv-secret", output.getvalue())
        self.assertEqual(Counter(actors), {"alice": 2, "bob": 2, "charlie": 2})
        self.assertEqual(set(credentials), {("alice", "dotenv-secret-1"), ("bob", "dotenv-secret-2"), ("charlie", "dotenv-secret-3")})
        log = self.repo.run("log", "-6", "--format=%ae|%s").decode().splitlines()
        remote_log = self.repo.run("--git-dir", str(remote), "log", "-6", "--format=%ae|%s", "refs/heads/main").decode().splitlines()
        self.assertEqual(remote_log, log)
        self.assertEqual(self.repo.run("--git-dir", str(remote), "rev-parse", "refs/heads/main^{tree}").strip(),
                         self.repo.run("rev-parse", "HEAD^{tree}").strip())
        for row in log:
            email, message = row.split("|", 1)
            index = int(email.split("+", 1)[0]) - 1
            self.assertEqual(message, messages[index])
        self.assertEqual(self.repo.run("status", "--porcelain"), b"")
        self.assertFalse((self.directory / "config/commiter").exists())

    def test_single_command_resumes_failed_push_without_duplicate_commits(self):
        self.changes(6)
        self.write_settings()
        remote = self.directory / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], env=git_env(), check=True)
        pushes = 0

        def local_transport(repo, config, identity, *args):
            nonlocal pushes
            if args[0] == "push":
                pushes += 1
                if pushes == 2:
                    raise CommiterError("Simulated publish failure")
            return repo.run(*[str(remote) if value == config.url else value for value in args])

        messages = ["First", "Second", "Third"]
        output, error = io.StringIO(), io.StringIO()
        with patch("commiter.cli.identities", return_value=self.resolved), patch("commiter.executor.authenticated_git", side_effect=local_transport), redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(messages), 1)
            saved_head = self.repo.head()
            self.assertEqual(main(["Different", "Second", "Third"]), 1)
            self.assertEqual(self.repo.head(), saved_head)
            self.assertEqual(main(messages), 0)
            self.assertEqual(self.repo.head(), saved_head)
        self.assertIn("Resuming publication", output.getvalue())
        self.assertIn("commiter First Second Third", error.getvalue())
        self.assertEqual(pushes, 7)  # six successful pushes and one failed attempt
        self.assertEqual(self.repo.run("ls-remote", str(remote), "refs/heads/main").split()[0].decode(), saved_head)

    def test_single_command_dry_run_accepts_comma_syntax_and_does_not_authenticate(self):
        self.changes(6)
        self.write_settings(COMMITER_DRY_RUN="true", GITHUB_TOKEN_1="", GITHUB_TOKEN_2="", GITHUB_TOKEN_3="")
        before_head = self.repo.head()
        before_index = self.repo.git_path("index").read_bytes()
        with patch("commiter.cli.identities") as github, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["First,", "Second,", "Third"]), 0)
            github.assert_not_called()
        self.assertIn("Dry run", output.getvalue())
        self.assertEqual(before_head, self.repo.head())
        self.assertEqual(before_index, self.repo.git_path("index").read_bytes())
        plan = read_json(next((self.directory / "new-state/plans").glob("*.json")))
        self.assertEqual({commit["message"] for commit in plan["commits"]}, {"First", "Second", "Third"})

    def test_single_command_local_only_then_publish_same_messages(self):
        self.changes(3)
        path = self.write_settings(COMMITER_PUSH="false", COMMITER_COMMITS="3")
        messages = ["One", "Two", "Three"]
        with patch("commiter.cli.identities", return_value=self.resolved), patch("commiter.cli.push_receipt") as publish, redirect_stdout(io.StringIO()):
            self.assertEqual(main(messages), 0)
            saved_head = self.repo.head()
            self.assertEqual(main(messages), 0)
            publish.assert_not_called()
            path.write_text(path.read_text().replace("COMMITER_PUSH=false", "COMMITER_PUSH=true"))
            self.assertEqual(main(messages), 0)
            publish.assert_called_once()
            self.assertEqual(self.repo.head(), saved_head)

    def test_invalid_settings_and_argument_count_fail_before_changes(self):
        self.changes(6)
        before = self.repo.head()
        path = self.write_settings(COMMITER_JITTER="nan")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["One", "Two", "Three"]), 1)
            self.assertEqual(main(["One", "Two"]), 1)
        self.assertEqual(self.repo.head(), before)
        path.write_text(path.read_text().replace("COMMITER_JITTER=nan", "COMMITER_JITTER=0.15") + "COMMITER_PSUH=true\n")
        with self.assertRaisesRegex(CommiterError, "Unknown .env setting"):
            load_settings(path)

    def test_settings_relative_paths_origin_and_optional_identities(self):
        self.repo.run("remote", "add", "origin", "git@github.com:owner/project.git")
        path = self.write_settings(COMMITER_REPO="repo", COMMITER_GITHUB="", COMMITER_STATE_DIR="settings-state",
                                   GITHUB_NAME_1="Alice Example", GITHUB_EMAIL_1="alice@example.com")
        settings = load_settings(path)
        self.assertEqual(settings.repo.root, self.root.resolve())
        self.assertEqual(settings.config.repository, "owner/project")
        self.assertEqual(settings.state_path, (self.directory / "settings-state").resolve())
        self.assertEqual(settings.config.accounts[0].name, "Alice Example")
        self.assertEqual(settings.config.accounts[0].email, "alice@example.com")

    def test_env_files_are_excluded_and_staged_credentials_are_rejected(self):
        self.changes(3)
        self.write(".env", "GITHUB_TOKEN_1=secret-from-file\n")
        self.write("accounts.env", "GITHUB_TOKEN_2=another-file-secret\n")
        plan, _ = create_plan(self.repo, self.config, count=3, env_files=[self.root / "accounts.env"])
        self.assertEqual(len(plan["units"]), 3)
        self.assertIn(".env", plan["credential_files"])
        self.assertIn("accounts.env", plan["credential_files"])
        self.repo.run("add", "-A")
        original_index = self.repo.git_path("index").read_bytes()
        with self.assertRaisesRegex(CommiterError, "token .env file"):
            create_plan(self.repo, self.config, source="staged", count=3, env_files=[self.root / "accounts.env"])
        self.assertEqual(original_index, self.repo.git_path("index").read_bytes())



class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / ".env"
        self.key = "COMMITER_TEST_DOTENV_TOKEN"
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(self.key, None)

    def test_comments_quotes_literal_values_and_only_requested_keys(self):
        self.path.write_text(f"# Tokens\nexport {self.key}='secret-${{literal}}' # comment\nUNRELATED_SETTING=unused\n")
        with token_environment(self.path, [self.key]):
            self.assertEqual(os.environ[self.key], "secret-${literal}")
            self.assertNotIn("UNRELATED_SETTING", os.environ)
        self.assertNotIn(self.key, os.environ)

    def test_shell_precedence_and_exception_redaction(self):
        self.path.write_text(f"{self.key}=file-secret-value\n")
        os.environ[self.key] = "shell-secret"
        with token_environment(self.path, [self.key]):
            self.assertEqual(os.environ[self.key], "shell-secret")
        self.assertEqual(os.environ[self.key], "shell-secret")
        with token_environment(self.path, [self.key], override=True):
            self.assertEqual(os.environ[self.key], "file-secret-value")
        self.assertEqual(os.environ[self.key], "shell-secret")
        del os.environ[self.key]
        with self.assertRaisesRegex(CommiterError, "\\[redacted\\]") as error:
            with token_environment(self.path, [self.key]):
                raise CommiterError("downstream error: file-secret-value")
        self.assertNotIn("file-secret-value", str(error.exception))
        self.assertNotIn(self.key, os.environ)

    def test_discovery_precedence_and_missing_explicit_file(self):
        cwd = self.root / "current"
        repo = self.root / "target"
        cwd.mkdir()
        repo.mkdir()
        (repo / ".env").write_text("TOKEN=repo\n")
        original_directory = Path.cwd()
        self.addCleanup(os.chdir, original_directory)
        os.chdir(cwd)
        self.assertEqual(find_env_file(repo), (repo / ".env").resolve())
        (cwd / ".env").write_text("TOKEN=current\n")
        self.assertEqual(find_env_file(repo), (cwd / ".env").resolve())
        self.assertEqual(find_env_file(repo, repo / ".env"), (repo / ".env").resolve())
        with self.assertRaisesRegex(CommiterError, "--env-file"):
            find_env_file(repo, repo / "missing.env")


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "config.json"
        self.config = save_config(self.path, "owner/project", [
            Account("alice", "TEST_TOKEN_1", 40), Account("bob", "TEST_TOKEN_2", 30), Account("charlie", "TEST_TOKEN_3", 30),
        ])
        self.environment = patch.dict(os.environ, {f"TEST_TOKEN_{i + 1}": f"token-{i + 1}" for i in range(3)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def api(self, endpoint, token):
        index = int(token[-1]) - 1
        account = self.config.accounts[index]
        if endpoint == "/user":
            return {"id": index + 1, "login": account.login, "name": account.login.title()}
        return {"full_name": "owner/project", "permissions": {"push": True}, "archived": False, "disabled": False}

    def test_account_validation_and_noreply_identity(self):
        with patch("commiter.github.api_get", side_effect=self.api):
            resolved = identities(self.config)
        self.assertEqual(resolved["bob"].email, "2+bob@users.noreply.github.com")
        self.assertNotIn("token-2", repr(resolved["bob"]))

    def test_wrong_token_account_and_missing_push_permission(self):
        with patch("commiter.github.api_get", return_value={"login": "someoneelse", "id": 9}):
            with self.assertRaisesRegex(CommiterError, "does not belong"):
                identities(self.config)
        with patch("commiter.github.api_get", side_effect=[{"login": "alice", "id": 1}, {"permissions": {"push": False}}]):
            with self.assertRaisesRegex(CommiterError, "push access"):
                identities(self.config)

    def test_custom_email_must_be_verified(self):
        accounts = list(self.config.accounts)
        accounts[0] = Account("alice", "TEST_TOKEN_1", 40, email="alice@example.com")
        config = save_config(self.path, "owner/project", accounts)
        with patch("commiter.github.api_get", side_effect=[self.api("/user", "token-1"), self.api("/repos/owner/project", "token-1"),
                                                          [{"email": "alice@example.com", "verified": False}]]):
            with self.assertRaisesRegex(CommiterError, "not verified"):
                identities(config)

    def test_invalid_config_does_not_overwrite_previous_valid_config(self):
        original = self.path.read_bytes()
        with self.assertRaises(CommiterError):
            save_config(self.path, "owner/project", [Account("alice", "TOKEN", 1)] * 3)
        self.assertEqual(self.path.read_bytes(), original)
        data = read_json(self.path)
        data["accounts"][0]["token"] = "secret"
        self.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(CommiterError, "token_env"):
            load_config(self.path)

    def test_repository_urls_do_not_accept_credentials_or_other_hosts(self):
        for value in ("owner/project", "https://github.com/owner/project.git", "git@github.com:owner/project.git", "ssh://git@github.com/owner/project.git"):
            self.assertEqual(parse_repository(value), "owner/project")
        for value in ("https://token@github.com/owner/project", "https://gitlab.com/owner/project", "owner/..", "https://github.com:8443/owner/project", "owner/project?secret"):
            with self.assertRaises(CommiterError):
                parse_repository(value)


if __name__ == "__main__":
    unittest.main()

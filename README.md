# Commiter

Split pending changes across three GitHub accounts with one command:

```sh
commiter "Build dashboard" "Add settings" "Update styles"
```

**Every setting comes from `.env`.** The first message is used for every commit assigned to account 1, the second for account 2, and the third for account 3. Messages are stored exactly as supplied. Commiter splits the change, verifies all three account tokens, creates the commits, and pushes each commit with its assigned account's token.

## Install

Requires Python 3.11+ and Git. The first option also requires [uv](https://docs.astral.sh/uv/getting-started/installation/). `python-dotenv` is installed automatically.

Clone [elina801/commiter](https://github.com/elina801/commiter), then install the CLI:

```sh
git clone https://github.com/elina801/commiter.git
cd commiter
uv tool install .
commiter --help
```

Or, from the cloned `commiter` folder, install into a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/commiter --help
```

## Configure once

Create your local settings file from the template included in the repository:

```sh
cp .env.example .env
```

Edit `.env` with your target repository and three accounts. The local `.env` is ignored by Git. Commiter uses the nearest `.env` in the current directory or its parents. Relative paths are resolved from that file's directory. Settings in `.env` are authoritative, including tokens, even if your shell has different values.

```dotenv
COMMITER_REPO=/path/to/your/local/clone
COMMITER_GITHUB=https://github.com/owner/repository.git

GITHUB_ACCOUNT_1=alice
GITHUB_TOKEN_1=your_alice_token
GITHUB_ACCOUNT_2=bob
GITHUB_TOKEN_2=your_bob_token
GITHUB_ACCOUNT_3=charlie
GITHUB_TOKEN_3=your_charlie_token

COMMITER_WEIGHTS=40,30,30
COMMITER_JITTER=0.15
COMMITER_COMMITS=
COMMITER_LINES_PER_COMMIT=200
COMMITER_SEED=
COMMITER_SOURCE=worktree
COMMITER_SPLIT=files
COMMITER_PUSH=true
COMMITER_DRY_RUN=false
COMMITER_STATE_DIR=
```

`COMMITER_REPO` points to the existing local clone whose changes you want to distribute. `COMMITER_GITHUB` accepts that project's GitHub URL or `owner/repository`; leave it blank to infer the target from the clone's `origin` remote. Each of the three distinct accounts needs write access to that repository. For fine-grained tokens, select the target repo and grant **Contents: Read and write**; changes to workflow files also require **Workflows: Read and write**. Organization approval, SSO, and branch rules still apply. See [GitHub's token documentation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

After filling in `.env`, run:

```sh
commiter "Message 1" "Message 2" "Message 3"
```

With `COMMITER_PUSH=true` and `COMMITER_DRY_RUN=false`, this one command pushes **all three accounts' assigned work** to the configured GitHub repository and the clone's current branch:

- Account 1's commits use `Message 1` and are pushed with `GITHUB_TOKEN_1`.
- Account 2's commits use `Message 2` and are pushed with `GITHUB_TOKEN_2`.
- Account 3's commits use `Message 3` and are pushed with `GITHUB_TOKEN_3`.

The command reports success after every assigned commit has been published. If a push fails, rerun the same three messages to resume the remaining commits.

The comma syntax also works:

```sh
commiter "Message 1", "Message 2", "Message 3"
```

No initialization command or separate configuration JSON is needed. Plans and receipts are generated automatically as run records.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `COMMITER_REPO` | `.` relative to `.env` | Local target clone. |
| `COMMITER_GITHUB` | Infer from `origin` | GitHub `owner/repo` or GitHub URL. |
| `GITHUB_ACCOUNT_1`, `_2`, `_3` | Required | Account logins, matching the three message positions. |
| `GITHUB_TOKEN_1`, `_2`, `_3` | Required for committing | Each account's personal access token. |
| `COMMITER_WEIGHTS` | `40,30,30` | Relative number of commits per account. |
| `COMMITER_JITTER` | `0.15` | Independent relative variation of each weight, from `0` to `0.5`. |
| `COMMITER_COMMITS` | Blank / automatic | Desired positive commit count, bounded by available change units. |
| `COMMITER_LINES_PER_COMMIT` | `200` | Approximate commit size when the count is automatic. |
| `COMMITER_SEED` | Blank / random | Optional integer for a repeatable allocation. |
| `COMMITER_SOURCE` | `worktree` | `worktree` includes staged, unstaged, deleted, and nonignored new files; `staged` uses only staged changes. |
| `COMMITER_SPLIT` | `files` | `files` keeps whole files together; `hunks` divides separate edits in existing regular text files. |
| `COMMITER_PUSH` | `true` | Publish each created commit with its assigned account's token. |
| `COMMITER_DRY_RUN` | `false` | `true` saves and prints a preview without authenticating, committing, or pushing. |
| `COMMITER_STATE_DIR` | `~/.local/state/commiter`, honoring `XDG_STATE_HOME` | Folder for plans, receipts, and resume records. |
| `GITHUB_NAME_1`, `_2`, `_3` | GitHub profile name or login | Optional author/committer name per account. |
| `GITHUB_EMAIL_1`, `_2`, `_3` | `ID+LOGIN@users.noreply.github.com` | Optional account commit email. |

Blank values use the defaults shown. The template includes every supported app setting. `.env` supports comments and quotes; values are read literally without interpolation. Unknown `COMMITER_` settings and invalid values produce an error.

## Preview and local commits

Set `COMMITER_DRY_RUN=true` in `.env`, then run the same three-message command. The output shows account percentages, each commit's message, and its files. Change it to `false` to create and publish the commits.

For local commits only, set `COMMITER_PUSH=false`. Commiter still verifies the tokens and account identities. To publish that saved batch later, change it to `true` and rerun the same three messages. Repeating a pending batch does not create duplicate commits. Finish publishing a pending batch before starting another one.

Weights control **commit counts**, while changed line counts guide chunk sizes. A 10-commit run will usually allocate about 4, 3, and 3 commits for `40,30,30`. File sizes and random variation mean the accounts' actual line totals can differ. With three or more commits, every account receives at least one. With fewer available change units, the CLI reports that some messages/accounts cannot be used.

New files, deleted files, binary files, symlinks, mode changes, and single contiguous hunks stay whole. A huge new file cannot be split into extra commits. Lower text hunks are applied first to preserve original upper line positions. The CLI does not infer code dependencies; intermediate commits may not build even when the complete change does.

## Resuming and repository behavior

If publishing fails halfway through, rerun **the same command with the same three messages**. Commiter loads its saved receipt and checks the remote branch to resume from the last published commit. It creates no additional commits during that retry. A pending batch with different messages is rejected with the original command to rerun. Normal fast-forward pushes are used; unexpected remote history stops publishing.

Planning preserves working files, the current branch, and staging. Applying checks that selected changes still match the plan, builds the entire commit chain in a temporary index, verifies the final tree, and updates the branch with an expected-HEAD check and index lock. Staged mode preserves unstaged edits. Plans keep Git snapshot objects reachable through `refs/commiter/plans/PLAN_ID`.

The root `.env` and active settings file are excluded from worktree plans. Staged changes to those files are rejected. Tokens are never copied to run records, command arguments, or remote URLs. Push credentials use an ephemeral askpass helper. A configured SSH origin is left intact while publication uses the GitHub HTTPS URL.

GitHub associates commit identities through their email address, while tokens authenticate the account pushing. Both author and committer are set to the assigned account. Check the exact noreply address under GitHub **Settings → Emails**, especially for older accounts. A custom personal email is verified through `/user/emails`, requiring **Email addresses: Read** for fine-grained tokens or `user:email` for classic tokens. See [GitHub's email reference](https://docs.github.com/en/account-and-profile/reference/email-addresses-reference).

This version operates on pending changes in a full checkout on a branch. Existing committed history, detached HEAD, unresolved merges/rebases, sparse checkouts, submodules, and GitHub Enterprise are not supported. Commits use Git `commit-tree`, are unsigned, and do not run local commit hooks. Push hooks still run. An interrupted local apply prints its receipt and expected HEAD; inspect repository state before retrying if local bookkeeping was interrupted.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Tests use disposable real Git repositories and a local bare remote. GitHub API responses are mocked; live publishing requires your real tokens and repository.

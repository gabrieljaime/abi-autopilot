# ABI Autopilot v1.8.1

[![tests](https://github.com/gabrieljaime/abi-autopilot/actions/workflows/tests.yml/badge.svg)](https://github.com/gabrieljaime/abi-autopilot/actions/workflows/tests.yml)

**English** · [Español](README.es.md)

ABI Autopilot turns GitHub issues into reviewed, tested commits using AI coding agents. You label an issue, and Autopilot:

1. creates an isolated copy of your repository for that issue (a Git *worktree*),
2. asks an AI agent (OpenAI Codex CLI or Claude Code) to implement it,
3. runs **your** test, lint and build commands,
4. asks Claude to review the change as an independent reviewer,
5. loops back to the agent to fix failures or review blockers,
6. commits the result to its own branch and reports back on the issue.

Nothing reaches your main branch until you explicitly run `integrate` (or turn on auto-integration yourself).

It runs on your own machine from a terminal. It is not a hosted service and it never stores tokens: it reuses the logins you already have in GitHub CLI, Codex CLI and Claude Code.

> [!IMPORTANT]
> **You need your own AI accounts.** Autopilot does not include any AI model. It drives the Claude Code CLI (Anthropic) and/or the OpenAI Codex CLI, logged in with **your** accounts: a Claude Pro/Max/Team plan or an Anthropic API key, and/or a ChatGPT plan or an OpenAI API key. The reviewer always uses Claude, so a Claude account is required; Codex is optional. Every implementation, fix and review uses your plan's quota or your API credit, and a single issue can take many agent calls.

> [!WARNING]
> Autopilot runs AI agents that **edit code, push branches, and comment on, label and close issues on your behalf**. Their changes can be wrong. Use it only on repositories and machines you trust, review what gets integrated, and read the [disclaimer](#license-and-disclaimer).

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Step 1 — Install the tools](#step-1--install-the-tools)
- [Step 2 — Get Autopilot](#step-2--get-autopilot)
- [Step 3 — Create your configuration](#step-3--create-your-configuration)
- [Step 4 — Adapt the checks to your project](#step-4--adapt-the-checks-to-your-project)
- [Step 5 — Verify everything](#step-5--verify-everything)
- [Step 6 — Run your first issue](#step-6--run-your-first-issue)
- [Step 7 — Integrate the result](#step-7--integrate-the-result)
- [Running continuously](#running-continuously)
- [Writing good issues](#writing-good-issues)
- [Integration vs. deployment branches](#integration-vs-deployment-branches)
- [Configuration reference](#configuration-reference)
- [Command reference](#command-reference)
- [Files and folders Autopilot creates](#files-and-folders-autopilot-creates)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [Development](#development)
- [Contact](#contact)
- [License and disclaimer](#license-and-disclaimer)

---

## How it works

Autopilot keeps track of each issue's progress with GitHub labels. You only set the first one; Autopilot moves the issue through the rest:

```text
agent:ready        you marked the issue as ready for Autopilot
agent:running      the implementer agent is writing code
agent:fix          the agent is fixing failing checks or review blockers
agent:review       Claude is reviewing the change (read-only)
agent:implemented  review + all checks PASS; committed on the issue branch
agent:integrated   merged into the integration branch, not yet deployed (only with 2 branches)
agent:done         reachable from the deployment branch — the issue is closed

agent:blocked         stopped; needs a human (also adds needs:human)
agent:waiting-quota   the AI provider hit a usage limit; Autopilot will retry later
```

The pipeline for one issue:

```text
 implement ──► targeted checks ──► fast checks ──► Claude review ──► full checks ──► commit + push branch
     ▲               │                  │                │                │
     └───────────────┴──── failure ─────┴─── FAIL ───────┴────────────────┘
                  (sent back to the agent as a "fix" loop, up to a limit)
```

- **Targeted checks**: cheap tests next to the files that changed (auto-discovered, plus any you configure).
- **Fast checks**: your usual unit tests, type checks and lint.
- **Review**: Claude reads the issue and the diff, then answers `PASS` or `FAIL` with concrete blockers. It runs in read-only mode and cannot edit files.
- **Full checks**: slow suites such as builds or end-to-end tests.
- If a check also fails on the base commit (before the change), it is treated as pre-existing and not blamed on the agent (the *baseline*).
- When the fix or review loop limit runs out, the issue is marked `agent:blocked` + `needs:human` with an explanation.

The result is a branch named `agent/issue-<N>-<title-slug>` pushed to `origin`, plus a comment on the issue summarizing what passed.

---

## Requirements

| Tool | Needed for | Minimum |
| --- | --- | --- |
| Python | Running Autopilot | 3.11 |
| Git | Worktrees, commits, pushes | 2.20+ |
| GitHub CLI (`gh`) | Reading issues, labels, comments | any recent version |
| OpenAI Codex CLI (`codex`) | Implementer (default) | any recent version |
| Claude Code CLI (`claude`) | Reviewer (always), optional implementer | any recent version |
| Node.js + npm | Only if *your project's* checks use them, or to install the CLIs above | 18+ |

You also need:

- A GitHub repository where you can push branches and edit issues.
- A local clone of that repository.
- **A Claude account** (Claude Pro/Max/Team plan or Anthropic API key) — always required, because Claude is the reviewer.
- **Optionally an OpenAI account** (ChatGPT plan or OpenAI API key) to use Codex as implementer. Without it, set `"implementer": "claude"`.
- **Every agent call consumes your quota or API credit.** Autopilot waits and retries when a quota runs out, but it cannot create more.

Works on Windows, macOS and Linux. The examples use PowerShell on Windows; on macOS/Linux use the same commands with `\` line continuations and POSIX paths.

---

## Step 1 — Install the tools

Skip any tool you already have. Check with the `--version` command shown in each section.

### 1.1 Python

- **Windows**: install from <https://www.python.org/downloads/> and tick **"Add python.exe to PATH"**. Or: `winget install Python.Python.3.12`
- **macOS**: `brew install python@3.12`
- **Linux**: use your package manager, e.g. `sudo apt install python3 python3-venv`

```powershell
python --version    # macOS/Linux may need: python3 --version
```

Autopilot has **no Python dependencies** to install; it only uses the standard library.

### 1.2 Git

- **Windows**: <https://git-scm.com/download/win> or `winget install Git.Git`
- **macOS**: `xcode-select --install` or `brew install git`
- **Linux**: `sudo apt install git`

```powershell
git --version
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

Autopilot commits in your name, so Git needs a name and email.

### 1.3 GitHub CLI (`gh`)

Install: <https://cli.github.com/> — or `winget install GitHub.cli` / `brew install gh` / see the site for Linux.

Log in:

```powershell
gh auth login
```

Answer the prompts: **GitHub.com** → **HTTPS** → **Yes** (authenticate Git with your GitHub credentials) → **Login with a web browser**.

Then let Git use the same login for pushes:

```powershell
gh auth setup-git
gh auth status
```

`gh auth status` should say you are logged in and list the `repo` scope. Autopilot needs it to read issues, create labels, comment, close issues and push branches.

> If your repository remote uses SSH (`git@github.com:...`), pushes use your SSH key instead. Make sure `git push` works from your clone before continuing.

### 1.4 Node.js and npm

Needed to install the two agent CLIs below (and by your project if it is a JavaScript project).

- **Windows**: `winget install OpenJS.NodeJS.LTS`
- **macOS**: `brew install node`
- **Linux**: <https://nodejs.org/en/download> or [nvm](https://github.com/nvm-sh/nvm)

```powershell
node --version
npm --version
```

### 1.5 OpenAI Codex CLI (`codex`)

The default implementer.

```powershell
npm install -g @openai/codex
codex --version
```

Log in once:

```powershell
codex
```

Choose **Sign in with ChatGPT** (uses your ChatGPT plan) or provide an OpenAI API key, then exit. Autopilot runs Codex non-interactively with `codex exec` in `workspace-write` sandbox mode, so it can only write inside the issue's worktree.

> Don't want Codex? Set `"implementer": "claude"` in the config (see [Choosing the implementer](#choosing-the-implementer)) and skip this step.

### 1.6 Claude Code CLI (`claude`)

Always used as the reviewer; optionally also as implementer.

```powershell
npm install -g @anthropic-ai/claude-code
claude --version
```

Log in once by starting it and following the prompts (Claude account or Anthropic API key), then exit:

```powershell
claude
```

Autopilot runs Claude headless (`claude -p`). The reviewer uses `--permission-mode plan` (read-only). When Claude is the implementer it uses `acceptEdits` so it can edit files without asking.

---

## Step 2 — Get Autopilot

Choose one of the two options.

### Option A — Install the command (recommended)

[pipx](https://pipx.pypa.io/) installs Python command-line tools in their own isolated environment:

```powershell
python -m pip install --user pipx
python -m pipx ensurepath          # then close and reopen the terminal
pipx install git+https://github.com/gabrieljaime/abi-autopilot.git
abi-autopilot version
```

To upgrade later: `pipx upgrade abi-autopilot`.

Autopilot keeps its configuration (`config.local.json`) and logs (`runtime/`) in the folder **you run it from**, called the *home* folder. Create one folder per project you automate and always run the commands there:

```powershell
mkdir C:\autopilot\my-project
cd C:\autopilot\my-project
```

You can also set the environment variable `ABI_AUTOPILOT_HOME` to that folder and run the command from anywhere.

### Option B — Run from a clone

```powershell
git clone https://github.com/gabrieljaime/abi-autopilot.git
cd abi-autopilot
python autopilot.py version
```

Here the clone itself is the home folder. **In the rest of this guide, replace `abi-autopilot` with `python autopilot.py`.**

Either way, you should see the version, copyright, license and account notice.

---

## Step 3 — Create your configuration

From your home folder, run:

```powershell
abi-autopilot init
```

It asks a few questions, suggesting answers it reads from your repository (press Enter to accept them):

```text
Path to your local clone of the target repository: C:\code\my-project
GitHub repository (owner/name) [my-user/my-project]:
Integration branch (where approved changes are merged) [main]:
Deployment branch (Enter = same as integration) [main]:

Detected in the target repository:
  - Node.js (npm) in frontend/: test, typecheck, lint, build, test:e2e
  - Python in backend/: pytest
  fast        backend-pytest (python -m pytest -q), frontend-vitest (npm test), ...
  full        frontend-build (npm run build), frontend-playwright (npm run test:e2e)

Write config.local.json with these settings? [Y/n]
```

**Stack detection** looks at the repository root and its first-level folders and builds the checks from what each project declares:

| Project | Detected from | Checks |
| --- | --- | --- |
| Node.js | `package.json` scripts; npm, pnpm or yarn from the lockfile | `test`, `typecheck`/`type-check`, `lint`, `build`, `test:e2e`/`e2e` |
| Python | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`, `Pipfile` | `pytest`, `ruff`, `mypy` (when configured) |
| Go | `go.mod` | `go vet`, `go test`, `go build` |
| Rust | `Cargo.toml` | `cargo test`, `cargo check`, `cargo build` |

It also reads `.nvmrc` / `.python-version` for the expected versions. The result is a starting point: always review it in Step 4.

For scripts, pass everything as options (the GitHub name and the branch are inferred from the `origin` remote when omitted):

```powershell
abi-autopilot init --repo "C:\code\my-project" --non-interactive
```

| Option | Meaning |
| --- | --- |
| `--repo` | Path to your **local clone** of the target project. |
| `--repo-slug` | The GitHub name, `owner/repository`. Default: read from the `origin` remote. |
| `--base-branch` | Branch where approved changes get integrated, e.g. `main` or `develop`. Default: `origin`'s default branch. |
| `--deployment-branch` | *(optional)* Branch that actually gets deployed, if different. Issues close only when their work reaches it. See [Integration vs. deployment branches](#integration-vs-deployment-branches). |
| `--non-interactive` | Never ask; fail if something cannot be inferred. |
| `--no-detect` | Skip detection and write the example `frontend/` + `backend/` checks instead. |
| `--force` | Overwrite an existing `config.local.json`. |

This creates:

- `config.local.json` — your personal configuration (ignored by Git; never commit it).
- `runtime/` — local state and logs (ignored by Git).

It also decides where to put working copies, **next to** your project, not inside it:

```text
C:\code\my-project                      ← your repository (untouched until you integrate)
C:\code\my-project-autopilot-worktrees  ← one folder per issue (issue-12, issue-13, …)
C:\code\my-project-autopilot-deps       ← shared npm dependency cache
```

---

## Step 4 — Review the checks

**This is the most important step.** Autopilot only approves what your checks verify. Open `config.local.json` and look at the `validation` section: remove checks you don't want, add missing ones (for example a custom script, or a test command detection didn't recognize), and adjust timeouts. If nothing was detected, the section is empty and `doctor` warns you; add your commands before running issues.

Python checks run with the `python` found on `PATH`. Run Autopilot from the target project's virtual environment, or write the interpreter's full path in the command (e.g. `C:\code\my-project\.venv\Scripts\python -m pytest -q`).

Each check looks like:

```json
{ "name": "unit-tests", "cwd": "", "command": "npm test", "when": "always", "timeout": 1800 }
```

| Field | Meaning |
| --- | --- |
| `name` | Label shown in logs. Names containing `vitest`, `pytest`, `playwright`/`e2e` get the worker limits from `validation_workers`. |
| `cwd` | Folder, relative to the repository root, where the command runs. `""` = the root. |
| `command` | Any shell command. Exit code 0 = pass. |
| `when` | `always`, `changed:<path>` (only if the diff touches that path, e.g. `changed:src/`), `docs_changed`, and the legacy `frontend_changed` / `backend_changed`. |
| `timeout` | Seconds before the command is killed. |

The phases:

| Phase | Runs | Typical content |
| --- | --- | --- |
| `targeted` | after every implement/fix | a few quick tests (Autopilot also auto-discovers tests next to changed files in `backend/` and `frontend/`) |
| `fast` | after targeted passes | unit tests, type check, lint |
| `full` | after the review passes | build, end-to-end tests |
| `batch_fast` / `batch_full` | during `integrate-done` | cheap per-issue checks / one final full gate for the whole batch |

Also review:

- `protected_paths` — files the agents must never touch (default: `.env`). The issue is blocked if one is modified. Add secrets, data folders or generated files here.
- `workspace_bootstrap` — how dependencies get installed in each worktree (see below).
- `dependency_cache.done_artifacts` and `integration.generated_artifacts` — disposable build output to delete (e.g. `dist`, `coverage`).

If you prefer to write the checks by hand, here are two complete examples.

### Example: a single Node.js project

```json
"workspace_bootstrap": [
  {
    "name": "npm-ci", "cwd": "", "strategy": "npm_shared_cache",
    "command": "npm ci --no-audit --no-fund",
    "manifest": "package.json", "lockfile": "package-lock.json",
    "key_files": ["package-lock.json", "package.json"],
    "cache_probe": "node_modules/.package-lock.json",
    "link_path": "node_modules",
    "if_missing": "node_modules/.package-lock.json",
    "rerun_if_changed": ["package.json", "package-lock.json"],
    "timeout": 2400
  }
],
"validation": {
  "non_llm_retries": 1,
  "targeted": [],
  "fast": [
    { "name": "vitest", "cwd": "", "command": "npm test",          "when": "always", "timeout": 1800 },
    { "name": "lint",   "cwd": "", "command": "npm run lint",      "when": "always", "timeout": 1200 }
  ],
  "full": [
    { "name": "build",  "cwd": "", "command": "npm run build",     "when": "always", "timeout": 1800 }
  ],
  "batch_fast": [
    { "name": "lint",   "cwd": "", "command": "npm run lint",      "when": "always", "timeout": 1200 }
  ],
  "batch_full": [
    { "name": "vitest", "cwd": "", "command": "npm test",          "when": "always", "timeout": 1800 },
    { "name": "build",  "cwd": "", "command": "npm run build",     "when": "always", "timeout": 1800 }
  ]
}
```

### Example: a single Python project

```json
"workspace_bootstrap": [],
"validation": {
  "non_llm_retries": 1,
  "targeted": [],
  "fast": [
    { "name": "pytest", "cwd": "", "command": "python -m pytest -q", "when": "always", "timeout": 3600 },
    { "name": "ruff",   "cwd": "", "command": "ruff check .",        "when": "always", "timeout": 600 }
  ],
  "full": [],
  "batch_fast": [],
  "batch_full": [
    { "name": "pytest", "cwd": "", "command": "python -m pytest -q", "when": "always", "timeout": 3600 }
  ]
}
```

Set `"dependency_cache": { "enabled": false, ... }` if you don't use npm.

> **Your project's `.gitignore` must ignore `node_modules/`, build output and coverage files.** Autopilot commits with `git add -A` inside the worktree, so anything not ignored and not cleaned up would end up in the commit.

---

## Step 5 — Verify everything

```powershell
abi-autopilot doctor
```

Every line should say `PASS`. `WARN` lines are advice (e.g. a check folder that doesn't exist, or a Node version different from what you declared in `toolchain`). Fix any `FAIL` before continuing.

To also confirm that the agents answer (this uses a tiny bit of quota):

```powershell
abi-autopilot doctor --probe-agents
```

Create the labels Autopilot uses in your GitHub repository (safe to run again):

```powershell
abi-autopilot bootstrap-labels
```

See what Autopilot sees:

```powershell
abi-autopilot observe
```

---

## Step 6 — Run your first issue

1. **Create an issue on GitHub** with a clear goal, the files involved and acceptance criteria. Start with something small. The agents read only the title and body (not comments), so read [Writing good issues](#writing-good-issues) first.

2. **Mark it ready**, choosing a risk level (`low`, `medium` or `high`):

   ```powershell
   abi-autopilot mark-ready 123 --risk low
   ```

3. **Dry run** — checks eligibility and creates the worktree, but calls no agent and changes no labels:

   ```powershell
   abi-autopilot run --issue 123 --dry-run
   ```

4. **Run it for real**:

   ```powershell
   abi-autopilot run --issue 123
   ```

   This can take from a few minutes to an hour depending on your checks. You'll see the agent's progress and every check's output in the terminal. When it finishes, it prints a JSON summary.

5. **Look at the result**:
   - The issue gets a comment and the `agent:implemented` label.
   - Branch `agent/issue-123-...` is pushed to GitHub. Open a compare view or PR to read the diff.
   - The full logs, prompts and agent output are in `runtime/runs/issue-123/<timestamp>/`.

If it ends in `agent:blocked`, the issue comment explains why. Fix the cause (clarify the issue, fix a broken check, …) and continue without losing work:

```powershell
abi-autopilot resume --issue 123
abi-autopilot resume --issue 123 --stage review   # force a specific stage: targeted|fast|review|full
```

---

## Step 7 — Integrate the result

When you are happy with the change:

```powershell
abi-autopilot integrate --issue 123
```

Autopilot re-validates the approved commit in a temporary worktree on top of the latest `base_branch`, checks nobody pushed to the base branch meanwhile, pushes, and (if integration = deployment branch) closes the issue.

| Option | Effect |
| --- | --- |
| `--no-close` | Push but leave the issue open. |
| `--continue` | Continue after you manually resolved a conflict in the preserved worktree. |

**Several issues at once:**

```powershell
abi-autopilot integrate-done                                  # show the plan only
abi-autopilot integrate-done --issues 123 124 125             # plan with an explicit order
abi-autopilot integrate-done --issues 123 124 125 --execute   # do it
```

In batch mode every change is stacked in one temporary worktree, cheap checks run per issue, the full suite runs **once** at the end, and nothing is pushed unless everything is green.

If there's a conflict, Autopilot stops and leaves the worktree on disk for you to resolve. It never force-pushes, resets, stashes or rebases.

---

## Running continuously

The daemon repeatedly looks for issues labeled `agent:ready` (or waiting for quota) and processes them, one at a time by default:

```powershell
abi-autopilot daemon          # loop forever, every autopilot.poll_seconds (default 120)
abi-autopilot daemon --once   # one cycle, then exit — good for schedulers
```

Stop it with `Ctrl+C`; the saved state is kept.

- **Windows Task Scheduler**: create a task that runs `abi-autopilot daemon --once` every 10–15 minutes with *Start in* set to your home folder. From a clone you can run `powershell -File C:\path\to\abi-autopilot\scripts\run-once.ps1` instead (`scripts\run-daemon.ps1` starts the endless loop).
- **cron (macOS/Linux)**: `*/15 * * * * cd ~/autopilot/my-project && ~/.local/bin/abi-autopilot daemon --once >> runtime/daemon.log 2>&1`

### Running several issues in parallel

Set `autopilot.max_parallel` above `1` to let the daemon work on several issues at once:

```json
"autopilot": { "max_parallel": 3 }
```

- Each issue runs as its own `abi-autopilot run --issue N` process, with its output in `runtime/daemon/issue-N-<timestamp>.log`. The daemon terminal only shows when each one starts and finishes.
- Git operations on the shared repository (fetch, creating worktrees, pushes) and the baseline checks for the same base commit are serialized with locks in `<worktree_root>/.locks/`, so parallel runs don't corrupt each other.
- With `--once`, the daemon starts up to `max_parallel` issues, waits until all of them finish and exits; the rest of the queue is picked up next time.

Before raising it, check that **your checks can run concurrently**: end-to-end tests or dev servers that bind a fixed port, shared test databases or a shared `.env` will collide between issues. Also remember that N parallel issues use roughly N times the AI quota and CPU/RAM. Start with `2`.

With `"auto_ready": true`, the daemon also marks new issues (those with no `agent:*` label) as ready automatically, skipping epics, issues with open dependencies and those labeled `needs:human` / `needs:product`.

By default the daemon **never integrates or closes** anything; you do that with `integrate`. See `integration.auto_integrate` to change it.

---

## Writing good issues

The issue is the only instruction the agents get, so its quality decides the result.

### What the agents see

- **Only the title and the body.** Comments are *not* sent to the agents. To add or correct information, **edit the issue body**; the body is re-read at every step, so an edit also reaches a run that is already in progress.
- The implementer, the fixer and the reviewer all read the same text. The reviewer rejects the change if any acceptance criterion is not met, so the criteria are also your quality bar.
- **The body is sent to the AI provider** (Anthropic, and OpenAI if you use Codex). Never paste secrets, credentials, tokens, customer data or internal URLs you wouldn't share with them.

### What makes a good issue

- **Small**: one change you could review as a single pull request. Split big features into several issues and link them with dependencies; use an `[EPIC]` issue to group them.
- **Located**: name the files, folders, functions, screens or endpoints involved, and describe current vs. expected behavior. The agent searches the code, but pointers save time and quota.
- **Bounded**: an *Out of scope* list stops the agent from "improving" things you didn't ask for.
- **Checkable**: acceptance criteria describe observable behavior, ideally something a test can prove. Avoid "make it better" or "clean up".
- **Decided**: if a product or security decision is still open, decide it in the issue or add `needs:product` / `needs:human`. The agent is instructed to stop and report instead of guessing.

### Template

Use the Markdown template in [`examples/ISSUE_TEMPLATE_AUTOPILOT.md`](examples/ISSUE_TEMPLATE_AUTOPILOT.md), or, better, the GitHub **issue form** in [`examples/autopilot-issue-form.yml`](examples/autopilot-issue-form.yml): copy it into your project as `.github/ISSUE_TEMPLATE/autopilot.yml` and GitHub will offer an "Autopilot task" form under *New issue*, with the right sections and reminders.

```markdown
## Goal
## Context          (files, current vs. expected behavior)
## Scope
## Out of scope
## Acceptance criteria
## Dependencies
## Verification
```

### Example

A good issue:

```markdown
Title: Add CSV export to the orders page

## Goal
Users can download the orders they are viewing as a CSV file.

## Context
Orders page: frontend/src/pages/orders/OrdersTable.tsx. Data comes from
GET /api/orders (backend/app/routes/orders.py). Today there is no export.

## Out of scope
- Excel/PDF formats
- Changes to the orders API

## Acceptance criteria
- [ ] An "Export CSV" button above the table downloads orders.csv
- [ ] The file has the visible columns, in the same order, and respects active filters
- [ ] A unit test covers the CSV generation

## Dependencies
#41
```

A bad one: *"Orders page improvements — add export and fix the slow loading, see comments"*. It mixes two changes, has no location or criteria, and points to comments the agent never reads.

### Labels, dependencies and risk

- **Risk comes from a label, not from the text.** Set it with `abi-autopilot mark-ready <N> --risk low|medium|high` (or add a `risk:*` label). Without one, the issue counts as `medium`. Medium and high risk require the full checks at integration time; only the risks in `integration.allowed_risks` can be auto-integrated.
- **Dependencies**: references under a `## Dependencies` (or `## Dependencias`) heading, or on a `Depends-On: #12` line, must be **closed** before the issue runs. A reference followed by `optional`, `recommended`, `soft`, `nice-to-have`, `opcional` or `recomendada` is not blocking. References elsewhere in the body (for example in *Context*) are ignored. Named references like `UX-12` work if you list the prefix in `dependency_ref_prefixes` and an issue has `[UX-12]` in its title.
- **Epics**: issues with `[EPIC]` in the title are skipped (they're for grouping).
- **Human decisions**: add `needs:human` or `needs:product` to keep Autopilot away. If the agent finds an ambiguous product or security decision, it is told to stop and report it instead of guessing.

---

## Integration vs. deployment branches

Many teams merge into a branch like `develop` and deploy from `main`. Tell Autopilot:

```powershell
abi-autopilot init --repo ... --repo-slug ... --base-branch develop --deployment-branch main
```

Then:

- `integrate` merges into `develop` and labels the issue `agent:integrated`, but **keeps it open**.
- Once you promote `develop` into `main` your usual way, run:

  ```powershell
  abi-autopilot release-audit             # what is and isn't deployed, and branch drift
  abi-autopilot release-audit --promote   # close issues whose work is now in main
  abi-autopilot release-status --issue 123
  ```

Autopilot considers the work deployed if the commit is reachable from `main`, or if Git reports an equivalent patch there (cherry-picks/rebases). If `main` only contains commits with the same message prefix but a different patch (conflicts were resolved during promotion), it shows `message-match` and recommends checking the diff by hand.

If both branches keep accumulating their own commits, `release-audit` warns about **drift**. With `release.block_close_on_drift: true`, drift also blocks `--promote`.

---

## Configuration reference

`config.local.json` is plain JSON. [`config.example.json`](config.example.json) shows every option with its default. After upgrading Autopilot, merge in new defaults without losing your changes:

```powershell
abi-autopilot upgrade-config   # a backup is saved as config.local.json.pre-v<version>.bak
```

### Top level

| Key | Default | Meaning |
| --- | --- | --- |
| `repo_path` | from `init` | Local clone of the target repository. |
| `repo_slug` | from `init` | `owner/repository` on GitHub. |
| `base_branch` | from `init` | Integration branch. |
| `deployment_branch` | = `base_branch` | Branch that gets deployed. |
| `worktree_root` | `<repo>-autopilot-worktrees` | Where per-issue worktrees go. |
| `runtime_dir` | `runtime` | State and logs, relative to the Autopilot folder. |
| `dependency_ref_prefixes` | `[]` | Named dependency prefixes, e.g. `["UX", "API"]`. |
| `protected_paths` | `[".env", "backend/data/"]` | Paths agents may not modify (listed in their prompts too). |
| `process_env` | `{"PLAYWRIGHT_HTML_OPEN": "never"}` | Environment variables for every command Autopilot runs. |

### `autopilot`

| Key | Default | Meaning |
| --- | --- | --- |
| `poll_seconds` | `120` | Daemon wait between cycles. |
| `max_parallel` | `1` | Issues the daemon works on at once. See [Running several issues in parallel](#running-several-issues-in-parallel). |
| `max_fix_loops` | `3` | Max times the agent is asked to fix failing checks/review before blocking. |
| `max_review_loops` | `2` | Max review FAILs before blocking. |
| `skip_epics` | `true` | Ignore issues with `[EPIC]` in the title. |
| `auto_ready` | `false` | Daemon labels new issues `agent:ready` automatically. |
| `auto_ready_default_risk` | `medium` | Risk given by `auto_ready`. |
| `implementer` | `codex` | `codex` or `claude`. |
| `implementer_fallback` | `null` | The other agent to use if the first is out of quota, or `null`. |

#### Choosing the implementer

```json
"autopilot": { "implementer": "claude", "implementer_fallback": null }
```

With only Claude installed, this is all you need. With both, `"implementer": "codex", "implementer_fallback": "claude"` switches to Claude when Codex hits its limit.

### `codex`

| Key | Default | Meaning |
| --- | --- | --- |
| `command` | `codex` | Executable name or full path. |
| `sandbox` | `workspace-write` | Codex sandbox mode. |
| `model` | `null` | Model for implementing; `null` = Codex's default. |
| `fix_model` | `null` | Model for fix loops; `null` = same as `model`. |
| `extra_args` | `["--ephemeral"]` | Extra arguments for `codex exec`. |

### `claude`

| Key | Default | Meaning |
| --- | --- | --- |
| `command` | `claude` | Executable name or full path. |
| `model` | `sonnet` | Reviewer model (alias or full model ID). |
| `permission_mode` | `plan` | Reviewer permissions. Keep `plan` (read-only). |
| `max_turns` | `8` | Reviewer turn budget per call. |
| `resume_turns` / `max_turn_resumes` | `4` / `2` | Extra turns if the reviewer runs out, without counting as a new review. |
| `max_diff_chars` | `60000` | Largest diff sent to the reviewer. |
| `implement_model` | `null` | Model when Claude implements; `null` = `model`. |
| `implement_max_turns` | `8` | Turn budget when Claude implements. |
| `implement_permission_mode` | `acceptEdits` | Lets Claude edit files headless. |

### `availability` (quota and transient errors)

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Wait and retry instead of failing on quota/rate limits. |
| `quota_fallback_wait_seconds` | `900` | Wait when the provider doesn't say when the quota resets. |
| `quota_reset_grace_seconds` | `120` | Extra margin after a reported reset time. |
| `max_wait_hours` | `24` | Give up (block) after waiting this long. |
| `transient_backoff_seconds` | `[60,120,300,600,900]` | Waits between retries for network/server errors. |

### `validation`

| Key | Meaning |
| --- | --- |
| `targeted`, `fast`, `full`, `batch_fast`, `batch_full` | Lists of checks (see [Step 4](#step-4--adapt-the-checks-to-your-project)). |
| `non_llm_retries` | Re-run a failed check this many times before involving the agent (absorbs flaky tests). Default `1`. |
| `targeted_backend_no_cov` | Disable the global coverage threshold on the small auto-discovered pytest subset. Default `true`. |

### `validation_workers`

Parallel workers added to checks whose `name` contains `vitest` (default `4`), `pytest` (`1`, >1 requires pytest-xdist) or `playwright`/`e2e` (`1`).

### `baseline`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Also run failing checks on the base commit to spot pre-existing failures. |
| `required` | `false` | Block if the baseline cannot be computed. |
| `compare_phases` | `["targeted","fast"]` | Phases compared against the baseline. |
| `mode` | `no_new_failures` | Only failures the change introduced count. |

### `workspace_bootstrap`

Steps that prepare each worktree before checks run (worktrees don't include ignored files like `node_modules`).

| Key | Meaning |
| --- | --- |
| `name`, `cwd`, `command`, `timeout`, `when` | As in checks. |
| `strategy` | `npm_shared_cache` = install once per unique lockfile in `dependency_cache.root` and link it into each worktree. Omit for a plain command. |
| `manifest`, `lockfile`, `key_files` | Files whose content defines the cache key. |
| `cache_probe` | File that must exist for the cache to count as complete. |
| `link_path` | Where the shared folder is linked in the worktree. |
| `if_missing` | Only run if this file is missing (plain strategy). |
| `rerun_if_changed` | Re-run if the diff touches these files. |

### `dependency_cache`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Use the shared npm cache. |
| `root` | `<repo>-autopilot-deps` | Cache location (outside your repository). |
| `min_free_gb` | `8` | `doctor` fails below this much free disk space. |
| `lock_timeout_seconds` / `stale_lock_seconds` | `1800` / `7200` | Locking between concurrent installs. |
| `cleanup_done_artifacts` | `true` | Delete `done_artifacts` from the worktree when an issue finishes. |
| `done_artifacts` | build/test output | Paths to delete (never tracked files). |

### `integration`

| Key | Default | Meaning |
| --- | --- | --- |
| `auto_integrate` | `false` | Let `run`/`daemon` fast-forward the base branch after success. |
| `auto_close` | `false` | Let `run`/`daemon` close issues after auto-integration. |
| `allowed_risks` | `["low"]` | Risks allowed to auto-integrate. |
| `push_issue_branch` | `true` | Push `agent/issue-N-...` to `origin`. |
| `comment_updates` | `true` | Post progress/result comments on the issue. |
| `manual_close_after_success` | `true` | `integrate` closes the issue after a successful push. |
| `validate_targeted` / `validate_fast` | `true` | Phases re-run at integration time. |
| `full_for_risks` | `["medium","high"]` | Risks that also need the full phase at integration. |
| `full_if_frontend_changed` | `true` | Force full checks if `frontend/` changed. |
| `code_paths` | `[]` | Path prefixes that count as code. Empty = everything except `docs/` and `*.md`. Docs-only changes skip validation. |
| `cleanup_generated` / `generated_artifacts` | `true` / list | Disposable output removed before committing. |
| `sync_clean_local_base_after_push` | `true` | Fast-forward your local base branch after pushing, if it is clean. |

### `batch_validation`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Use the batch strategy in `integrate-done`. |
| `full_suite_once_at_end` | `true` | One final full gate for the whole batch. |
| `close_only_after_final_gate` | `true` | Close nothing until the final gate passes. |
| `force_full_for_high_risk` | `true` | Run full checks right after each high-risk issue. |

### `release`

| Key | Default | Meaning |
| --- | --- | --- |
| `require_deployment_branch` | `true` | Close only when work reaches `deployment_branch`. |
| `integrated_label` | `agent:integrated` | Label for integrated-but-not-deployed. |
| `drift_warn_commits` | `1` | Warn when both branches have at least this many exclusive commits. |
| `block_close_on_drift` | `false` | Drift blocks `release-audit --promote`. |
| `audit_limit` | `1000` | Closed issues checked by `release-audit`. |

### `toolchain`

| Key | Default | Meaning |
| --- | --- | --- |
| `expected_python_major_minor` | `""` | e.g. `"3.11"`; `doctor` warns if the active Python differs. |
| `expected_node_major` | `null` | e.g. `20`; `doctor` warns if Node differs. |
| `strict` | `false` | Turn those warnings into failures. |

---

## Command reference

All commands: `abi-autopilot <command>` (or `python autopilot.py <command>` from a clone), run from your home folder. Add `--help` to any command for its options.

| Command | What it does | Changes |
| --- | --- | --- |
| `version` | Show version, copyright and license. | Nothing. |
| `init` | Create `config.local.json` for a target repository. | Writes the config and `runtime/`. |
| `upgrade-config` | Merge new defaults into your config. | Updates the config, keeps a backup. |
| `doctor [--probe-agents]` | Check tools, logins, repository, branch, disk and config. | Nothing (may create the cache folder). `--probe-agents` uses a little quota. |
| `bootstrap-labels` | Create/update the `agent:*`, `risk:*` and `needs:*` labels. | GitHub labels. |
| `observe` | List ready, blocked, implemented issues and release state. | Nothing (unless `auto_ready` is on). |
| `mark-ready <N> [--risk low\|medium\|high]` | Queue an issue. | Labels. |
| `auto-ready` | Queue every eligible unlabeled open issue. | Labels. |
| `run --issue <N> [--dry-run] [--force] [--resume-existing]` | Full pipeline for one issue. `--force` ignores the ready label and eligibility. | Worktree, branch push, labels, comments. |
| `resume --issue <N> [--stage S]` | Continue a blocked/interrupted issue keeping its work. | Same as `run`. |
| `daemon [--once]` | Process the queue continuously. | Same as `run`. |
| `cleanup --issue <N>` | Delete a finished issue's worktree (only if clean; the branch is kept). | Local files. |
| `integrate --issue <N> [--no-close] [--continue]` | Validate and push one implemented issue to the base branch. | Base branch, labels, may close. |
| `integrate-done [--issues N...] [--execute] [--no-close]` | Plan or run a batch integration. | Only with `--execute`. |
| `release-audit [--promote]` | Compare integration vs deployment branch, find closed-but-not-deployed issues. | Only with `--promote` (closes delivered issues). |
| `release-status --issue <N>` | Delivery state of one issue with evidence. | Nothing. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `2` | Configuration/command error, or `doctor`/`cleanup` failed. |
| `3` | `run`/`resume` ended blocked. |
| `4` | `integrate`/`integrate-done` blocked or hit a conflict. |
| `5` | `release-audit --promote` blocked by branch drift. |
| `6` | `release-audit` found closed issues not present in the deployment branch. |
| `130` | Interrupted with `Ctrl+C` (state preserved). |

---

## Files and folders Autopilot creates

| Location | Contents | In Git? |
| --- | --- | --- |
| `config.local.json` (+ `*.bak` backups), in the home folder | Your configuration. | Ignored. |
| `runtime/`, in the home folder | Run state, logs, prompts, diffs, agent output, baselines, integration state, parallel daemon logs. **May contain private issue text and code.** | Ignored. |
| `<repo>-autopilot-worktrees/` | One working copy per issue, plus `.locks/` for parallel runs. Outside both repositories. | Not in any repository. |
| `<repo>-autopilot-deps/` | Shared `node_modules` installs, keyed by lockfile hash. Outside both repositories. | Not in any repository. |
| `prompts/`, in the home folder *(optional)* | Your own versions of `implementer.md`, `fixer.md`, `reviewer.md`; each one found here replaces the built-in prompt. Copy the originals from [`abi_autopilot/prompts/`](abi_autopilot/prompts/). | Up to you. |
| `__pycache__/` | Python bytecode. | Ignored. |

Caches and worktrees live **next to** your project by default, so they can't be committed by accident. If you move `worktree_root` or `dependency_cache.root`, keep them outside both the Autopilot folder and your project; the `.gitignore` also ignores `*-autopilot-worktrees/` and `*-autopilot-deps/` as a safety net.

To free disk space, delete finished worktrees with `cleanup --issue <N>` and old folders inside `<repo>-autopilot-deps/npm/` when no run is active.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `config.local.json does not exist` | Run `init` first (Step 3), and run every command from the same home folder. |
| `abi-autopilot: command not found` | Run `python -m pipx ensurepath` and open a new terminal, or use `python autopilot.py` from a clone. |
| `doctor`: `gh auth FAIL` | `gh auth login`, then `gh auth setup-git`. |
| `doctor`: `codex` / `claude` not found | Install the CLI (Step 1). If installed elsewhere, set `codex.command` / `claude.command` to the full path (on Windows often `...\npm\codex.cmd`). |
| `doctor`: `origin/base FAIL` | The `base_branch` doesn't exist on `origin`, or `git fetch` fails. Check `git -C <repo> fetch origin`. |
| `WARN cwd '...' ... does not exist` | A check points to a folder that isn't in the repository. Fix it in Step 4. |
| `WARN No validation checks are configured` | Detection found nothing. Add your commands to `validation` (Step 4). |
| `#N is not labeled agent:ready` | `mark-ready N` first, or use `resume`. |
| `Issue not eligible: open dependencies` | Close the dependencies, or mark them `optional` in the issue. |
| Issue stuck in `agent:waiting-quota` | The provider's usage limit was hit. The daemon retries automatically; or configure `implementer_fallback`. |
| `agent:blocked` after fix loops | Read the issue comment and `runtime/runs/issue-N/...`. Clarify the issue or fix the environment, then `resume`. |
| `The reviewer modified the working tree` | The reviewer must be read-only. Keep `claude.permission_mode` = `plan`. |
| A protected path was modified | The agent touched something in `protected_paths`. Adjust the issue or the list, then `resume`. |
| `Cannot find module` errors | Dependency install problem, not a code bug. Check the `workspace_bootstrap` log under `runtime/runs/...`. |
| Integration conflict | Resolve the files in the worktree path printed, then `integrate --issue N --continue`. |
| Characters like `·` or `→` look garbled on Windows | Run `chcp 65001` or use Windows Terminal. |


---

## Security

- Run it only on trusted machines and repositories. The agents execute commands in your project.
- Never put tokens, passwords or API keys in `config.local.json`. Authentication stays in each CLI.
- Don't publish `runtime/`: it contains issue text, prompts, diffs and agent output.
- Safe defaults: no auto-integration, no auto-close, no force push, no `reset --hard`, stash, rebase or `git clean`. Conflicts are preserved for you. The reviewer is read-only.

See [`SECURITY.md`](SECURITY.md) for the full model and how to report vulnerabilities.

---

## Development

The test suite needs no network or external services:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q autopilot.py abi_autopilot
```

```text
autopilot.py              run from a clone: python autopilot.py <command>
pyproject.toml            package metadata; installs the abi-autopilot command
abi_autopilot/            implementation (cli.py is the command line)
abi_autopilot/prompts/    instructions given to the implementer, fixer and reviewer
scripts/                  PowerShell helpers
examples/                 issue template
tests/                    unittest suite
.github/workflows/        CI: tests on Windows, macOS and Linux
config.example.json       full configuration reference
```

To work on Autopilot itself with the command pointing at your checkout: `pip install -e .`

Changes are listed in [`CHANGELOG.md`](CHANGELOG.md). Contributions are welcome: open an issue or pull request.

---

## Contact

Gabriel Jaime — <gabrielsjaime@gmail.com>

For security issues, write privately to that address instead of opening a public issue.

---

## License and disclaimer

Copyright (c) 2026 Gabriel Jaime. Released under the [MIT License](LICENSE): you may use, copy, modify and redistribute it, provided the copyright notice and license are kept.

ABI Autopilot runs AI agents that modify code, push branches, and comment on or close issues on your behalf. The changes they produce may contain errors and must be reviewed before deployment. The software is provided "as is", without warranty of any kind, and the authors are not liable for any damage arising from its use. You need your own Claude and/or OpenAI Codex accounts, and you pay for what the agents use under those accounts. You are responsible for reviewing what gets integrated and for complying with the terms of service of GitHub, OpenAI and Anthropic. Codex, Claude and GitHub are trademarks of their respective owners; this project is not affiliated with or endorsed by them.

Every comment Autopilot posts on GitHub ends with a footer carrying the version, copyright, license and this disclaimer. `abi-autopilot version` prints the same notice.

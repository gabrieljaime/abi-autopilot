# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
"""Command-line interface.

Entry points:
- `python autopilot.py <command>` from a source checkout: the checkout folder
  holds `config.local.json` and `runtime/`.
- `abi-autopilot <command>` / `python -m abi_autopilot <command>` when
  installed: the *home* folder is `$ABI_AUTOPILOT_HOME`, or the current
  directory.
"""
from __future__ import annotations
import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

from . import __version__
from .notice import cli_notice
from .config import load_config, write_initial_config, upgrade_config, ConfigError
from .shell import which, run_capture, CommandError
from . import github as ghx
from .gitops import remote_branch_sha, cleanup_worktree, status_porcelain, current_branch
from .orchestrator import Orchestrator
from .integration import IntegrationManager
from . import detect

# Set by main(); module-level so every command sees the same home.
BASE_DIR = Path.cwd()
CONFIG_PATH = BASE_DIR / "config.local.json"
PROG = "abi-autopilot"


def _set_home(home: Path, prog: str):
    global BASE_DIR, CONFIG_PATH, PROG
    BASE_DIR = home.resolve()
    CONFIG_PATH = BASE_DIR / "config.local.json"
    PROG = prog
    # Child processes started by the parallel daemon must use the same home.
    os.environ["ABI_AUTOPILOT_HOME"] = str(BASE_DIR)
    os.environ["ABI_AUTOPILOT_PROG"] = PROG


def cmd_version(args):
    print(cli_notice())


def _print_notice(stream=None):
    print("\n" + cli_notice(), file=stream or sys.stdout)


def _ask(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"{question}{suffix}: ").strip()
        except EOFError:
            answer = ""
        if answer or default:
            return answer or default
        print("  A value is required.")


def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [Y/n]: ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("", "y", "yes", "s", "si", "sí")


def _print_detection(det) -> None:
    print("\nDetected in the target repository:")
    for line in det.summary or ["(nothing recognized)"]:
        print(f"  - {line}")
    for phase in detect.PHASES:
        checks = det.validation[phase]
        if checks:
            print(f"  {phase:<11} " + ", ".join(f"{c['name']} ({c['command']})" for c in checks))
    for note in det.notes:
        print(f"  note: {note}")
    if not det.found:
        print("  WARNING: no checks were detected. Add your test/lint/build commands to")
        print("  `validation` in config.local.json before running issues (README, Step 4).")


def cmd_init(args):
    if CONFIG_PATH.exists() and not args.force:
        raise SystemExit(f"{CONFIG_PATH} already exists. Use --force only if you want to replace it.")
    interactive = sys.stdin.isatty() and not args.non_interactive

    repo_arg = args.repo
    if not repo_arg:
        if not interactive:
            raise SystemExit("--repo is required when init is not interactive.")
        repo_arg = _ask("Path to your local clone of the target repository")
    repo = Path(repo_arg).expanduser().resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"{repo} is not a Git repository (no .git folder).")

    slug = args.repo_slug or detect.infer_repo_slug(repo)
    base = args.base_branch or detect.infer_base_branch(repo)
    deployment = args.deployment_branch
    if interactive:
        if not args.repo_slug:
            slug = _ask("GitHub repository (owner/name)", slug)
        if not args.base_branch:
            base = _ask("Integration branch (where approved changes are merged)", base)
        if not args.deployment_branch:
            deployment = _ask("Deployment branch (Enter = same as integration)", base)
    missing = [flag for flag, value in (("--repo-slug", slug), ("--base-branch", base)) if not value]
    if missing:
        raise SystemExit("Could not infer " + " and ".join(missing) + "; pass them explicitly.")
    if not detect.parse_github_slug(f"github.com/{slug}"):
        raise SystemExit(f"'{slug}' does not look like owner/repository.")

    detection = None
    if not args.no_detect:
        detection = detect.detect_project(repo)
        _print_detection(detection)
        if interactive and not _confirm("\nWrite config.local.json with these settings?"):
            raise SystemExit("Nothing written.")

    cfg = write_initial_config(CONFIG_PATH, str(repo), slug, base, deployment, detection=detection)
    (BASE_DIR / "runtime").mkdir(exist_ok=True)
    print(f"\nConfig created: {CONFIG_PATH}")
    print(f"Repository: {cfg['repo_slug']} · {cfg['repo_path']}")
    print(f"Integration: {cfg['base_branch']} · Deployment: {cfg['deployment_branch']}")
    if cfg["base_branch"] != cfg["deployment_branch"]:
        print("Issues will be closed only when the work reaches the deployment branch.")
    print(f"Worktrees: {cfg['worktree_root']}")
    print(f"Next: {PROG} doctor")
    _print_notice()


def cmd_upgrade_config(args):
    cfg = upgrade_config(CONFIG_PATH)
    print(f"Config upgraded to v{__version__}: {CONFIG_PATH}")
    print(f"Integration: {cfg['base_branch']} · Deployment: {cfg.get('deployment_branch')}")
    print(f"Previous config backup: {CONFIG_PATH}.pre-v{__version__}.bak")
    print(f"Workspace bootstrap: {cfg.get('workspace_bootstrap',[{}])[0].get('strategy','legacy')} · {cfg.get('workspace_bootstrap',[{}])[0].get('command','-')}")
    print(f"Dependency cache: {cfg.get('dependency_cache',{}).get('root','-')}")
    print(f"Manual integration: {PROG} integrate --issue <N>")
    print(f"Batch integration: {PROG} integrate-done --issues <N...> --execute")


def _first_line(text: str, default: str = "-") -> str:
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else default


def cmd_doctor(args):
    cfg = load_config(CONFIG_PATH)
    checks = []
    warnings = []
    repo = Path(cfg["repo_path"])
    checks.append(("version", True, f"ABI Autopilot v{__version__}"))
    checks.append(("repo", repo.exists() and (repo / ".git").exists(), str(repo)))
    for tool in ("git", "gh", cfg["codex"].get("command", "codex"), cfg["claude"].get("command", "claude")):
        checks.append((tool, which(tool) is not None, which(tool) or "not found"))
    specs = list(cfg.get("workspace_bootstrap", []))
    for phase_specs in cfg.get("validation", {}).values():
        if isinstance(phase_specs, list):
            specs.extend(x for x in phase_specs if isinstance(x, dict))
    uses_node = any(str(x.get("command", "")).split(" ", 1)[0] in ("npm", "npx", "node") for x in specs)
    if uses_node:
        checks.append(("node", which("node") is not None, which("node") or "not found"))
        checks.append(("npm", which("npm") is not None, which("npm") or "not found"))
    # Un cwd inexistente hace que el check se saltee en silencio: casi siempre
    # es la config de ejemplo sin adaptar al repositorio objetivo.
    missing_cwds = sorted({
        str(x.get("cwd")) for x in specs
        if x.get("cwd") and repo.exists() and not (repo / str(x.get("cwd"))).exists()
    })
    if not any(isinstance(v, list) and v for v in cfg.get("validation", {}).values()):
        warnings.append("No validation checks are configured: issues would pass with the AI review only. Add checks to `validation` (README, Step 4).")
    for cwd in missing_cwds:
        warnings.append(f"cwd '{cwd}' used by a check does not exist in the repo; that check will never run. Adapt the config.")
    if which("gh"):
        p = run_capture(["gh", "auth", "status"], check=False)
        checks.append(("gh auth", p.returncode == 0, _first_line((p.stdout or "") + (p.stderr or ""), f"rc={p.returncode}")))
    try:
        sha = remote_branch_sha(cfg["repo_path"], cfg["base_branch"])
        checks.append(("origin/base", True, sha))
    except Exception as e:
        checks.append(("origin/base", False, str(e)))

    # Runtime facts are informational; only clearly unsafe disk pressure fails.
    try:
        py = run_capture([sys.executable, "--version"], check=False)
        checks.append(("python", py.returncode == 0, _first_line((py.stdout or "") + (py.stderr or ""), platform.python_version())))
    except Exception as e:
        checks.append(("python", False, str(e)))
    node_version_text = None
    for cmd, name in (("node", "node version"), ("npm", "npm version")):
        if which(cmd):
            p = run_capture([cmd, "--version"], check=False)
            detail = _first_line((p.stdout or "") + (p.stderr or ""))
            checks.append((name, p.returncode == 0, detail))
            if cmd == "node" and p.returncode == 0:
                node_version_text = detail.lstrip("v")

    toolchain = cfg.get("toolchain", {})
    expected_py = str(toolchain.get("expected_python_major_minor", "")).strip()
    expected_node = toolchain.get("expected_node_major")
    strict_toolchain = bool(toolchain.get("strict", False))
    actual_py = f"{sys.version_info.major}.{sys.version_info.minor}"
    if expected_py and actual_py != expected_py:
        msg = f"Active Python is {actual_py}; the repo CI expects {expected_py}."
        if strict_toolchain:
            checks.append(("python compat", False, msg))
        else:
            warnings.append(msg)
    if expected_node and node_version_text:
        try:
            actual_node_major = int(node_version_text.split(".", 1)[0])
            if actual_node_major != int(expected_node):
                msg = f"Active Node is {actual_node_major}; the repo CI expects {expected_node}."
                if strict_toolchain:
                    checks.append(("node compat", False, msg))
                else:
                    warnings.append(msg)
        except ValueError:
            warnings.append(f"Could not parse the Node version: {node_version_text}")

    try:
        dep_root = Path(cfg.get("dependency_cache", {}).get("root", repo.parent))
        dep_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(dep_root).free / (1024 ** 3)
        minimum = float(cfg.get("dependency_cache", {}).get("min_free_gb", 8))
        checks.append(("disk free", free >= minimum, f"{free:.1f} GB free · cache minimum {minimum:.1f} GB"))
        npm_cache = dep_root / "npm"
        cache_entries = 0
        if npm_cache.exists():
            cache_entries = sum(1 for x in npm_cache.iterdir() if x.is_dir() and not x.name.startswith("."))
        checks.append(("dependency cache", True, f"{cache_entries} entr(y/ies) · {dep_root}"))
    except Exception as e:
        warnings.append(f"disk: {e}")

    process_env = cfg.get("process_env", {})
    if process_env.get("PLAYWRIGHT_HTML_OPEN") != "never":
        warnings.append("PLAYWRIGHT_HTML_OPEN is not 'never'; a failing E2E run may open/serve the HTML reporter.")

    try:
        branch = current_branch(repo)
        dirty = status_porcelain(repo).strip().splitlines()
        detail = f"branch={branch or '(detached)'} · dirty={len(dirty)}"
        checks.append(("local checkout", True, detail))
    except Exception as e:
        warnings.append(f"local checkout: {e}")

    print(f"{'CHECK':<18} {'STATUS':<6} DETAIL")
    failed = False
    for name, ok, detail in checks:
        print(f"{name:<18} {'PASS' if ok else 'FAIL':<6} {detail}")
        failed |= not ok
    for warning in warnings:
        print(f"WARN              {warning}")

    if args.probe_agents:
        print("\nProbing Codex (uses a minimal amount of quota)...")
        p = run_capture([cfg['codex'].get('command','codex'), "exec", "--ephemeral", "Reply exactly: OK"], cwd=cfg['repo_path'], check=False, timeout=300)
        print("Codex:", "PASS" if p.returncode == 0 and "OK" in p.stdout else "FAIL")
        failed |= p.returncode != 0
        print("Probing Claude (uses a minimal amount of quota)...")
        p = run_capture([cfg['claude'].get('command','claude'), "-p", "Reply exactly: OK", "--max-turns", "1"], cwd=cfg['repo_path'], check=False, timeout=300)
        print("Claude:", "PASS" if p.returncode == 0 and "OK" in p.stdout else "FAIL")
        failed |= p.returncode != 0
    _print_notice()
    if failed:
        raise SystemExit(2)


def get_orch():
    return Orchestrator(BASE_DIR, load_config(CONFIG_PATH))


def get_integration_manager():
    return IntegrationManager(BASE_DIR, load_config(CONFIG_PATH))


def cmd_bootstrap(args):
    cfg = load_config(CONFIG_PATH)
    ghx.ensure_labels(cfg["repo_slug"])
    print("Labels created/updated.")


def cmd_observe(args):
    get_orch().observe()


def cmd_mark_ready(args):
    cfg = load_config(CONFIG_PATH)
    issue = ghx.get_issue(cfg["repo_slug"], args.issue)
    ghx.set_risk(cfg["repo_slug"], issue, args.risk)
    issue = ghx.get_issue(cfg["repo_slug"], args.issue)
    ghx.set_state(cfg["repo_slug"], issue, "agent:ready", remove_special=True)
    print(f"#{args.issue} -> agent:ready, risk:{args.risk}")


def cmd_auto_ready(args):
    rows = get_orch().auto_ready_scan()
    if not rows:
        print("No untriaged issues (all of them already have an agent:* label).")
        return
    for row in rows:
        if row["action"] == "marked-ready":
            print(f"#{row['issue']} -> agent:ready, risk:{row['risk']}")
        else:
            print(f"#{row['issue']} -> skipped ({row['reason']})")


def cmd_run(args):
    result = get_orch().run_issue(args.issue, dry_run=args.dry_run, force=args.force, resume_existing=args.resume_existing)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "blocked":
        raise SystemExit(3)


def cmd_resume(args):
    cfg = load_config(CONFIG_PATH)
    issue = ghx.get_issue(cfg["repo_slug"], args.issue)
    ghx.set_state(cfg["repo_slug"], issue, "agent:ready", remove_special=True)
    result = get_orch().run_issue(args.issue, resume_existing=True, resume_stage=args.stage)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") == "blocked":
        raise SystemExit(3)


def cmd_daemon(args):
    print(cli_notice() + "\n", file=sys.stderr)
    get_orch().daemon(once=args.once)


def cmd_cleanup(args):
    cfg = load_config(CONFIG_PATH)
    ok, msg = cleanup_worktree(cfg["repo_path"], cfg["worktree_root"], args.issue)
    print(msg)
    if not ok:
        raise SystemExit(2)


def cmd_integrate(args):
    result = get_integration_manager().integrate_issue(
        args.issue,
        close=not args.no_close,
        continue_existing=args.continue_existing,
    )
    payload = as_json(result)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if result.status in ("blocked", "conflict"):
        raise SystemExit(4)


def as_json(result):
    if hasattr(result, "__dict__"):
        return result.__dict__
    return result


def cmd_integrate_done(args):
    manager = get_integration_manager()
    issue_numbers = args.issues or None
    result = manager.integrate_done(
        issue_numbers=issue_numbers,
        execute=args.execute,
        close=not args.no_close,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.execute and result and result[-1].get("status") not in ("integrated", "reconciled", "already-closed"):
        raise SystemExit(4)


def cmd_release_audit(args):
    """Check that closed issues are really in the deployment branch."""
    orch = get_orch()
    overview = orch.print_release_section()

    errors = orch.consistency_check()
    print()
    if errors:
        print(f"CONSISTENCY ERROR · {len(errors)} issue(s) CLOSED outside the deployment branch")
        for row in errors:
            print(
                f"  #{row['issue']} CLOSED but not present in deployment branch "
                f"{overview['deployment_branch']} ({len(row['pending_commits'])} unpromoted commit(s))"
            )
    else:
        print("CONSISTENCY OK · no closed issue is missing from the deployment branch.")

    if not overview["separate"]:
        print("No separate deployment branch: nothing can be pending promotion.")
        if errors:
            raise SystemExit(6)
        return

    if args.promote:
        cfg = load_config(CONFIG_PATH)
        drift_blocks = (
            cfg.get("release", {}).get("block_close_on_drift", False)
            and overview["drift_deployment_only"]
            and overview["drift_integration_only"]
        )
        if drift_blocks:
            print("\nPromotion blocked: the branches diverged and block_close_on_drift is enabled.")
            raise SystemExit(5)
        promoted = []
        for row in overview["rows"]:
            if row["released"] is not True:
                continue
            if str(row["state"]).upper() == "CLOSED":
                continue
            issue = ghx.get_issue(cfg["repo_slug"], row["issue"])
            if issue.is_epic:
                continue
            ghx.set_state(cfg["repo_slug"], issue, "agent:done")
            ghx.close_issue(
                cfg["repo_slug"],
                issue.number,
                f"Promoted to `{overview['deployment_branch']}`: the work is reachable "
                "from the deployment branch. Closed as delivered.",
            )
            promoted.append(issue.number)
        print()
        if promoted:
            print("PROMOTED: " + ", ".join(f"#{n}" for n in promoted))
        else:
            print("No integrated issues ready to promote.")

    if errors:
        raise SystemExit(6)


def cmd_release_status(args):
    """Delivery state of a single issue."""
    cfg = load_config(CONFIG_PATH)
    orch = get_orch()
    issue = ghx.get_issue(cfg["repo_slug"], args.issue)
    info = orch.issue_release_state(issue)
    state = next((x for x in issue.labels if x.startswith("agent:")), "-")
    if info["released"] is None:
        released = "UNKNOWN (issue branch not found on origin)"
    else:
        released = "YES" if info["released"] else "NO"
    print(f"#{issue.number} {issue.title}")
    print(f"  GitHub state       {issue.state}")
    print(f"  Agent state        {state}")
    print(f"  Integration branch {orch.base_branch}")
    print(f"  Deployment branch  {orch.deployment_branch}")
    print(f"  RELEASED           {released}")
    if info.get("evidence"):
        print(f"  Evidence           {info['evidence']}")
        if info["evidence"] == "message-match":
            print("                     (promoted with resolved conflicts: the patch is not")
            print("                      identical, review the diff by hand)")
    if info["pending_commits"]:
        print(f"  Unpromoted commits ({len(info['pending_commits'])}):")
        for sha in info["pending_commits"][:20]:
            print(f"    + {sha}")


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        description=f"ABI Autopilot v{__version__}",
        epilog=cli_notice(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("version"); s.set_defaults(func=cmd_version)
    s = sp.add_parser("init")
    s.add_argument("--repo", help="Path to the local clone Autopilot will work on (asked if omitted)")
    s.add_argument("--repo-slug", help="GitHub repository as owner/repository (default: from the origin remote)")
    s.add_argument("--base-branch", help="Remote branch that receives integrations (default: origin's default branch)")
    s.add_argument(
        "--deployment-branch",
        default=None,
        help="Branch that gets deployed. An issue is closed only when its work is "
             "reachable from it. Defaults to --base-branch.",
    )
    s.add_argument("--force", action="store_true", help="Replace an existing config.local.json")
    s.add_argument("--no-detect", action="store_true", help="Skip stack detection and write the example frontend/backend checks")
    s.add_argument("--non-interactive", action="store_true", help="Never prompt; fail if a value cannot be inferred")
    s.set_defaults(func=cmd_init)
    s = sp.add_parser("upgrade-config"); s.set_defaults(func=cmd_upgrade_config)
    s = sp.add_parser("doctor"); s.add_argument("--probe-agents", action="store_true"); s.set_defaults(func=cmd_doctor)
    s = sp.add_parser("bootstrap-labels"); s.set_defaults(func=cmd_bootstrap)
    s = sp.add_parser("observe"); s.set_defaults(func=cmd_observe)
    s = sp.add_parser("mark-ready"); s.add_argument("issue", type=int); s.add_argument("--risk", choices=["low","medium","high"], default="medium"); s.set_defaults(func=cmd_mark_ready)
    s = sp.add_parser("auto-ready", help="Label open issues without any agent:* label as agent:ready (epics, needs:human and open dependencies are left alone)"); s.set_defaults(func=cmd_auto_ready)
    s = sp.add_parser("run"); s.add_argument("--issue", type=int, required=True); s.add_argument("--dry-run", action="store_true"); s.add_argument("--force", action="store_true"); s.add_argument("--resume-existing", action="store_true"); s.set_defaults(func=cmd_run)
    s = sp.add_parser("resume"); s.add_argument("--issue", type=int, required=True); s.add_argument("--stage", choices=["targeted","fast","review","full"], default=None); s.set_defaults(func=cmd_resume)
    s = sp.add_parser("daemon"); s.add_argument("--once", action="store_true"); s.set_defaults(func=cmd_daemon)
    s = sp.add_parser("cleanup"); s.add_argument("--issue", type=int, required=True); s.set_defaults(func=cmd_cleanup)

    s = sp.add_parser("integrate", help="Validate an agent:implemented issue in a temporary worktree and integrate it")
    s.add_argument("--issue", type=int, required=True)
    s.add_argument("--continue", dest="continue_existing", action="store_true", help="Continue a conflicting cherry-pick you already resolved")
    s.add_argument("--no-close", action="store_true", help="Validate and integrate but leave the issue open")
    s.set_defaults(func=cmd_integrate)

    s = sp.add_parser("integrate-done", help="Plan or sequentially integrate agent:implemented issues")
    s.add_argument("--issues", type=int, nargs="*", help="Explicit order, e.g. --issues 27 28 29 96 92")
    s.add_argument("--execute", action="store_true", help="Without this flag only the plan is shown")
    s.add_argument("--no-close", action="store_true")
    s.set_defaults(func=cmd_integrate_done)

    s = sp.add_parser(
        "release-audit",
        help="Check that closed issues are really in the deployment branch "
             "and show integration/deployment drift",
    )
    s.add_argument(
        "--promote",
        action="store_true",
        help="Close agent:integrated issues whose work is already in the "
             "deployment branch as delivered",
    )
    s.set_defaults(func=cmd_release_audit)

    s = sp.add_parser("release-status", help="Delivery state of a single issue")
    s.add_argument("--issue", type=int, required=True)
    s.set_defaults(func=cmd_release_status)
    return p


def main(argv=None, home: Path | None = None, prog: str | None = None):
    # Subprocess output streamed to the console (npm/eslint/etc.) can contain
    # characters the default Windows console codepage (cp1252) can't encode.
    # Force UTF-8 with replacement instead of letting a stray glyph crash an
    # otherwise-passing run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if home is None:
        home = Path(os.environ.get("ABI_AUTOPILOT_HOME") or Path.cwd())
    _set_home(home, prog or os.environ.get("ABI_AUTOPILOT_PROG") or "abi-autopilot")
    try:
        args = build_parser().parse_args(argv)
        args.func(args)
    except (ConfigError, CommandError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if getattr(e, "output", ""):
            print(e.output[-8000:], file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\nInterrupted by user. Saved state is kept; nothing is aborted, reset or stashed automatically.")
        raise SystemExit(130)


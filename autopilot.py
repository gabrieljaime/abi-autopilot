from __future__ import annotations
import argparse
import json
import platform
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Subprocess output streamed to the console (npm/eslint/etc.) can contain
# characters the default Windows console codepage (cp1252) can't encode, e.g.
# the checkmarks next lint prints. Force UTF-8 with replacement instead of
# letting a stray glyph crash an otherwise-passing integration run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from abi_autopilot import __version__
from abi_autopilot.config import load_config, write_initial_config, upgrade_config, ConfigError
from abi_autopilot.shell import which, run_capture, CommandError
from abi_autopilot import github as ghx
from abi_autopilot.gitops import remote_branch_sha, cleanup_worktree, status_porcelain, current_branch
from abi_autopilot.orchestrator import Orchestrator
from abi_autopilot.integration import IntegrationManager

CONFIG_PATH = BASE_DIR / "config.local.json"


def cmd_version(args):
    print(f"ABI Autopilot v{__version__}")


def cmd_init(args):
    if CONFIG_PATH.exists() and not args.force:
        raise SystemExit(f"{CONFIG_PATH} ya existe. Usá --force sólo si querés reemplazarlo.")
    cfg = write_initial_config(CONFIG_PATH, args.repo, args.repo_slug, args.base_branch)
    (BASE_DIR / "runtime").mkdir(exist_ok=True)
    print(f"Config creada: {CONFIG_PATH}")
    print(f"Worktrees: {cfg['worktree_root']}")
    print("Siguiente: python autopilot.py doctor")


def cmd_upgrade_config(args):
    cfg = upgrade_config(CONFIG_PATH)
    print(f"Config actualizada a v{__version__}: {CONFIG_PATH}")
    print(f"Backup previo: {CONFIG_PATH}.pre-v{__version__}.bak")
    print(f"Bootstrap frontend: {cfg.get('workspace_bootstrap',[{}])[0].get('strategy','legacy')} · {cfg.get('workspace_bootstrap',[{}])[0].get('command','-')}")
    print(f"Dependency cache: {cfg.get('dependency_cache',{}).get('root','-')}")
    print("Integración manual: python autopilot.py integrate --issue <N>")
    print("Batch optimizado: python autopilot.py integrate-done --issues <N...> --execute")


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
    if (repo / "frontend").exists():
        checks.append(("node", which("node") is not None, which("node") or "not found"))
        checks.append(("npm", which("npm") is not None, which("npm") or "not found"))
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
        msg = f"Python activo {actual_py}; CI del repo espera {expected_py}."
        if strict_toolchain:
            checks.append(("python compat", False, msg))
        else:
            warnings.append(msg)
    if expected_node and node_version_text:
        try:
            actual_node_major = int(node_version_text.split(".", 1)[0])
            if actual_node_major != int(expected_node):
                msg = f"Node activo {actual_node_major}; CI del repo espera {expected_node}."
                if strict_toolchain:
                    checks.append(("node compat", False, msg))
                else:
                    warnings.append(msg)
        except ValueError:
            warnings.append(f"No pude interpretar versión Node: {node_version_text}")

    try:
        dep_root = Path(cfg.get("dependency_cache", {}).get("root", repo.parent))
        dep_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(dep_root).free / (1024 ** 3)
        minimum = float(cfg.get("dependency_cache", {}).get("min_free_gb", 8))
        checks.append(("disk free", free >= minimum, f"{free:.1f} GB libres · mínimo cache {minimum:.1f} GB"))
        npm_cache = dep_root / "npm"
        cache_entries = 0
        if npm_cache.exists():
            cache_entries = sum(1 for x in npm_cache.iterdir() if x.is_dir() and not x.name.startswith("."))
        checks.append(("dependency cache", True, f"{cache_entries} entrada(s) · {dep_root}"))
    except Exception as e:
        warnings.append(f"disk: {e}")

    process_env = cfg.get("process_env", {})
    if process_env.get("PLAYWRIGHT_HTML_OPEN") != "never":
        warnings.append("PLAYWRIGHT_HTML_OPEN no está en 'never'; una falla E2E puede abrir/servir el HTML reporter.")

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
        print("\nProbe Codex (consume una interacción mínima)...")
        p = run_capture([cfg['codex'].get('command','codex'), "exec", "--ephemeral", "Reply exactly: OK"], cwd=cfg['repo_path'], check=False, timeout=300)
        print("Codex:", "PASS" if p.returncode == 0 and "OK" in p.stdout else "FAIL")
        failed |= p.returncode != 0
        print("Probe Claude (consume una interacción mínima)...")
        p = run_capture([cfg['claude'].get('command','claude'), "-p", "Reply exactly: OK", "--max-turns", "1"], cwd=cfg['repo_path'], check=False, timeout=300)
        print("Claude:", "PASS" if p.returncode == 0 and "OK" in p.stdout else "FAIL")
        failed |= p.returncode != 0
    if failed:
        raise SystemExit(2)


def get_orch():
    return Orchestrator(BASE_DIR, load_config(CONFIG_PATH))


def get_integration_manager():
    return IntegrationManager(BASE_DIR, load_config(CONFIG_PATH))


def cmd_bootstrap(args):
    cfg = load_config(CONFIG_PATH)
    ghx.ensure_labels(cfg["repo_slug"])
    print("Labels creados/actualizados.")


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
        print("No hay issues sin triage (todas ya tienen label agent:*).")
        return
    for row in rows:
        if row["action"] == "marked-ready":
            print(f"#{row['issue']} -> agent:ready, risk:{row['risk']}")
        else:
            print(f"#{row['issue']} -> omitida ({row['reason']})")


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


def build_parser():
    p = argparse.ArgumentParser(description=f"ABI Autopilot v{__version__}")
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("version"); s.set_defaults(func=cmd_version)
    s = sp.add_parser("init")
    s.add_argument("--repo", required=True, help="Ruta al repositorio que Autopilot administrará")
    s.add_argument("--repo-slug", required=True, help="Repositorio GitHub en formato owner/repository")
    s.add_argument("--base-branch", required=True, help="Rama remota que recibirá las integraciones")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)
    s = sp.add_parser("upgrade-config"); s.set_defaults(func=cmd_upgrade_config)
    s = sp.add_parser("doctor"); s.add_argument("--probe-agents", action="store_true"); s.set_defaults(func=cmd_doctor)
    s = sp.add_parser("bootstrap-labels"); s.set_defaults(func=cmd_bootstrap)
    s = sp.add_parser("observe"); s.set_defaults(func=cmd_observe)
    s = sp.add_parser("mark-ready"); s.add_argument("issue", type=int); s.add_argument("--risk", choices=["low","medium","high"], default="medium"); s.set_defaults(func=cmd_mark_ready)
    s = sp.add_parser("auto-ready", help="Etiqueta agent:ready las issues abiertas sin label agent:* (epics/needs:human/deps abiertas quedan intactas)"); s.set_defaults(func=cmd_auto_ready)
    s = sp.add_parser("run"); s.add_argument("--issue", type=int, required=True); s.add_argument("--dry-run", action="store_true"); s.add_argument("--force", action="store_true"); s.add_argument("--resume-existing", action="store_true"); s.set_defaults(func=cmd_run)
    s = sp.add_parser("resume"); s.add_argument("--issue", type=int, required=True); s.add_argument("--stage", choices=["targeted","fast","review","full"], default=None); s.set_defaults(func=cmd_resume)
    s = sp.add_parser("daemon"); s.add_argument("--once", action="store_true"); s.set_defaults(func=cmd_daemon)
    s = sp.add_parser("cleanup"); s.add_argument("--issue", type=int, required=True); s.set_defaults(func=cmd_cleanup)

    s = sp.add_parser("integrate", help="Integra una issue agent:done en un worktree temporal y la valida")
    s.add_argument("--issue", type=int, required=True)
    s.add_argument("--continue", dest="continue_existing", action="store_true", help="Continúa un cherry-pick conflictivo ya resuelto")
    s.add_argument("--no-close", action="store_true", help="Integra y valida pero deja la issue abierta")
    s.set_defaults(func=cmd_integrate)

    s = sp.add_parser("integrate-done", help="Planifica o integra secuencialmente issues agent:done")
    s.add_argument("--issues", type=int, nargs="*", help="Orden explícito, ej. --issues 27 28 29 96 92")
    s.add_argument("--execute", action="store_true", help="Sin este flag sólo muestra el plan")
    s.add_argument("--no-close", action="store_true")
    s.set_defaults(func=cmd_integrate_done)
    return p


def main():
    try:
        args = build_parser().parse_args()
        args.func(args)
    except (ConfigError, CommandError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if getattr(e, "output", ""):
            print(e.output[-8000:], file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\nInterrumpido por usuario. El estado persistido se conserva; no se aborta/reset/stash automáticamente.")
        raise SystemExit(130)

if __name__ == "__main__":
    main()

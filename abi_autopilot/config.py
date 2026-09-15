from __future__ import annotations
import json
from pathlib import Path

class ConfigError(RuntimeError):
    pass


def default_config(repo_path: Path, repo_slug: str, base_branch: str) -> dict:
    worktree_root = repo_path.parent / f"{repo_path.name}-autopilot-worktrees"
    return {
        "repo_path": str(repo_path),
        "repo_slug": repo_slug,
        "base_branch": base_branch,
        "worktree_root": str(worktree_root),
        "runtime_dir": "runtime",
        "autopilot": {
            "poll_seconds": 120,
            "max_parallel": 1,
            "max_fix_loops": 3,
            "max_review_loops": 2,
            "skip_epics": True,
            # When true, the daemon auto-labels open issues with no agent:*
            # state (fresh issues/epics children created in GitHub) as
            # agent:ready on every poll, instead of requiring a manual
            # `mark-ready` per issue. Epics, needs:human/needs:product and
            # issues with open dependencies are always left untouched.
            "auto_ready": False,
            "auto_ready_default_risk": "medium",
            "implementer": "codex",
            "implementer_fallback": None,
        },
        "availability": {
            "enabled": True,
            "quota_fallback_wait_seconds": 900,
            "quota_reset_grace_seconds": 120,
            "max_wait_hours": 24,
            "transient_backoff_seconds": [60, 120, 300, 600, 900],
        },
        "dependency_cache": {
            "enabled": True,
            "root": str(repo_path.parent / f"{repo_path.name}-autopilot-deps"),
            "min_free_gb": 8,
            "lock_timeout_seconds": 1800,
            "stale_lock_seconds": 7200,
            "cleanup_done_artifacts": True,
            "done_artifacts": [
                "frontend/.next",
                "frontend/node_modules",
                "frontend/playwright-report",
                "frontend/test-results",
                ".coverage",
                "backend/.coverage",
                "coverage.xml",
                "backend/coverage.xml",
            ],
        },
        "process_env": {
            "PLAYWRIGHT_HTML_OPEN": "never",
        },
        "toolchain": {
            # Mirrors the repository CI workflow. Doctor warns on drift by
            # default rather than silently changing the operator's interpreters.
            "expected_python_major_minor": "3.11",
            "expected_node_major": 20,
            "strict": False,
        },
        "baseline": {
            "enabled": True,
            "required": False,
            "compare_phases": ["targeted", "fast"],
            "mode": "no_new_failures"
        },
        "codex": {
            "command": "codex",
            "sandbox": "workspace-write",
            "model": None,
            "fix_model": None,
            "extra_args": ["--ephemeral"],
        },
        "claude": {
            "command": "claude",
            "model": "sonnet",
            "implement_model": None,
            "implement_max_turns": 8,
            "implement_permission_mode": "acceptEdits",
            "max_turns": 8,
            "resume_turns": 4,
            "max_turn_resumes": 2,
            "max_diff_chars": 60000,
            "permission_mode": "plan",
        },
        "batch_validation": {
            # integrate-done stages the whole batch in one detached worktree,
            # runs targeted + cheap checks per issue, then one accumulated full gate.
            "enabled": True,
            "full_suite_once_at_end": True,
            "close_only_after_final_gate": True,
            "force_full_for_high_risk": True,
        },
        "validation_workers": {
            # Vitest is safe to fan out. Pytest stays serial until pytest-xdist is
            # explicitly installed in the ABI repo. Playwright stays serial because
            # the current E2E suite shares application/backend state.
            "vitest": 4,
            "pytest": 1,
            "playwright": 1,
        },
        "integration": {
            "auto_integrate": False,
            "auto_close": False,
            "allowed_risks": ["low"],
            "push_issue_branch": True,
            "comment_updates": True,
            # Explicit `integrate` is a human-triggered action. These settings do
            # not change daemon auto_integrate/auto_close defaults.
            "manual_close_after_success": True,
            "validate_targeted": True,
            "validate_fast": True,
            "full_for_risks": ["medium", "high"],
            "full_if_frontend_changed": True,
            "cleanup_generated": True,
            "generated_artifacts": [
                ".coverage",
                "backend/.coverage",
                "coverage.xml",
                "backend/coverage.xml",
                "frontend/.next",
                "frontend/playwright-report",
                "frontend/test-results",
            ],
            "sync_clean_local_base_after_push": True,
        },
        "protected_paths": [".env", "backend/data/"],
        "workspace_bootstrap": [
            {
                "name": "frontend-npm-ci",
                "cwd": "frontend",
                "strategy": "npm_shared_cache",
                "command": "npm ci --no-audit --no-fund",
                "manifest": "package.json",
                "lockfile": "package-lock.json",
                "key_files": ["package-lock.json", "package.json"],
                "cache_probe": "node_modules/jsdom/package.json",
                "link_path": "node_modules",
                "if_missing": "node_modules/jsdom/package.json",
                "rerun_if_changed": ["frontend/package.json", "frontend/package-lock.json"],
                "timeout": 2400,
            }
        ],
        "validation": {
            "non_llm_retries": 1,
            "targeted_backend_no_cov": True,
            "targeted": [],
            "fast": [
                {"name":"frontend-vitest","cwd":"frontend","command":"npm test","when":"frontend_changed","timeout":1800},
                {"name":"frontend-tsc","cwd":"frontend","command":"npm run typecheck -- --incremental false","when":"frontend_changed","timeout":1200},
                {"name":"frontend-lint","cwd":"frontend","command":"npm run lint","when":"frontend_changed","timeout":1200},
                {"name":"backend-pytest","cwd":"backend","command":"python -m pytest -q","when":"backend_changed","timeout":3600},
            ],
            "full": [
                {"name":"frontend-build","cwd":"frontend","command":"npm run build","when":"frontend_changed","timeout":1800},
                {"name":"frontend-e2e","cwd":"frontend","command":"npm run test:e2e","when":"frontend_changed","timeout":7200},
            ],
            # Batch mode intentionally separates cheap per-issue checks from the
            # expensive accumulated gate. Individual `integrate --issue` keeps the
            # v1.6 validation policy unchanged.
            "batch_fast": [
                {"name":"frontend-tsc","cwd":"frontend","command":"npm run typecheck -- --incremental false","when":"frontend_changed","timeout":1200},
                {"name":"frontend-lint","cwd":"frontend","command":"npm run lint","when":"frontend_changed","timeout":1200},
            ],
            "batch_full": [
                {"name":"backend-pytest","cwd":"backend","command":"python -m pytest -q","when":"backend_changed","timeout":3600},
                {"name":"frontend-vitest","cwd":"frontend","command":"npm test","when":"frontend_changed","timeout":1800},
                {"name":"frontend-build","cwd":"frontend","command":"npm run build","when":"frontend_changed","timeout":1800},
                {"name":"frontend-e2e","cwd":"frontend","command":"npm run test:e2e","when":"frontend_changed","timeout":7200},
            ],
        },
    }


def _deep_merge(defaults: dict, current: dict) -> dict:
    out = dict(defaults)
    for k, v in current.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"No existe {path}. Ejecutá: python autopilot.py init --repo ...")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ["repo_path", "repo_slug", "base_branch", "worktree_root"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ConfigError(f"Faltan claves en config: {', '.join(missing)}")
    data["repo_path"] = str(Path(data["repo_path"]).expanduser().resolve())
    data["worktree_root"] = str(Path(data["worktree_root"]).expanduser().resolve())
    dep_root = data.get("dependency_cache", {}).get("root")
    if dep_root:
        data["dependency_cache"]["root"] = str(Path(dep_root).expanduser().resolve())
    return data


def write_initial_config(dest: Path, repo: str, repo_slug: str, base_branch: str) -> dict:
    repo_path = Path(repo).expanduser().resolve()
    cfg = default_config(repo_path, repo_slug, base_branch)
    dest.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def upgrade_config(path: Path) -> dict:
    current = load_config(path)
    defaults = default_config(Path(current["repo_path"]), current["repo_slug"], current["base_branch"])
    merged = _deep_merge(defaults, current)

    # v1 used npx directly. Replace only the exact old defaults; preserve custom commands.
    replacements = {
        "npx vitest run": "npm test",
        "npx tsc --noEmit --incremental false": "npm run typecheck -- --incremental false",
        "npx playwright test": "npm run test:e2e",
    }
    for phase in ("fast", "full"):
        for spec in merged.get("validation", {}).get(phase, []):
            if spec.get("command") in replacements:
                spec["command"] = replacements[spec["command"]]

    # v1.3 defaulted to a 120k patch. Preserve custom values.
    if current.get("claude", {}).get("max_diff_chars") == 120000:
        merged.setdefault("claude", {})["max_diff_chars"] = 60000

    # v1.5: migrate the stock frontend npm bootstrap to the shared lock-keyed cache.
    # Custom bootstrap entries are left untouched.
    for spec in merged.get("workspace_bootstrap", []):
        if (
            spec.get("name") == "frontend-npm-ci"
            and spec.get("cwd") == "frontend"
            and str(spec.get("command", "")).strip().startswith("npm ci")
            and not spec.get("strategy")
        ):
            spec["strategy"] = "npm_shared_cache"
            spec.setdefault("manifest", "package.json")
            spec.setdefault("lockfile", "package-lock.json")
            spec.setdefault("key_files", ["package-lock.json", "package.json"])
            spec.setdefault("cache_probe", spec.get("if_missing", "node_modules/jsdom/package.json"))
            spec.setdefault("link_path", "node_modules")

    # v1.6: targeted backend subsets must not be failed by the repository-wide
    # coverage threshold. The broad backend suite still enforces coverage.
    merged.setdefault("validation", {}).setdefault("targeted_backend_no_cov", True)

    # Add newly known generated artifacts without replacing the operator's list.
    dep_artifacts = merged.setdefault("dependency_cache", {}).setdefault("done_artifacts", [])
    for rel in (".coverage", "backend/.coverage", "coverage.xml", "backend/coverage.xml"):
        if rel not in dep_artifacts:
            dep_artifacts.append(rel)

    integ = merged.setdefault("integration", {})
    integ_defaults = defaults["integration"]
    for key, value in integ_defaults.items():
        if key not in integ:
            integ[key] = value
    generated = integ.setdefault("generated_artifacts", [])
    for rel in integ_defaults["generated_artifacts"]:
        if rel not in generated:
            generated.append(rel)

    # v1.7: batch-aware integration and bounded worker settings are merged from
    # defaults. Existing operator values always win through _deep_merge.
    merged.setdefault("batch_validation", {})
    merged.setdefault("validation_workers", {})
    merged.setdefault("validation", {}).setdefault("batch_fast", defaults["validation"]["batch_fast"])
    merged.setdefault("validation", {}).setdefault("batch_full", defaults["validation"]["batch_full"])

    backup = path.with_suffix(path.suffix + ".pre-v1.7.0.bak")
    if not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged

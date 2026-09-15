from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re
from .shell import run_shell_stream

@dataclass
class CheckResult:
    name: str
    command: str
    cwd: str
    returncode: int
    log: str
    ignored: bool = False
    note: str = ""

    @property
    def passed(self):
        return self.returncode == 0 or self.ignored


def _condition_matches(condition: str, changed: list[str]) -> bool:
    if condition in (None, "", "always"):
        return True
    if condition == "frontend_changed":
        return any(x.startswith("frontend/") for x in changed)
    if condition == "backend_changed":
        return any(x.startswith("backend/") for x in changed)
    if condition == "docs_changed":
        return any(x.startswith("docs/") or x.lower().endswith(".md") for x in changed)
    return True


def _npm_append_arg(command: str, arg: str) -> str:
    """Append an argument to an npm script without breaking existing forwarded args."""
    if arg in command:
        return command
    if " -- " in command:
        return f"{command} {arg}"
    return f"{command} -- {arg}"


def _with_worker_limits(command: str, name: str, cfg: dict) -> str:
    """Apply bounded parallelism to known test runners.

    Pytest parallelism is opt-in because it requires pytest-xdist in the ABI
    backend environment. Keeping pytest=1 is always safe. Vitest can be bounded
    directly. Playwright remains configurable but defaults to 1 because the
    current ABI E2E suite shares backend/application state.
    """
    workers = cfg.get("validation_workers", {})
    low = name.lower()
    cmd = command
    if "vitest" in low:
        n = max(1, int(workers.get("vitest", 1) or 1))
        if n > 1 and "--maxWorkers" not in cmd and "--max-workers" not in cmd:
            cmd = _npm_append_arg(cmd, f"--maxWorkers={n}")
    elif "playwright" in low or low.endswith("e2e") or "-e2e" in low:
        n = max(1, int(workers.get("playwright", 1) or 1))
        if "--workers" not in cmd:
            cmd = _npm_append_arg(cmd, f"--workers={n}")
    elif "pytest" in low:
        n = max(1, int(workers.get("pytest", 1) or 1))
        if n > 1 and not re.search(r"(?:^|\s)-n(?:\s|=)", cmd):
            cmd = f"{cmd} -n {n}"
    return cmd


def _run_specs(specs: list[dict], worktree: Path, changed: list[str], phase: str, run_dir: Path, cfg: dict | None = None) -> list[CheckResult]:
    cfg = cfg or {}
    results: list[CheckResult] = []
    for spec in specs:
        if not _condition_matches(spec.get("when", "always"), changed):
            continue
        cwd = worktree / spec.get("cwd", "")
        name = spec["name"]
        cmd = _with_worker_limits(spec["command"], name, cfg)
        timeout = int(spec.get("timeout", 1800))
        log_path = run_dir / f"{phase}-{name}.log"
        print(f"\n=== {phase.upper()} · {name} ===")
        rc = run_shell_stream(cmd, cwd, log_path, timeout)
        results.append(CheckResult(name, cmd, str(cwd), rc, str(log_path)))
        if rc != 0:
            break
    return results


def rerun_failed_checks(results: list[CheckResult], phase: str, run_dir: Path, attempt: int) -> list[CheckResult]:
    """Re-run only the failed command(s), without calling an LLM.

    This absorbs one-off/flaky harness failures before spending a fix loop.
    """
    out: list[CheckResult] = []
    for r in results:
        if r.passed:
            out.append(r)
            continue
        log_path = run_dir / f"{phase}-{r.name}-retry{attempt}.log"
        print(f"\n=== {phase.upper()} RETRY {attempt} · {r.name} ===")
        rc = run_shell_stream(r.command, Path(r.cwd), log_path, 7200 if phase in ("full", "batch_full") else 1800)
        out.append(CheckResult(r.name, r.command, r.cwd, rc, str(log_path)))
    return out


def _quote_arg(value: str) -> str:
    # Paths discovered by Autopilot are repository-relative. Double quotes are
    # enough for PowerShell/cmd/bash invocations generated here.
    return '"' + value.replace('"', '\\"') + '"'


def discover_targeted_specs(worktree: Path, changed: list[str], cfg: dict | None = None) -> list[dict]:
    """Discover cheap tests adjacent to the changed code.

    v1.6 adds backend discovery and deliberately disables the *global* coverage
    gate for targeted pytest subsets. A three-file subset cannot reasonably meet
    the repository-wide ``--cov-fail-under`` threshold; coverage remains enforced
    by the broad backend suite (fast for single-issue integration, batch_full for batch mode).
    """
    cfg = cfg or {}
    specs: list[dict] = []

    backend = worktree / "backend"
    pytest_files: set[str] = set()
    if backend.exists():
        tests_root = backend / "tests"
        for f in changed:
            if not f.startswith("backend/") or not f.lower().endswith(".py"):
                continue
            rel = f[len("backend/"):]
            rel_path = Path(rel)
            low = rel.replace("\\", "/").lower()
            if low.startswith("tests/") and rel_path.name.startswith("test_"):
                if (backend / rel_path).exists():
                    pytest_files.add(rel_path.as_posix())
                continue

            # Map app/services/foo.py -> tests/**/test_foo.py and test_foo_*.py.
            if low.startswith("app/") and tests_root.exists():
                stem = rel_path.stem
                for cand in tests_root.rglob(f"test_{stem}.py"):
                    pytest_files.add(cand.relative_to(backend).as_posix())
                for cand in tests_root.rglob(f"test_{stem}_*.py"):
                    pytest_files.add(cand.relative_to(backend).as_posix())

        if pytest_files:
            args = " ".join(_quote_arg(x) for x in sorted(pytest_files))
            no_cov = cfg.get("validation", {}).get("targeted_backend_no_cov", True)
            cov_arg = " --no-cov" if no_cov else ""
            specs.append({
                "name": "auto-pytest",
                "cwd": "backend",
                "command": f"python -m pytest -q{cov_arg} {args}",
                "timeout": 1800,
            })

    frontend = worktree / "frontend"
    if frontend.exists():
        vitest: set[str] = set()
        playwright: set[str] = set()

        for f in changed:
            if not f.startswith("frontend/"):
                continue
            rel = f[len("frontend/"):]
            low = rel.lower()
            if low.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx")):
                vitest.add(rel)
            elif (low.startswith("e2e/") or low.startswith("tests/e2e/")) and low.endswith((".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")):
                playwright.add(rel)
            elif low.startswith("src/") and low.endswith((".ts", ".tsx", ".js", ".jsx")):
                src = frontend / rel
                suffix = src.suffix
                base = src.name[:-len(suffix)] if suffix else src.name
                for cand in (
                    src.with_name(base + ".test.ts"),
                    src.with_name(base + ".test.tsx"),
                    src.with_name(base + ".test.js"),
                    src.with_name(base + ".test.jsx"),
                    src.with_name(base + ".spec.ts"),
                    src.with_name(base + ".spec.tsx"),
                ):
                    if cand.exists():
                        vitest.add(cand.relative_to(frontend).as_posix())

        if vitest:
            args = " ".join(_quote_arg(x) for x in sorted(vitest))
            specs.append({"name": "auto-vitest", "cwd": "frontend", "command": f"npm test -- {args}", "timeout": 1800})
        if playwright:
            args = " ".join(_quote_arg(x) for x in sorted(playwright))
            specs.append({"name": "auto-playwright", "cwd": "frontend", "command": f"npm run test:e2e -- {args}", "timeout": 3600})

    return specs

def run_checks(cfg: dict, worktree: Path, changed: list[str], phase: str, run_dir: Path) -> list[CheckResult]:
    if phase == "targeted":
        explicit = cfg.get("validation", {}).get("targeted", [])
        specs = list(explicit) + discover_targeted_specs(worktree, changed, cfg)
    else:
        specs = cfg.get("validation", {}).get(phase, [])
    return _run_specs(specs, worktree, changed, phase, run_dir, cfg)


def summarize(results: list[CheckResult]) -> str:
    if not results:
        return "No checks aplicaron a los archivos modificados."
    lines = []
    for r in results:
        if r.ignored:
            status = "BASELINE-KNOWN"
        else:
            status = "PASS" if r.returncode == 0 else "FAIL"
        extra = f" · {r.note}" if r.note else ""
        lines.append(f"- {r.name}: {status} (rc={r.returncode}) log={r.log}{extra}")
    return "\n".join(lines)


def failure_feedback(results: list[CheckResult], max_chars: int = 50000) -> str:
    failed = [r for r in results if not r.passed]
    if not failed:
        return ""
    chunks = []
    for r in failed:
        p = Path(r.log)
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        chunks.append(f"CHECK {r.name} FAILED\nCOMMAND: {r.command}\n\n{text[-max_chars:]}")
    return "\n\n".join(chunks)


def _read_log(result: CheckResult) -> str:
    p = Path(result.log)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def failure_signatures(result: CheckResult) -> set[str]:
    """Extract stable test identities from common runners.

    We intentionally avoid comparing raw logs because timings and stack traces vary.
    """
    if result.returncode == 0:
        return set()
    text = _read_log(result)
    sigs: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        # Vitest: FAIL  src/x.test.tsx > suite > test
        if line.startswith("FAIL "):
            body = line[5:].strip()
            if body:
                sigs.add("vitest:" + re.sub(r"\s+", " ", body))
        # pytest: FAILED tests/test_x.py::test_y - ...
        elif line.startswith("FAILED "):
            body = line[7:].split(" - ", 1)[0].strip()
            if body:
                sigs.add("pytest:" + body)
        # Playwright commonly prints: 1) [chromium] › file.spec.ts:12:3 › title
        else:
            m = re.match(r"\d+\)\s+\[[^\]]+\]\s+›\s+(.+)", line)
            if m:
                sigs.add("playwright:" + re.sub(r"\s+", " ", m.group(1).strip()))
    return sigs


def mark_inherited_failures(candidate: list[CheckResult], baseline: list[CheckResult]) -> tuple[list[CheckResult], list[str]]:
    """Ignore only failures that are demonstrably identical to the base SHA.

    If the baseline passed, or we cannot extract stable identities, the candidate
    remains failed. This keeps the default conservative.
    """
    base_by_name = {r.name: r for r in baseline}
    inherited_notes: list[str] = []
    for cand in candidate:
        if cand.returncode == 0:
            continue
        base = base_by_name.get(cand.name)
        if not base or base.returncode == 0:
            continue
        c_sig = failure_signatures(cand)
        b_sig = failure_signatures(base)
        if c_sig and b_sig and c_sig.issubset(b_sig):
            cand.ignored = True
            cand.note = f"mismo fallo que base ({len(c_sig)} signature(s))"
            inherited_notes.append(f"{cand.name}: {', '.join(sorted(c_sig))}")
    return candidate, inherited_notes


def looks_like_workspace_dependency_failure(results: list[CheckResult]) -> bool:
    """Detect missing local JS deps so we don't waste LLM fix loops.

    A git worktree does not contain node_modules. If npx falls back to its cache,
    errors like vitest/config or jsdom missing are environment failures, not code.
    """
    needles = (
        "cannot find module 'vitest/config'",
        'cannot find module "vitest/config"',
        "cannot find package 'jsdom'",
        'cannot find package "jsdom"',
        "could not resolve '@vitejs/plugin-react'",
        'could not resolve "@vitejs/plugin-react"',
        "could not resolve 'vitest/config'",
        "err_module_not_found",
        "npm-cache\\_npx",
        "npm-cache/_npx",
    )
    for r in results:
        if r.passed:
            continue
        text = _read_log(r).lower()
        if any(n in text for n in needles):
            return True
    return False

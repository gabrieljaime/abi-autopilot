# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import github as ghx
from .locks import file_lock
from .agents import run_codex, run_claude_review, run_claude_implement
from .availability import classify_provider_failure
from .bootstrap import prepare_workspace, cleanup_done_artifacts
from .gitops import (
    create_or_reuse_worktree, changed_files, ensure_no_protected, status_porcelain,
    commit_all, push_issue_branch, fast_forward_remote_base, ensure_baseline_worktree,
    delete_issue_branch_if_merged,
    branch_divergence, branch_release_status, commit_is_released,
    find_issue_commits_on_branch, fetch as git_fetch, ref_exists, slugify,
)
from .shell import CommandError
from .state import RunStateStore
from .validator import (
    run_checks, summarize, failure_feedback, looks_like_workspace_dependency_failure,
    rerun_failed_checks, mark_inherited_failures,
)


def finalize_lifecycle(*, integrated: bool, gate_active: bool, released: bool) -> tuple[str, bool]:
    """Etiqueta y cierre al terminar `finalize`: ``(label, cerrar)``.

    Sólo lo entregado en la rama de despliegue cierra. Mergear en la rama de
    integración (el fast-forward de `auto_integrate`) no alcanza cuando la
    rama de despliegue es otra: así quedaron #71, #95 y #97 cerradas con su
    código fuera de `main`.
    """
    if not integrated:
        return ghx.IMPLEMENTED_LABEL, False
    if gate_active and not released:
        return ghx.INTEGRATED_LABEL, False
    return ghx.DONE_LABEL, True


class Orchestrator:
    def __init__(self, base_dir: Path, cfg: dict):
        self.base_dir = base_dir
        self.cfg = cfg
        self.repo_slug = cfg["repo_slug"]
        self.repo_path = cfg["repo_path"]
        self.base_branch = cfg["base_branch"]
        self.deployment_branch = cfg.get("deployment_branch") or cfg["base_branch"]
        runtime = Path(cfg.get("runtime_dir", "runtime"))
        if not runtime.is_absolute():
            runtime = base_dir / runtime
        self.runtime = runtime
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.states = RunStateStore(self.runtime)

        # Non-interactive subprocess defaults belong to the runner, not to the
        # operator's PowerShell session. This prevents Playwright HTML reporter
        # from starting a blocking local server after failures.
        for key, value in self.cfg.get("process_env", {}).items():
            if value is None:
                continue
            os.environ[str(key)] = str(value)

    def _lock(self, name: str):
        """Cross-process lock shared by every run against this repository.

        Needed when `autopilot.max_parallel > 1`; uncontended otherwise.
        """
        return file_lock(Path(self.cfg["worktree_root"]) / ".locks", name)

    def _run_dir(self, issue_number: int) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        p = self.runtime / "runs" / f"issue-{issue_number}" / ts
        p.mkdir(parents=True, exist_ok=True)
        return p

    def eligible(self, issue) -> tuple[bool, str]:
        if issue.state.upper() != "OPEN":
            return False, "issue closed"
        if self.cfg.get("autopilot", {}).get("skip_epics", True) and issue.is_epic:
            return False, "epic skipped by policy"
        if "needs:human" in issue.labels or "needs:product" in issue.labels:
            return False, "needs a human/product decision"
        try:
            ok, deps = ghx.deps_status(self.repo_slug, issue, self.cfg.get("dependency_ref_prefixes", []))
        except CommandError as e:
            return False, str(e)
        if not ok:
            return False, "open dependencies: " + ", ".join(f"{ref}:{state}" for ref, state in deps if state.upper() != "CLOSED")
        return True, "ok"

    def observe(self):
        if self.cfg.get("autopilot", {}).get("auto_ready", False):
            triaged = [r for r in self.auto_ready_scan() if r["action"] == "marked-ready"]
            if triaged:
                print("AUTO-READY (just triaged):")
                for row in triaged:
                    print(f"  #{row['issue']} -> agent:ready, risk:{row['risk']}")
                print()

        issues = ghx.list_actionable(self.repo_slug)
        rows = []
        if not issues:
            print("No agent:ready or agent:waiting-quota issues.")
        else:
            for i in sorted(issues, key=lambda x: x.number):
                ok, reason = self.eligible(i)
                deps = ghx.dependency_refs_from_body(i.body, self.cfg.get("dependency_ref_prefixes", []))
                state = next((x for x in i.labels if x.startswith("agent:")), "-")
                rows.append((i.number, i.risk, state, "YES" if ok else "NO", str(deps) if deps else "-", reason, i.title))
            print(f"{'#':>5}  {'RISK':<6} {'STATE':<20} {'READY':<5} {'DEPS':<22} {'REASON':<32} TITLE")
            for row in rows:
                print(f"{row[0]:>5}  {row[1]:<6} {row[2]:<20} {row[3]:<5} {row[4][:22]:<22} {row[5][:32]:<32} {row[6]}")

        done = [x for x in ghx.list_integration_candidates(self.repo_slug) if not x.is_epic]
        print()
        if not done:
            print(f"No {ghx.IMPLEMENTED_LABEL} issues waiting for integration.")
        else:
            print(f"{ghx.IMPLEMENTED_LABEL} waiting for integration ({len(done)}) — run 'integrate-done' to see the plan:")
            print(f"{'#':>5}  {'RISK':<6} TITLE")
            for i in sorted(done, key=lambda x: x.number):
                print(f"{i.number:>5}  {i.risk:<6} {i.title}")

        self.print_release_section()
        return rows

    # ── Release / promoción ─────────────────────────────────────────────────

    def _deployment_separate(self) -> bool:
        return self.deployment_branch != self.base_branch

    def _release_gate_active(self) -> bool:
        """Mismo criterio que `IntegrationManager._deployment_is_integration`."""
        if not self.cfg.get("release", {}).get("require_deployment_branch", True):
            return False
        return self._deployment_separate()

    def issue_release_state(self, issue) -> dict:
        """Estado de entrega de una issue, con la evidencia que lo respalda.

        Comparar SHAs no alcanza. Se combinan tres señales, de más fuerte a más
        débil:

        ``ancestor``          el commit es alcanzable desde la rama de despliegue.
        ``patch-equivalent``  el parche ya existe upstream aunque el SHA difiera
                              (cherry-pick o rebase limpios), según `git cherry`.
        ``message-match``     la rama de despliegue tiene commits con el prefijo
                              de Autopilot de esa issue, pero el parche no es
                              idéntico. Es lo que pasa cuando la promoción
                              resolvió conflictos: el trabajo está, el contenido
                              no es byte a byte el mismo.

        Se distingue `message-match` en lugar de mezclarlo con los otros dos
        porque es la única de las tres que conviene revisar a ojo.
        """
        deployment_ref = f"origin/{self.deployment_branch}"
        branch = f"origin/agent/issue-{issue.number}-{slugify(issue.title)}"
        released = None
        pending: list[str] = []
        evidence = None

        if ref_exists(self.repo_path, f"refs/remotes/{branch}"):
            status = branch_release_status(self.repo_path, branch, deployment_ref)
            released = status["released"]
            pending = status["pending"]
            if released:
                evidence = "patch-equivalent"
        else:
            # La rama de la issue puede haberse borrado tras integrar. El
            # trabajo sigue siendo identificable por sus commits en la rama de
            # integración: así se detectó el caso de #71.
            commits = find_issue_commits_on_branch(
                self.repo_path, f"origin/{self.base_branch}", issue.number
            )
            if commits:
                pending = [c for c in commits if not commit_is_released(self.repo_path, c, deployment_ref)]
                released = not pending
                if released:
                    evidence = "patch-equivalent"

        # Última señal: la promoción puede haber resuelto conflictos, y entonces
        # el patch-id ya no coincide aunque el trabajo sí esté entregado.
        if released is False or released is None:
            promoted = find_issue_commits_on_branch(
                self.repo_path, deployment_ref, issue.number
            )
            if promoted:
                released = True
                evidence = "message-match"
                pending = []

        return {
            "issue": issue.number,
            "title": issue.title,
            "labels": issue.labels,
            "state": issue.state,
            "branch_known": released is not None,
            "released": released,
            "evidence": evidence,
            "pending_commits": pending,
        }

    def release_overview(self, limit: int = 100) -> dict:
        """Conteos por etapa + divergencia entre integración y despliegue."""
        git_fetch(self.repo_path)
        integrated = [x for x in ghx._list_with_label(self.repo_slug, ghx.INTEGRATED_LABEL, limit) if not x.is_epic]
        implemented = [x for x in ghx.list_integration_candidates(self.repo_slug, limit) if not x.is_epic]
        # Sólo lo integrado puede estar pendiente de promoción: lo implementado
        # todavía no pasó por ninguna rama compartida.
        rows = [self.issue_release_state(i) for i in sorted(integrated, key=lambda x: x.number)]
        drift = (0, 0)
        if self._deployment_separate():
            drift = branch_divergence(
                self.repo_path,
                f"origin/{self.deployment_branch}",
                f"origin/{self.base_branch}",
            )
        return {
            "integration_branch": self.base_branch,
            "deployment_branch": self.deployment_branch,
            "separate": self._deployment_separate(),
            "drift_deployment_only": drift[0],
            "drift_integration_only": drift[1],
            "implemented": len(implemented),
            "released_closed": ghx.count_closed_with_label(self.repo_slug, ghx.DONE_LABEL),
            "rows": rows,
        }

    def print_release_section(self) -> dict:
        overview = self.release_overview()
        print()
        if not overview["separate"]:
            print(
                f"RELEASE · integration and deployment are the same branch ({self.base_branch}); "
                "integrating means delivering."
            )
            print(f"  IMPLEMENTED        {overview['implemented']}  (open, not integrated)")
            print(f"  RELEASED           {overview['released_closed']}  (closed {ghx.DONE_LABEL})")
            return overview

        rows = overview["rows"]
        released = [r for r in rows if r["released"] is True]
        pending = [r for r in rows if r["released"] is False]
        unknown = [r for r in rows if r["released"] is None]
        print(
            f"RELEASE · integration `{overview['integration_branch']}` "
            f"→ deployment `{overview['deployment_branch']}`"
        )
        print(f"  IMPLEMENTED        {overview['implemented']}  (open, not integrated)")
        print(f"  INTEGRATED         {len(rows)}  (open {ghx.INTEGRATED_LABEL})")
        print(f"  RELEASED           {overview['released_closed']}  (closed {ghx.DONE_LABEL})")
        print(f"  PENDING PROMOTION  {len(pending)}")
        if released:
            print(f"  READY TO CLOSE     {len(released)}  (integrated and already deployed; `release-audit --promote`)")
        if unknown:
            print(f"  UNKNOWN BRANCH     {len(unknown)}")
        if pending:
            print()
            print(f"  {'#':>5}  {'STATE':<20} {'RELEASED':<9} TITLE")
            for r in pending:
                state = next((x for x in r["labels"] if x.startswith("agent:")), "-")
                print(f"  {r['issue']:>5}  {state:<20} {'NO':<9} {r['title'][:60]}")

        left = overview["drift_deployment_only"]
        right = overview["drift_integration_only"]
        threshold = int(self.cfg.get("release", {}).get("drift_warn_commits", 1))
        if left and right and (left + right) >= threshold:
            print()
            print("RELEASE DRIFT")
            print(f"  {overview['deployment_branch']} unique commits: {left}")
            print(f"  {overview['integration_branch']} unique commits: {right}")
            print()
            print("  WARNING: the integration and deployment branches have diverged.")
            print("  Do not close new issues as released until they are reconciled.")
        return overview

    def consistency_check(self, limit: int | None = None) -> list[dict]:
        """Issues CLOSED cuyo trabajo no llegó a la rama de despliegue.

        Es el chequeo que faltaba: una issue cerrada con su código sólo en la
        rama de integración es una mentira de estado, y así #71, #95 y #97
        figuraron terminadas durante semanas sin estar en `main`.
        """
        # Corre también con una sola rama: una issue cerrada a mano, o cuyo merge
        # se revirtió, tampoco está entregada aunque no haya promoción.
        if limit is None:
            limit = int(self.cfg.get("release", {}).get("audit_limit", 1000))
        git_fetch(self.repo_path)
        p = ghx._gh(
            self.repo_slug,
            [
                "issue", "list", "--state", "closed", "--limit", str(limit),
                "--json", "number,title,body,state,url,labels",
            ],
        )
        arr = json.loads(p.stdout or "[]")
        errors = []
        for x in arr:
            issue = ghx.Issue(
                x["number"], x["title"], x.get("body") or "", x["state"], x["url"],
                [l["name"] for l in x.get("labels", [])],
            )
            if issue.is_epic:
                continue
            info = self.issue_release_state(issue)
            if info["branch_known"] and info["released"] is False:
                errors.append(info)
        return errors

    def auto_ready_scan(self) -> list[dict]:
        """Auto-triage open issues that have no agent:* label yet.

        This lets the daemon pick work straight from GitHub epics/issues
        instead of requiring a manual `mark-ready` per issue. Epics and
        issues that need a human/product call are left untouched; issues
        with open dependencies are skipped until those close.
        """
        default_risk = self.cfg.get("autopilot", {}).get("auto_ready_default_risk", "medium")
        rows = []
        for issue in ghx.list_untriaged(self.repo_slug):
            if self.cfg.get("autopilot", {}).get("skip_epics", True) and issue.is_epic:
                rows.append({"issue": issue.number, "action": "skipped", "reason": "epic"})
                continue
            if "needs:human" in issue.labels or "needs:product" in issue.labels:
                rows.append({"issue": issue.number, "action": "skipped", "reason": "needs:human/needs:product"})
                continue
            try:
                ok, deps = ghx.deps_status(self.repo_slug, issue, self.cfg.get("dependency_ref_prefixes", []))
            except CommandError as e:
                rows.append({"issue": issue.number, "action": "skipped", "reason": str(e)})
                continue
            if not ok:
                pending = ", ".join(f"{ref}:{state}" for ref, state in deps if state.upper() != "CLOSED")
                rows.append({"issue": issue.number, "action": "skipped", "reason": f"open dependencies: {pending}"})
                continue
            risk = issue.risk if any(l.startswith("risk:") for l in issue.labels) else default_risk
            ghx.set_risk(self.repo_slug, issue, risk)
            issue = ghx.get_issue(self.repo_slug, issue.number)
            ghx.set_state(self.repo_slug, issue, "agent:ready", remove_special=True)
            rows.append({"issue": issue.number, "action": "marked-ready", "risk": risk})
        return rows

    def eligible_queue(self) -> list:
        candidates = []
        for i in ghx.list_actionable(self.repo_slug):
            ok, _ = self.eligible(i)
            if ok:
                candidates.append(i)
        risk_order = {"low": 0, "medium": 1, "high": 2}
        # Resume quota waits first, then normal ready work.
        return sorted(candidates, key=lambda x: (0 if "agent:waiting-quota" in x.labels else 1, risk_order.get(x.risk, 1), x.number))

    def next_issue(self):
        queue = self.eligible_queue()
        return queue[0] if queue else None

    def _save_stage(self, issue_number: int, **updates):
        self.states.save(issue_number, **updates)

    def _bootstrap_or_block(self, issue, worktree: Path, changed: list[str], run_dir: Path, force: bool=False):
        results = prepare_workspace(self.cfg, worktree, changed, run_dir, force=force)
        failed = [r for r in results if not r.passed]
        if failed:
            lines = []
            for r in failed:
                lines.append(f"- {r.describe()}")
                if r.reason:
                    lines.append(f"  {r.reason}")
            reason = "Workspace bootstrap failed:\n" + "\n".join(lines)
            return self._block(issue, run_dir, reason)
        return None

    def _wait_seconds(self, signal, transient_attempt: int) -> int:
        avail = self.cfg.get("availability", {})
        if signal.kind == "quota":
            base = signal.retry_after_seconds or int(avail.get("quota_fallback_wait_seconds", 900))
            return max(30, base + int(avail.get("quota_reset_grace_seconds", 120)))
        backoff = list(avail.get("transient_backoff_seconds", [60, 120, 300, 600, 900]))
        if not backoff:
            return 120
        return int(backoff[min(transient_attempt, len(backoff)-1)])

    def _wait_for_provider(self, issue, provider: str, stage: str, signal, run_dir: Path, wait_started: float, transient_attempt: int, state_payload: dict) -> bool:
        avail = self.cfg.get("availability", {})
        if not avail.get("enabled", True):
            return False
        max_wait = int(float(avail.get("max_wait_hours", 24)) * 3600)
        if time.time() - wait_started > max_wait:
            return False
        delay = self._wait_seconds(signal, transient_attempt)
        if time.time() - wait_started + delay > max_wait:
            return False

        if signal.kind == "quota":
            live = ghx.get_issue(self.repo_slug, issue.number)
            ghx.set_state(self.repo_slug, live, "agent:waiting-quota")
            self._save_stage(issue.number, stage=stage, provider=provider, waiting_kind="quota", **state_payload)
            print(f"\nWAITING QUOTA · {provider}: retrying in {delay // 60} min {delay % 60} s. Does not count as a fix loop.")
        else:
            self._save_stage(issue.number, stage=stage, provider=provider, waiting_kind="transient", **state_payload)
            print(f"\nTRANSIENT · {provider}: retrying in {delay}s. Does not count as a fix loop.")
        time.sleep(delay)
        return True

    def _call_codex_with_retry(self, issue, worktree: Path, base_sha: str, run_dir: Path, mode: str, feedback: str | None, stage_state: dict):
        wait_started = time.time()
        transient_attempt = 0
        fallback = self.cfg.get("autopilot", {}).get("implementer_fallback")
        while True:
            rc, stdout, stderr = run_codex(self.base_dir, self.cfg, worktree, issue, base_sha, run_dir, mode, feedback)
            if rc == 0:
                return True, "", "codex"
            raw = (stdout or "") + "\n" + (stderr or "")
            signal = classify_provider_failure(raw)
            if signal.kind == "fatal":
                return False, f"Codex exited rc={rc}\n{raw[-8000:]}", "codex"
            # Out of Codex credits/quota and a fallback implementer is configured:
            # switch immediately instead of waiting hours for quota to reset.
            if signal.kind == "quota" and fallback and fallback != "codex":
                print(f"\nCODEX QUOTA exhausted — automatic fallback to {fallback} as implementer for this issue.")
                ok, err = self._call_provider_implement_with_retry(fallback, issue, worktree, base_sha, run_dir, mode, feedback, stage_state)
                return ok, err, fallback
            if not self._wait_for_provider(issue, "codex", "codex", signal, run_dir, wait_started, transient_attempt, stage_state):
                return False, f"Codex did not recover within the configured window ({signal.kind}).\n{raw[-8000:]}", "codex"
            if signal.kind == "transient":
                transient_attempt += 1
            # Restore execution label before retry.
            live = ghx.get_issue(self.repo_slug, issue.number)
            ghx.set_state(self.repo_slug, live, "agent:fix" if feedback else "agent:running")
            issue = ghx.get_issue(self.repo_slug, issue.number)

    def _call_claude_implement_with_retry(self, issue, worktree: Path, base_sha: str, run_dir: Path, mode: str, feedback: str | None, stage_state: dict):
        wait_started = time.time()
        transient_attempt = 0
        while True:
            rc, stdout, stderr = run_claude_implement(self.base_dir, self.cfg, worktree, issue, base_sha, run_dir, mode, feedback)
            if rc == 0:
                return True, ""
            raw = (stdout or "") + "\n" + (stderr or "")
            signal = classify_provider_failure(raw)
            if signal.kind == "fatal":
                return False, f"Claude (implementer) exited rc={rc}\n{raw[-8000:]}"
            if not self._wait_for_provider(issue, "claude", "codex", signal, run_dir, wait_started, transient_attempt, stage_state):
                return False, f"Claude (implementer) did not recover within the configured window ({signal.kind}).\n{raw[-8000:]}"
            if signal.kind == "transient":
                transient_attempt += 1
            live = ghx.get_issue(self.repo_slug, issue.number)
            ghx.set_state(self.repo_slug, live, "agent:fix" if feedback else "agent:running")
            issue = ghx.get_issue(self.repo_slug, issue.number)

    def _call_provider_implement_with_retry(self, provider: str, issue, worktree: Path, base_sha: str, run_dir: Path, mode: str, feedback: str | None, stage_state: dict):
        if provider == "claude":
            return self._call_claude_implement_with_retry(issue, worktree, base_sha, run_dir, mode, feedback, stage_state)
        ok, err, _ = self._call_codex_with_retry(issue, worktree, base_sha, run_dir, mode, feedback, stage_state)
        return ok, err

    def _call_claude_with_retry(self, issue, worktree: Path, base_sha: str, fast_summary: str, run_dir: Path, stage_state: dict):
        wait_started = time.time()
        transient_attempt = 0
        while True:
            try:
                review = run_claude_review(self.base_dir, self.cfg, worktree, issue, base_sha, fast_summary, run_dir)
                return True, review, ""
            except CommandError as e:
                raw = (e.output or "") + "\n" + str(e)
                signal = classify_provider_failure(raw)
                if signal.kind == "fatal":
                    return False, None, raw[-8000:]
                if not self._wait_for_provider(issue, "claude", "review", signal, run_dir, wait_started, transient_attempt, stage_state):
                    return False, None, f"Claude did not recover within the configured window ({signal.kind}).\n{raw[-8000:]}"
                if signal.kind == "transient":
                    transient_attempt += 1
                live = ghx.get_issue(self.repo_slug, issue.number)
                ghx.set_state(self.repo_slug, live, "agent:review")
                issue = ghx.get_issue(self.repo_slug, issue.number)
            except Exception as e:
                return False, None, f"Reviewer returned an invalid format or an unrecoverable error: {e}"

    def _run_baseline_compare(self, issue, base_sha: str, changed: list[str], phase: str, candidate_results, run_dir: Path):
        policy = self.cfg.get("baseline", {})
        if not policy.get("enabled", True):
            return candidate_results, None
        phases = set(policy.get("compare_phases", ["targeted", "fast"]))
        if phase not in phases:
            return candidate_results, None
        # Issues that share a base commit share its baseline worktree; running
        # checks there twice at once would corrupt both results.
        with self._lock(f"baseline-{base_sha[:12]}"):
            return self._run_baseline_compare_locked(issue, base_sha, changed, phase, candidate_results, run_dir)

    def _run_baseline_compare_locked(self, issue, base_sha: str, changed: list[str], phase: str, candidate_results, run_dir: Path):
        policy = self.cfg.get("baseline", {})
        try:
            with self._lock("git-repo"):
                baseline_wt, _ = ensure_baseline_worktree(self.repo_path, self.cfg["worktree_root"], base_sha)
        except Exception as e:
            if policy.get("required", False):
                return candidate_results, self._block(issue, run_dir, f"Could not prepare the baseline worktree: {e}")
            print(f"\nBASELINE: unavailable ({e}); keeping strict validation.")
            return candidate_results, None

        baseline_dir = self.runtime / "baselines" / base_sha[:12] / phase
        baseline_dir.mkdir(parents=True, exist_ok=True)
        # Ensure ignored dependencies exist in the detached baseline worktree too.
        boot = prepare_workspace(self.cfg, Path(baseline_wt), changed, baseline_dir, force=False)
        failed_boot = [r for r in boot if not r.passed]
        if failed_boot:
            msg = "Baseline bootstrap failed: " + "; ".join(f"{r.name} rc={r.returncode}" for r in failed_boot)
            if policy.get("required", False):
                return candidate_results, self._block(issue, run_dir, msg)
            print("\nBASELINE:", msg)
            return candidate_results, None

        print(f"\nBASELINE COMPARE · {phase} · {base_sha[:12]}")
        baseline_results = run_checks(self.cfg, Path(baseline_wt), changed, phase, baseline_dir)
        candidate_results, inherited = mark_inherited_failures(candidate_results, baseline_results)
        if inherited:
            print("BASELINE-KNOWN: the failure(s) also happen on the exact base commit; they do not count as a fix loop.")
            for line in inherited:
                print("  -", line)
        return candidate_results, None

    def _run_validation_stage(self, issue, worktree: Path, base_sha: str, changed: list[str], phase: str, run_dir: Path):
        results = run_checks(self.cfg, worktree, changed, phase, run_dir)
        if any(not r.passed for r in results) and looks_like_workspace_dependency_failure(results):
            print("\nENVIRONMENT: worktree dependencies are missing. Repairing without using a fix loop...")
            blocked = self._bootstrap_or_block(issue, worktree, changed, run_dir, force=True)
            if blocked:
                return results, blocked
            results = run_checks(self.cfg, worktree, changed, phase, run_dir)

        # Retry test harness failures before asking an LLM to touch code.
        retries = int(self.cfg.get("validation", {}).get("non_llm_retries", 1 if phase in ("targeted", "fast") else 0))
        attempt = 0
        while any(not r.passed for r in results) and attempt < retries:
            attempt += 1
            print(f"\nVALIDATION: plain retry {attempt}/{retries}; does not count as a fix loop.")
            results = rerun_failed_checks(results, phase, run_dir, attempt)

        if any(not r.passed for r in results):
            results, baseline_block = self._run_baseline_compare(issue, base_sha, changed, phase, results, run_dir)
            if baseline_block:
                return results, baseline_block
        return results, None

    def run_issue(self, number: int, dry_run: bool=False, force: bool=False, resume_existing: bool=False, resume_stage: str | None=None) -> dict:
        issue = ghx.get_issue(self.repo_slug, number)
        waiting = "agent:waiting-quota" in issue.labels
        if not force and "agent:ready" not in issue.labels and not waiting and not resume_existing:
            raise CommandError(f"#{number} is not labeled agent:ready. Use mark-ready, resume or --force.")
        ok, reason = self.eligible(issue)
        if not ok and not force and not resume_existing:
            raise CommandError(f"Issue not eligible: {reason}")

        with self._lock("git-repo"):
            wt, branch, base_sha, _created = create_or_reuse_worktree(self.repo_path, self.cfg["worktree_root"], number, issue.title, self.base_branch)
        wt = Path(wt)
        print(f"Issue: #{number} {issue.title}\nBase: {base_sha}\nBranch: {branch}\nWorktree: {wt}\nRisk: {issue.risk}")
        if dry_run:
            print("DRY RUN: no agents are called and no labels or code are changed.")
            return {"status":"dry-run","issue":number,"base":base_sha,"branch":branch,"worktree":str(wt)}

        run_dir = self._run_dir(number)
        (run_dir/"issue.json").write_text(json.dumps(issue.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")

        max_fix = int(self.cfg.get("autopilot", {}).get("max_fix_loops", 3))
        max_review = int(self.cfg.get("autopilot", {}).get("max_review_loops", 2))

        persisted = self.states.load(number) if (waiting or resume_existing) else None
        if persisted and persisted.get("base_sha") == base_sha and persisted.get("worktree") == str(wt):
            stage = persisted.get("stage", "codex")
            fix_count = int(persisted.get("fix_count", 0))
            review_count = int(persisted.get("review_count", 0))
            feedback = persisted.get("feedback")
            fast_summary = persisted.get("fast_summary", "")
            provider = persisted.get("provider") or self.cfg.get("autopilot", {}).get("implementer", "codex")
            print(f"RESUME persisted: stage={stage}, fix={fix_count}, review={review_count}")
        else:
            fix_count = 0
            review_count = 0
            feedback = None
            fast_summary = ""
            provider = self.cfg.get("autopilot", {}).get("implementer", "codex")
            existing_changed = changed_files(wt, base_sha)
            stage = "targeted" if resume_existing and existing_changed else "codex"

        if resume_stage:
            allowed_resume_stages = {"targeted", "fast", "review", "full"}
            if resume_stage not in allowed_resume_stages:
                raise CommandError(f"Invalid resume stage: {resume_stage}")
            if not changed_files(wt, base_sha):
                raise CommandError("Cannot force a resume stage when the worktree has no changes.")
            stage = resume_stage
            print(f"RESUME override: stage={stage}")

        # Unblock explicit human resume without discarding worktree changes.
        live = ghx.get_issue(self.repo_slug, number)
        if resume_existing:
            ghx.set_state(self.repo_slug, live, "agent:running", remove_special=True)
        elif stage == "review":
            ghx.set_state(self.repo_slug, live, "agent:review", remove_special=True)
        else:
            ghx.set_state(self.repo_slug, live, "agent:running", remove_special=True)
        issue = ghx.get_issue(self.repo_slug, number)

        # Worktrees do not share ignored dependencies (node_modules).
        initial_changed = changed_files(wt, base_sha)
        blocked = self._bootstrap_or_block(issue, wt, initial_changed, run_dir)
        if blocked:
            return blocked

        self._save_stage(number, stage=stage, provider=provider, base_sha=base_sha, branch=branch, worktree=str(wt), fix_count=fix_count, review_count=review_count, feedback=feedback, fast_summary=fast_summary)

        while True:
            issue = ghx.get_issue(self.repo_slug, number)

            if stage == "codex":
                mode = "fix" if feedback else "implement"
                label = "Claude/Sonnet" if provider == "claude" else "Codex"
                print(f"\n>>> {label} {mode.upper()} loop {fix_count if feedback else 0}")
                ghx.set_state(self.repo_slug, issue, "agent:fix" if feedback else "agent:running")
                issue = ghx.get_issue(self.repo_slug, number)
                state_payload = dict(base_sha=base_sha, branch=branch, worktree=str(wt), fix_count=fix_count, review_count=review_count, feedback=feedback, fast_summary=fast_summary)
                if provider == "claude":
                    ok_agent, err = self._call_claude_implement_with_retry(issue, wt, base_sha, run_dir, mode, feedback, state_payload)
                else:
                    ok_agent, err, provider = self._call_codex_with_retry(issue, wt, base_sha, run_dir, mode, feedback, state_payload)
                if not ok_agent:
                    return self._block(issue, run_dir, err)
                stage = "targeted"
                self._save_stage(number, stage=stage, provider=provider, **state_payload)
                continue

            changed = changed_files(wt, base_sha)
            if not changed:
                return self._block(issue, run_dir, "There are no changes compared to the base commit.")
            try:
                ensure_no_protected(changed, self.cfg.get("protected_paths", []))
            except CommandError as e:
                return self._block(issue, run_dir, str(e))

            # If the agent changed package manifests, refresh the workspace first.
            blocked = self._bootstrap_or_block(issue, wt, changed, run_dir)
            if blocked:
                return blocked

            if stage in ("targeted", "fast", "full"):
                results, env_block = self._run_validation_stage(issue, wt, base_sha, changed, stage, run_dir)
                if env_block:
                    return env_block
                summary = summarize(results)
                if any(not r.passed for r in results):
                    fix_count += 1
                    if fix_count > max_fix:
                        return self._block(issue, run_dir, f"Ran out of fix loops in {stage} validation.\n" + summary)
                    feedback = failure_feedback(results)
                    stage = "codex"
                    ghx.set_state(self.repo_slug, ghx.get_issue(self.repo_slug, number), "agent:fix")
                    self._save_stage(number, stage=stage, provider=provider, base_sha=base_sha, branch=branch, worktree=str(wt), fix_count=fix_count, review_count=review_count, feedback=feedback, fast_summary=fast_summary)
                    continue
                if stage == "targeted":
                    # `summary` here includes the targeted Playwright/pytest
                    # evidence (e.g. `targeted-auto-playwright.log`). Without
                    # keeping it, the "fast" stage below overwrites
                    # `fast_summary` and the reviewer never sees it, even
                    # though the issue's acceptance criteria explicitly
                    # require full E2E evidence.
                    fast_summary = summary
                    stage = "fast"
                elif stage == "fast":
                    fast_summary = fast_summary + "\n" + summary if fast_summary else summary
                    stage = "review"
                else:
                    stage = "finalize"
                self._save_stage(number, stage=stage, provider=provider, base_sha=base_sha, branch=branch, worktree=str(wt), fix_count=fix_count, review_count=review_count, feedback=feedback, fast_summary=fast_summary)
                continue

            if stage == "review":
                ghx.set_state(self.repo_slug, ghx.get_issue(self.repo_slug, number), "agent:review")
                issue = ghx.get_issue(self.repo_slug, number)
                before = status_porcelain(wt)
                state_payload = dict(base_sha=base_sha, branch=branch, worktree=str(wt), fix_count=fix_count, review_count=review_count, feedback=feedback, fast_summary=fast_summary)
                ok_review, review, err = self._call_claude_with_retry(issue, wt, base_sha, fast_summary, run_dir, state_payload)
                if not ok_review:
                    return self._block(issue, run_dir, f"Reviewer failed: {err}")
                after = status_porcelain(wt)
                if after != before:
                    return self._block(issue, run_dir, "The reviewer modified the working tree; blocked for safety.")
                assert review is not None
                if review["verdict"] == "FAIL":
                    review_count += 1
                    fix_count += 1
                    if review_count > max_review or fix_count > max_fix:
                        return self._block(issue, run_dir, "Reviewer still says FAIL and the loops ran out.\n" + json.dumps(review, ensure_ascii=False))
                    feedback = "REVIEWER BLOCKERS\n" + json.dumps(review.get("blocking", []), indent=2, ensure_ascii=False)
                    stage = "codex"
                    ghx.set_state(self.repo_slug, ghx.get_issue(self.repo_slug, number), "agent:fix")
                else:
                    stage = "full"
                self._save_stage(number, stage=stage, provider=provider, base_sha=base_sha, branch=branch, worktree=str(wt), fix_count=fix_count, review_count=review_count, feedback=feedback, fast_summary=fast_summary)
                continue

            if stage == "finalize":
                # Validation tools may leave untracked generated residue such as
                # .coverage/.next/reports. Remove only the configured disposable
                # artifacts *before* git add -A so they can never leak into the
                # issue commit. cleanup_done_artifacts refuses tracked paths.
                precommit_cleanup = cleanup_done_artifacts(self.cfg, wt)
                if precommit_cleanup:
                    print("\nPRE-COMMIT CLEANUP artefactos regenerables:")
                    for msg in precommit_cleanup:
                        print(f"- {msg}")

                changed = changed_files(wt, base_sha)
                ensure_no_protected(changed, self.cfg.get("protected_paths", []))
                if not changed:
                    return self._block(issue, run_dir, "No source changes left after the pre-commit cleanup.")
                issue = ghx.get_issue(self.repo_slug, number)
                commit_msg = f"fix(issue-{number}): {issue.title[:60]}"
                commit_sha = commit_all(wt, commit_msg)

                integ = self.cfg.get("integration", {})
                if integ.get("push_issue_branch", True):
                    with self._lock("git-repo"):
                        push_issue_branch(wt, branch)

                integrated = False
                integrate_note = None
                if integ.get("auto_integrate") and issue.risk in set(integ.get("allowed_risks", [])):
                    try:
                        with self._lock("git-repo"):
                            fast_forward_remote_base(wt, commit_sha, self.base_branch, base_sha)
                        integrated = True
                    except Exception as e:
                        # A failed fast-forward here (base drift, or the issue
                        # branch needed a real merge commit and so isn't a
                        # direct single-parent descendant of base_sha) is not
                        # a validation failure — the implementation is green
                        # and published. Blocking finalize on it stranded the
                        # issue below `agent:implemented`, where `autopilot.py
                        # integrate` (which requires that label) could never
                        # reach it. Fall through like a risk level that skips
                        # auto-integrate: mark implemented, let `integrate` finish
                        # the merge in its own temp worktree instead.
                        integrate_note = str(e)

                released = False
                if integrated and self._release_gate_active():
                    with self._lock("git-repo"):
                        git_fetch(self.repo_path)
                    released = commit_is_released(
                        self.repo_path, commit_sha, f"origin/{self.deployment_branch}"
                    )
                label, may_close = finalize_lifecycle(
                    integrated=integrated,
                    gate_active=self._release_gate_active(),
                    released=released,
                )
                issue = ghx.get_issue(self.repo_slug, number)
                ghx.set_state(self.repo_slug, issue, label)
                final_msg = (
                    f"Autopilot completed #{number}.\n\n"
                    f"- Branch: `{branch}`\n- Commit: `{commit_sha}`\n- Base: `{base_sha}`\n"
                    f"- Integrated into `{self.base_branch}`: {'yes' if integrated else 'no'}\n"
                    f"- Reviewer: PASS\n- Targeted/Fast validation: PASS\n- Full validation: PASS\n"
                    f"- State: `{label}`"
                )
                if label == ghx.INTEGRATED_LABEL:
                    final_msg += (
                        f"\n\nIntegrated into `{self.base_branch}`, **pending promotion** to "
                        f"`{self.deployment_branch}`. The issue stays open until the work "
                        "is reachable from the deployment branch "
                        "(`release-audit --promote`)."
                    )
                if integ.get("comment_updates", True):
                    ghx.comment(self.repo_slug, number, final_msg)
                if may_close and integ.get("auto_close"):
                    current = ghx.get_issue(self.repo_slug, number)
                    if current.state.upper() == "OPEN":
                        ghx.close_issue(self.repo_slug, number)
                cleanup_messages = precommit_cleanup + cleanup_done_artifacts(self.cfg, wt)
                # Stable de-duplication keeps result.json readable when an artifact
                # was already removed by the pre-commit pass.
                cleanup_messages = list(dict.fromkeys(cleanup_messages))
                if cleanup_messages:
                    print("\nCLEANUP artefactos regenerables:")
                    for msg in cleanup_messages:
                        print(f"- {msg}")

                if integrated:
                    branch_cleanup = delete_issue_branch_if_merged(
                        self.cfg["repo_path"], self.cfg["worktree_root"], number, branch, commit_sha
                    )
                    cleanup_messages = cleanup_messages + branch_cleanup
                    if branch_cleanup:
                        print("\nCLEANUP issue branch/worktree:")
                        for msg in branch_cleanup:
                            print(f"- {msg}")

                result = {
                    "status":"done",
                    "issue":number,
                    "branch":branch,
                    "commit":commit_sha,
                    "integrated":integrated,
                    "run_dir":str(run_dir),
                    "cleanup":cleanup_messages,
                }
                (run_dir/"result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
                self.states.clear(number)
                return result

            return self._block(issue, run_dir, f"Unknown stage: {stage}")

    def _block(self, issue, run_dir: Path, reason: str) -> dict:
        live = ghx.get_issue(self.repo_slug, issue.number)
        ghx.set_state(self.repo_slug, live, "agent:blocked", add=["needs:human"])
        if self.cfg.get("integration", {}).get("comment_updates", True):
            ghx.comment(self.repo_slug, issue.number, "Autopilot blocked this issue.\n\n```text\n" + reason[-8000:] + "\n```\n\nDetailed logs are in the runner's local `runtime/runs/issue-<N>/...` folder.")
        result = {"status":"blocked","issue":issue.number,"reason":reason,"run_dir":str(run_dir)}
        (run_dir/"result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print("\nBLOCKED:", reason)
        return result

    def _auto_ready_tick(self):
        if not self.cfg.get("autopilot", {}).get("auto_ready", False):
            return
        try:
            for row in self.auto_ready_scan():
                if row["action"] == "marked-ready":
                    print(f"AUTO-READY #{row['issue']} -> agent:ready, risk:{row['risk']}")
        except Exception as e:
            print(f"Unhandled error in auto-ready scan: {e}")

    def daemon(self, once: bool=False):
        poll = int(self.cfg.get("autopilot", {}).get("poll_seconds", 120))
        max_parallel = max(1, int(self.cfg.get("autopilot", {}).get("max_parallel", 1) or 1))
        if max_parallel > 1:
            return self._daemon_parallel(once, poll, max_parallel)
        while True:
            self._auto_ready_tick()

            try:
                issue = self.next_issue()
            except Exception as e:
                print(f"Unhandled error looking for the next issue: {e}")
                issue = None

            if issue:
                print(f"\nAutopilot picks #{issue.number}: {issue.title}")
                try:
                    self.run_issue(issue.number)
                except Exception as e:
                    print(f"Unhandled error in #{issue.number}: {e}")
            else:
                print("No eligible issues.")
            if once:
                return
            time.sleep(poll)

    # Seam for tests: parallel runs are separate `run --issue N` processes.
    _popen = staticmethod(subprocess.Popen)

    def _spawn_issue(self, issue, log_root: Path):
        log_root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = log_root / f"issue-{issue.number}-{ts}.log"
        log_file = open(log_path, "w", encoding="utf-8")
        cmd = [sys.executable, "-m", "abi_autopilot", "run", "--issue", str(issue.number)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", ABI_AUTOPILOT_HOME=str(self.base_dir))
        # From a source checkout the package is importable only via the checkout folder.
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(x for x in (package_root, env.get("PYTHONPATH", "")) if x)
        try:
            proc = self._popen(cmd, cwd=str(self.base_dir), stdout=log_file, stderr=subprocess.STDOUT, env=env)
        except Exception:
            log_file.close()
            raise
        print(f"PARALLEL #{issue.number} started (pid {proc.pid}) · {issue.title} · log {log_path}")
        return proc, log_path, log_file

    def _daemon_parallel(self, once: bool, poll: int, max_parallel: int):
        """Run up to `max_parallel` issues at once, each in its own process.

        Each child is a normal `run --issue N` process with its output in
        `runtime/daemon/`. Shared Git operations and baselines are serialized
        with cross-process locks. With `once`, the queue is scanned a single
        time and the daemon returns when every started issue has finished.
        """
        print(f"PARALLEL daemon · up to {max_parallel} issue(s) at once")
        log_root = self.runtime / "daemon"
        running: dict[int, tuple] = {}
        try:
            self._parallel_loop(once, poll, max_parallel, log_root, running)
        finally:
            # On Ctrl+C the children keep their own state; just release our
            # handles on their log files.
            for _proc, _log_path, log_file in running.values():
                log_file.close()

    def _parallel_loop(self, once: bool, poll: int, max_parallel: int, log_root: Path, running: dict):
        last_scan = None
        scanned_once = False
        while True:
            slot_freed = False
            for number, (proc, log_path, log_file) in list(running.items()):
                rc = proc.poll()
                if rc is None:
                    continue
                log_file.close()
                del running[number]
                slot_freed = True
                outcome = "done" if rc == 0 else ("blocked" if rc == 3 else f"failed rc={rc}")
                print(f"PARALLEL #{number} finished · {outcome} · log {log_path}")

            free = max_parallel - len(running)
            now = time.monotonic()
            due = last_scan is None or slot_freed or now - last_scan >= poll
            if free > 0 and due and not (once and scanned_once):
                last_scan = now
                scanned_once = True
                self._auto_ready_tick()
                try:
                    queue = [i for i in self.eligible_queue() if i.number not in running]
                except Exception as e:
                    print(f"Unhandled error looking for eligible issues: {e}")
                    queue = []
                for issue in queue[:free]:
                    try:
                        running[issue.number] = self._spawn_issue(issue, log_root)
                    except Exception as e:
                        print(f"Unhandled error starting #{issue.number}: {e}")
                if not queue and not running:
                    print("No eligible issues.")

            if once and scanned_once and not running:
                return
            time.sleep(min(poll, 5) if running else poll)

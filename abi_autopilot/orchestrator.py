from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import github as ghx
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

    def _run_dir(self, issue_number: int) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        p = self.runtime / "runs" / f"issue-{issue_number}" / ts
        p.mkdir(parents=True, exist_ok=True)
        return p

    def eligible(self, issue) -> tuple[bool, str]:
        if issue.state.upper() != "OPEN":
            return False, "issue cerrada"
        if self.cfg.get("autopilot", {}).get("skip_epics", True) and issue.is_epic:
            return False, "epic omitida por policy"
        if "needs:human" in issue.labels or "needs:product" in issue.labels:
            return False, "requiere humano/product"
        try:
            ok, deps = ghx.deps_status(self.repo_slug, issue)
        except CommandError as e:
            return False, str(e)
        if not ok:
            return False, "dependencias abiertas: " + ", ".join(f"{ref}:{state}" for ref, state in deps if state.upper() != "CLOSED")
        return True, "ok"

    def observe(self):
        if self.cfg.get("autopilot", {}).get("auto_ready", False):
            triaged = [r for r in self.auto_ready_scan() if r["action"] == "marked-ready"]
            if triaged:
                print("AUTO-READY (recién triadas):")
                for row in triaged:
                    print(f"  #{row['issue']} -> agent:ready, risk:{row['risk']}")
                print()

        issues = ghx.list_actionable(self.repo_slug)
        rows = []
        if not issues:
            print("No hay issues agent:ready ni agent:waiting-quota.")
        else:
            for i in sorted(issues, key=lambda x: x.number):
                ok, reason = self.eligible(i)
                deps = ghx.dependency_refs_from_body(i.body)
                state = next((x for x in i.labels if x.startswith("agent:")), "-")
                rows.append((i.number, i.risk, state, "YES" if ok else "NO", str(deps) if deps else "-", reason, i.title))
            print(f"{'#':>5}  {'RISK':<6} {'STATE':<20} {'READY':<5} {'DEPS':<22} {'REASON':<32} TITLE")
            for row in rows:
                print(f"{row[0]:>5}  {row[1]:<6} {row[2]:<20} {row[3]:<5} {row[4][:22]:<22} {row[5][:32]:<32} {row[6]}")

        done = [x for x in ghx.list_done(self.repo_slug) if not x.is_epic]
        print()
        if not done:
            print("No hay issues agent:done esperando integración.")
        else:
            print(f"agent:done pendientes de integrar ({len(done)}) — usá 'integrate-done' para verlas planificadas:")
            print(f"{'#':>5}  {'RISK':<6} TITLE")
            for i in sorted(done, key=lambda x: x.number):
                print(f"{i.number:>5}  {i.risk:<6} {i.title}")

        self.print_release_section()
        return rows

    # ── Release / promoción ─────────────────────────────────────────────────

    def _deployment_separate(self) -> bool:
        return self.deployment_branch != self.base_branch

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
        done = [x for x in ghx.list_done(self.repo_slug, limit) if not x.is_epic]
        rows = [self.issue_release_state(i) for i in sorted(integrated + done, key=lambda x: x.number)]
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
            "rows": rows,
        }

    def print_release_section(self) -> dict:
        overview = self.release_overview()
        print()
        if not overview["separate"]:
            print(
                f"RELEASE · integración y despliegue son la misma rama ({self.base_branch}); "
                "integrar equivale a entregar."
            )
            return overview

        rows = overview["rows"]
        released = [r for r in rows if r["released"] is True]
        pending = [r for r in rows if r["released"] is False]
        unknown = [r for r in rows if r["released"] is None]
        print(
            f"RELEASE · integración `{overview['integration_branch']}` "
            f"→ despliegue `{overview['deployment_branch']}`"
        )
        print(f"  RELEASED           {len(released)}")
        print(f"  PENDING PROMOTION  {len(pending)}")
        if unknown:
            print(f"  SIN RAMA CONOCIDA  {len(unknown)}")
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
            print("  WARNING: integración y rama de despliegue divergieron.")
            print("  No cerrar nuevas issues como released hasta reconciliar.")
        return overview

    def consistency_check(self, limit: int = 200) -> list[dict]:
        """Issues CLOSED cuyo trabajo no llegó a la rama de despliegue.

        Es el chequeo que faltaba: una issue cerrada con su código sólo en la
        rama de integración es una mentira de estado, y así #71, #95 y #97
        figuraron terminadas durante semanas sin estar en `main`.
        """
        if not self._deployment_separate():
            return []
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
                ok, deps = ghx.deps_status(self.repo_slug, issue)
            except CommandError as e:
                rows.append({"issue": issue.number, "action": "skipped", "reason": str(e)})
                continue
            if not ok:
                pending = ", ".join(f"{ref}:{state}" for ref, state in deps if state.upper() != "CLOSED")
                rows.append({"issue": issue.number, "action": "skipped", "reason": f"dependencias abiertas: {pending}"})
                continue
            risk = issue.risk if any(l.startswith("risk:") for l in issue.labels) else default_risk
            ghx.set_risk(self.repo_slug, issue, risk)
            issue = ghx.get_issue(self.repo_slug, issue.number)
            ghx.set_state(self.repo_slug, issue, "agent:ready", remove_special=True)
            rows.append({"issue": issue.number, "action": "marked-ready", "risk": risk})
        return rows

    def next_issue(self):
        candidates = []
        for i in ghx.list_actionable(self.repo_slug):
            ok, _ = self.eligible(i)
            if ok:
                candidates.append(i)
        risk_order = {"low": 0, "medium": 1, "high": 2}
        # Resume quota waits first, then normal ready work.
        return sorted(candidates, key=lambda x: (0 if "agent:waiting-quota" in x.labels else 1, risk_order.get(x.risk, 1), x.number))[0] if candidates else None

    def _save_stage(self, issue_number: int, **updates):
        self.states.save(issue_number, **updates)

    def _bootstrap_or_block(self, issue, worktree: Path, changed: list[str], run_dir: Path, force: bool=False):
        results = prepare_workspace(self.cfg, worktree, changed, run_dir, force=force)
        failed = [r for r in results if not r.passed]
        if failed:
            reason = "Workspace bootstrap falló:\n" + "\n".join(f"- {r.name}: rc={r.returncode} log={r.log}" for r in failed)
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
            print(f"\nWAITING QUOTA · {provider}: reintento en {delay // 60} min {delay % 60} s. No consume fix loop.")
        else:
            self._save_stage(issue.number, stage=stage, provider=provider, waiting_kind="transient", **state_payload)
            print(f"\nTRANSIENT · {provider}: reintento en {delay}s. No consume fix loop.")
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
                return False, f"Codex terminó rc={rc}\n{raw[-8000:]}", "codex"
            # Out of Codex credits/quota and a fallback implementer is configured:
            # switch immediately instead of waiting hours for quota to reset.
            if signal.kind == "quota" and fallback and fallback != "codex":
                print(f"\nCODEX QUOTA agotada — fallback automático a {fallback} como implementador para esta issue.")
                ok, err = self._call_provider_implement_with_retry(fallback, issue, worktree, base_sha, run_dir, mode, feedback, stage_state)
                return ok, err, fallback
            if not self._wait_for_provider(issue, "codex", "codex", signal, run_dir, wait_started, transient_attempt, stage_state):
                return False, f"Codex no se recuperó dentro de la ventana configurada ({signal.kind}).\n{raw[-8000:]}", "codex"
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
                return False, f"Claude (implementer) terminó rc={rc}\n{raw[-8000:]}"
            if not self._wait_for_provider(issue, "claude", "codex", signal, run_dir, wait_started, transient_attempt, stage_state):
                return False, f"Claude (implementer) no se recuperó dentro de la ventana configurada ({signal.kind}).\n{raw[-8000:]}"
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
                    return False, None, f"Claude no se recuperó dentro de la ventana configurada ({signal.kind}).\n{raw[-8000:]}"
                if signal.kind == "transient":
                    transient_attempt += 1
                live = ghx.get_issue(self.repo_slug, issue.number)
                ghx.set_state(self.repo_slug, live, "agent:review")
                issue = ghx.get_issue(self.repo_slug, issue.number)
            except Exception as e:
                return False, None, f"Reviewer devolvió formato inválido o error no recuperable: {e}"

    def _run_baseline_compare(self, issue, base_sha: str, changed: list[str], phase: str, candidate_results, run_dir: Path):
        policy = self.cfg.get("baseline", {})
        if not policy.get("enabled", True):
            return candidate_results, None
        phases = set(policy.get("compare_phases", ["targeted", "fast"]))
        if phase not in phases:
            return candidate_results, None
        try:
            baseline_wt, _ = ensure_baseline_worktree(self.repo_path, self.cfg["worktree_root"], base_sha)
        except Exception as e:
            if policy.get("required", False):
                return candidate_results, self._block(issue, run_dir, f"No se pudo preparar baseline worktree: {e}")
            print(f"\nBASELINE: no disponible ({e}); se mantiene validación estricta.")
            return candidate_results, None

        baseline_dir = self.runtime / "baselines" / base_sha[:12] / phase
        baseline_dir.mkdir(parents=True, exist_ok=True)
        # Ensure ignored dependencies exist in the detached baseline worktree too.
        boot = prepare_workspace(self.cfg, Path(baseline_wt), changed, baseline_dir, force=False)
        failed_boot = [r for r in boot if not r.passed]
        if failed_boot:
            msg = "Baseline bootstrap falló: " + "; ".join(f"{r.name} rc={r.returncode}" for r in failed_boot)
            if policy.get("required", False):
                return candidate_results, self._block(issue, run_dir, msg)
            print("\nBASELINE:", msg)
            return candidate_results, None

        print(f"\nBASELINE COMPARE · {phase} · {base_sha[:12]}")
        baseline_results = run_checks(self.cfg, Path(baseline_wt), changed, phase, baseline_dir)
        candidate_results, inherited = mark_inherited_failures(candidate_results, baseline_results)
        if inherited:
            print("BASELINE-KNOWN: el/los fallo(s) también ocurren en la base exacta; no consumen fix loop.")
            for line in inherited:
                print("  -", line)
        return candidate_results, None

    def _run_validation_stage(self, issue, worktree: Path, base_sha: str, changed: list[str], phase: str, run_dir: Path):
        results = run_checks(self.cfg, worktree, changed, phase, run_dir)
        if any(not r.passed for r in results) and looks_like_workspace_dependency_failure(results):
            print("\nENVIRONMENT: faltan dependencias locales del worktree. Reparando sin gastar fix loop...")
            blocked = self._bootstrap_or_block(issue, worktree, changed, run_dir, force=True)
            if blocked:
                return results, blocked
            results = run_checks(self.cfg, worktree, changed, phase, run_dir)

        # Retry test harness failures before asking an LLM to touch code.
        retries = int(self.cfg.get("validation", {}).get("non_llm_retries", 1 if phase in ("targeted", "fast") else 0))
        attempt = 0
        while any(not r.passed for r in results) and attempt < retries:
            attempt += 1
            print(f"\nVALIDATION: retry determinístico {attempt}/{retries}; no consume fix loop.")
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
            raise CommandError(f"#{number} no tiene agent:ready. Usá mark-ready, resume o --force.")
        ok, reason = self.eligible(issue)
        if not ok and not force and not resume_existing:
            raise CommandError(f"Issue no elegible: {reason}")

        wt, branch, base_sha, _created = create_or_reuse_worktree(self.repo_path, self.cfg["worktree_root"], number, issue.title, self.base_branch)
        wt = Path(wt)
        print(f"Issue: #{number} {issue.title}\nBase: {base_sha}\nBranch: {branch}\nWorktree: {wt}\nRisk: {issue.risk}")
        if dry_run:
            print("DRY RUN: no se llama a agentes, no se cambian labels ni código.")
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
                raise CommandError(f"Stage de resume inválido: {resume_stage}")
            if not changed_files(wt, base_sha):
                raise CommandError("No se puede forzar un stage de resume sin cambios en el worktree.")
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
                return self._block(issue, run_dir, "No hay cambios respecto de la base.")
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
                        return self._block(issue, run_dir, f"Se agotaron fix loops en {stage} validation.\n" + summary)
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
                    return self._block(issue, run_dir, f"Reviewer falló: {err}")
                after = status_porcelain(wt)
                if after != before:
                    return self._block(issue, run_dir, "El reviewer modificó el working tree; se bloquea por seguridad.")
                assert review is not None
                if review["verdict"] == "FAIL":
                    review_count += 1
                    fix_count += 1
                    if review_count > max_review or fix_count > max_fix:
                        return self._block(issue, run_dir, "Reviewer sigue en FAIL y se agotaron loops.\n" + json.dumps(review, ensure_ascii=False))
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
                    return self._block(issue, run_dir, "No quedaron cambios de fuente después del cleanup pre-commit.")
                issue = ghx.get_issue(self.repo_slug, number)
                commit_msg = f"fix(issue-{number}): {issue.title[:60]}"
                commit_sha = commit_all(wt, commit_msg)

                integ = self.cfg.get("integration", {})
                if integ.get("push_issue_branch", True):
                    push_issue_branch(wt, branch)

                integrated = False
                integrate_note = None
                if integ.get("auto_integrate") and issue.risk in set(integ.get("allowed_risks", [])):
                    try:
                        fast_forward_remote_base(wt, commit_sha, self.base_branch, base_sha)
                        integrated = True
                    except Exception as e:
                        # A failed fast-forward here (base drift, or the issue
                        # branch needed a real merge commit and so isn't a
                        # direct single-parent descendant of base_sha) is not
                        # a validation failure — the implementation is green
                        # and published. Blocking finalize on it stranded the
                        # issue below `agent:done`, where `autopilot.py
                        # integrate` (which requires that label) could never
                        # reach it. Fall through like a risk level that skips
                        # auto-integrate: mark done, let `integrate` finish
                        # the merge in its own temp worktree instead.
                        integrate_note = str(e)

                issue = ghx.get_issue(self.repo_slug, number)
                ghx.set_state(self.repo_slug, issue, "agent:done")
                final_msg = (
                    f"Autopilot completó #{number}.\n\n"
                    f"- Branch: `{branch}`\n- Commit: `{commit_sha}`\n- Base inicial: `{base_sha}`\n"
                    f"- Integrado a `{self.base_branch}`: {'sí' if integrated else 'no'}\n"
                    f"- Reviewer: PASS\n- Targeted/Fast validation: PASS\n- Full validation: PASS"
                )
                if integ.get("comment_updates", True):
                    ghx.comment(self.repo_slug, number, final_msg)
                if integrated and integ.get("auto_close"):
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
                        print("\nCLEANUP rama/worktree del issue:")
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

            return self._block(issue, run_dir, f"Stage desconocido: {stage}")

    def _block(self, issue, run_dir: Path, reason: str) -> dict:
        live = ghx.get_issue(self.repo_slug, issue.number)
        ghx.set_state(self.repo_slug, live, "agent:blocked", add=["needs:human"])
        if self.cfg.get("integration", {}).get("comment_updates", True):
            ghx.comment(self.repo_slug, issue.number, "Autopilot bloqueó esta issue.\n\n```text\n" + reason[-8000:] + "\n```\n\nLos logs detallados quedaron en el directorio local `runtime/runs/issue-<N>/...` del runner.")
        result = {"status":"blocked","issue":issue.number,"reason":reason,"run_dir":str(run_dir)}
        (run_dir/"result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print("\nBLOCKED:", reason)
        return result

    def daemon(self, once: bool=False):
        poll = int(self.cfg.get("autopilot", {}).get("poll_seconds", 120))
        auto_ready = self.cfg.get("autopilot", {}).get("auto_ready", False)
        while True:
            if auto_ready:
                try:
                    for row in self.auto_ready_scan():
                        if row["action"] == "marked-ready":
                            print(f"AUTO-READY #{row['issue']} -> agent:ready, risk:{row['risk']}")
                except Exception as e:
                    print(f"Error no controlado en auto-ready scan: {e}")

            try:
                issue = self.next_issue()
            except Exception as e:
                print(f"Error no controlado buscando próxima issue: {e}")
                issue = None

            if issue:
                print(f"\nAutopilot toma #{issue.number}: {issue.title}")
                try:
                    self.run_issue(issue.number)
                except Exception as e:
                    print(f"Error no controlado en #{issue.number}: {e}")
            else:
                print("Sin issues elegibles.")
            if once:
                return
            time.sleep(poll)

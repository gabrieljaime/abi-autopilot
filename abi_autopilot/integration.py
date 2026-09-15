from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from . import github as ghx
from . import __version__
from .bootstrap import prepare_workspace, _safe_remove_generated_dir
from .gitops import (
    git,
    fetch,
    remote_branch_sha,
    current_branch,
    rev_parse,
    changed_between,
    unmerged_files,
    cherry_pick_in_progress,
    local_base_relation,
    find_issue_commit_on_base,
    create_detached_worktree,
    remove_detached_worktree_if_clean,
    push_head_to_base_if_unchanged,
    status_porcelain,
    ensure_baseline_worktree,
    delete_issue_branch_if_merged,
)
from .shell import CommandError
from .validator import (
    run_checks,
    summarize,
    rerun_failed_checks,
    mark_inherited_failures,
)


@dataclass
class IntegrationResult:
    status: str
    issue: int
    issue_commit: str | None = None
    integration_commit: str | None = None
    base_before: str | None = None
    base_after: str | None = None
    worktree: str | None = None
    closed: bool = False
    validation: dict | None = None
    reason: str | None = None
    cleanup: list[str] | None = None


class IntegrationManager:
    """Human-triggered, safety-first integration of ``agent:done`` issues.

    The daemon remains non-integrating by default. This manager only runs when the
    operator explicitly invokes ``autopilot.py integrate`` / ``integrate-done``.

    Normal integration happens in a detached temporary worktree. That means the
    operator's main checkout is never used as a scratch area for cherry-picks,
    builds or E2E artifacts. If a conflict occurs, the temporary worktree is left
    intact and *no* automatic abort/reset/stash/rebase is performed.
    """

    def __init__(self, base_dir: Path, cfg: dict):
        self.base_dir = base_dir
        self.cfg = cfg
        self.repo = Path(cfg["repo_path"])
        self.repo_slug = cfg["repo_slug"]
        self.base_branch = cfg["base_branch"]
        self.worktree_root = Path(cfg["worktree_root"])
        runtime = Path(cfg.get("runtime_dir", "runtime"))
        if not runtime.is_absolute():
            runtime = base_dir / runtime
        self.runtime = runtime
        self.integration_runtime = runtime / "integrations"
        self.integration_runtime.mkdir(parents=True, exist_ok=True)
        self.integration_wt_root = self.worktree_root / "_integration"
        self.integration_wt_root.mkdir(parents=True, exist_ok=True)
        for key, value in cfg.get("process_env", {}).items():
            if value is not None:
                os.environ[str(key)] = str(value)

    def _run_dir(self, number: int) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        p = self.integration_runtime / f"issue-{number}" / ts
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _state_file(self, number: int) -> Path:
        return self.integration_runtime / f"issue-{number}" / "state.json"

    def _save_state(self, number: int, **payload) -> None:
        p = self._state_file(number)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload["issue"] = number
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_state(self, number: int) -> dict | None:
        p = self._state_file(number)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def _clear_state(self, number: int) -> None:
        p = self._state_file(number)
        if p.exists():
            p.unlink()

    def _write_result(self, run_dir: Path, result: IntegrationResult) -> IntegrationResult:
        (run_dir / "result.json").write_text(
            json.dumps(asdict(result), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return result

    def _require_issue_ready_for_integration(self, number: int):
        issue = ghx.get_issue(self.repo_slug, number)
        if issue.is_epic:
            raise CommandError(
                f"#{number} es una EPIC. El integrador no cierra epics automáticamente; "
                "hay que verificar hijos en vivo antes del cierre."
            )
        if issue.state.upper() != "OPEN":
            return issue, None
        if "agent:done" not in issue.labels:
            raise CommandError(f"#{number} no tiene agent:done; no es elegible para integración manual.")
        evidence = ghx.get_completion_evidence(self.repo_slug, number)
        if evidence is None:
            raise CommandError(f"#{number} no tiene comentario de finalización estructurado de Autopilot.")
        if not (evidence.reviewer_pass and evidence.targeted_fast_pass and evidence.full_pass):
            raise CommandError(
                f"#{number} no tiene evidencia completa PASS de implementador/reviewer/validaciones."
            )
        return issue, evidence

    def _cleanup_generated(self, root: Path) -> list[str]:
        integ = self.cfg.get("integration", {})
        if not integ.get("cleanup_generated", True):
            return []
        messages: list[str] = []
        for rel in integ.get("generated_artifacts", []):
            rel = str(rel).replace("\\", "/").strip("/")
            if not rel or rel in (".", "..") or rel.startswith("../"):
                messages.append(f"SKIP unsafe cleanup path: {rel}")
                continue
            p = root / rel
            if not os.path.lexists(p):
                continue
            # Never remove something tracked by Git. Generated cleanup is only for
            # untracked/ignored residue such as .coverage and Playwright reports.
            tracked = git(root, ["ls-files", "--error-unmatch", "--", rel], check=False).returncode == 0
            if tracked:
                messages.append(f"SKIP tracked path: {rel}")
                continue
            try:
                _safe_remove_generated_dir(p)
                messages.append(f"removed {rel}")
            except Exception as e:
                messages.append(f"WARN {rel}: {e}")
        return messages

    def _ensure_validation_tree_clean(self, wt: Path) -> list[str]:
        cleanup = self._cleanup_generated(wt)
        dirty = status_porcelain(wt)
        if dirty.strip():
            raise CommandError(
                "Los tests/builds dejaron cambios no regenerables en el worktree de integración. "
                "Se preserva para inspección:\n" + dirty[-8000:]
            )
        return cleanup

    def _run_phase_with_retry_and_baseline(
        self,
        issue,
        wt: Path,
        changed: list[str],
        phase: str,
        run_dir: Path,
        base_sha: str,
    ):
        results = run_checks(self.cfg, wt, changed, phase, run_dir)
        retries = int(self.cfg.get("validation", {}).get("non_llm_retries", 1 if phase in ("targeted", "fast", "batch_fast") else 0))
        attempt = 0
        while any(not r.passed for r in results) and attempt < retries:
            attempt += 1
            print(f"\nINTEGRATION VALIDATION: retry determinístico {attempt}/{retries} · {phase}")
            results = rerun_failed_checks(results, phase, run_dir, attempt)

        if any(not r.passed for r in results):
            policy = self.cfg.get("baseline", {})
            phases = set(policy.get("compare_phases", ["targeted", "fast"]))
            if policy.get("enabled", True) and phase in phases:
                try:
                    baseline_wt, _ = ensure_baseline_worktree(
                        str(self.repo), self.cfg["worktree_root"], base_sha
                    )
                    baseline_dir = self.runtime / "baselines" / base_sha[:12] / f"integration-{phase}"
                    baseline_dir.mkdir(parents=True, exist_ok=True)
                    boot = prepare_workspace(self.cfg, Path(baseline_wt), changed, baseline_dir, force=False)
                    if all(x.passed for x in boot):
                        baseline_results = run_checks(self.cfg, Path(baseline_wt), changed, phase, baseline_dir)
                        results, inherited = mark_inherited_failures(results, baseline_results)
                        if inherited:
                            print("\nINTEGRATION BASELINE-KNOWN:")
                            for line in inherited:
                                print("  -", line)
                except Exception as e:
                    print(f"\nINTEGRATION BASELINE no disponible: {e}")
        return results

    def _validate(self, issue, wt: Path, changed: list[str], run_dir: Path, base_sha: str) -> dict:
        code_changed = any(x.startswith("backend/") or x.startswith("frontend/") for x in changed)
        summary: dict[str, str] = {}

        boot = prepare_workspace(self.cfg, wt, changed, run_dir, force=False)
        failed_boot = [x for x in boot if not x.passed]
        if failed_boot:
            raise CommandError(
                "Bootstrap de integración falló: " + "; ".join(
                    f"{x.name} rc={x.returncode} log={x.log}" for x in failed_boot
                )
            )

        integ = self.cfg.get("integration", {})
        phases: list[str] = []
        if code_changed and integ.get("validate_targeted", True):
            phases.append("targeted")
        if code_changed and integ.get("validate_fast", True):
            phases.append("fast")

        need_full = False
        if code_changed:
            need_full = issue.risk in set(integ.get("full_for_risks", ["medium", "high"]))
            if integ.get("full_if_frontend_changed", True) and any(x.startswith("frontend/") for x in changed):
                need_full = True
        if need_full:
            phases.append("full")

        if not phases:
            summary["validation"] = "SKIP docs/non-code only"

        for phase in phases:
            results = self._run_phase_with_retry_and_baseline(issue, wt, changed, phase, run_dir, base_sha)
            text = summarize(results)
            summary[phase] = text
            print(f"\nINTEGRATION {phase.upper()} SUMMARY\n{text}")
            if any(not r.passed for r in results):
                raise CommandError(f"Integración bloqueada por {phase} validation:\n{text}")

        git(wt, ["diff", "--check", f"{base_sha}..HEAD"])
        summary["git_show_check"] = "PASS"
        return summary

    def _verify_issue_branch(self, evidence) -> None:
        # Fetch all remote tracking refs, then verify the exact issue branch head
        # still matches the commit declared by the completion comment.
        fetch(str(self.repo))
        p = git(
            self.repo,
            ["ls-remote", "origin", f"refs/heads/{evidence.branch}"],
            check=False,
        )
        line = (p.stdout or "").strip()
        if line:
            branch_sha = line.split()[0].lower()
            if branch_sha != evidence.commit:
                raise CommandError(
                    f"La rama {evidence.branch} cambió después del PASS: "
                    f"comentario={evidence.commit[:12]} remoto={branch_sha[:12]}"
                )
        # A deleted issue branch is still acceptable only if the commit object is
        # available after fetch. We never substitute another SHA.
        exists = git(self.repo, ["cat-file", "-e", f"{evidence.commit}^{{commit}}"], check=False)
        if exists.returncode != 0:
            raise CommandError(f"No está disponible el commit declarado por Autopilot: {evidence.commit}")

    def _matching_local_candidate(self, issue_number: int, remote_sha: str) -> str | None:
        local_sha, ahead, behind = local_base_relation(self.repo, self.base_branch, remote_sha)
        if not local_sha:
            return None
        if ahead == 1 and behind == 0:
            subject = git(self.repo, ["show", "-s", "--format=%s", local_sha]).stdout.strip()
            if re.search(rf"\bfix\(issue-{issue_number}\):", subject, re.I):
                return local_sha
        if ahead > 0:
            raise CommandError(
                f"La rama local {self.base_branch} tiene {ahead} commit(s) no publicados "
                f"respecto del remoto. No voy a integrar por detrás de esos cambios. "
                f"Si el commit local corresponde exactamente a la issue, dejalo como único commit ahead y reintentá."
            )
        return None

    def _maybe_sync_local_base(self, old_remote: str, new_remote: str) -> str:
        if not self.cfg.get("integration", {}).get("sync_clean_local_base_after_push", True):
            return "sync local deshabilitado"
        try:
            # Cleanup only known generated residue before deciding whether the
            # operator's checkout is safe to fast-forward.
            self._cleanup_generated(self.repo)
            if current_branch(self.repo) != self.base_branch:
                return "checkout local en otra rama; no se sincronizó"
            if status_porcelain(self.repo).strip():
                return "checkout local con cambios; no se sincronizó"
            local = rev_parse(self.repo, "HEAD")
            if local == new_remote:
                return "checkout local ya sincronizado"
            if local != old_remote:
                return "checkout local no coincide con base previa; no se sincronizó"
            git(self.repo, ["pull", "--ff-only", "origin", self.base_branch])
            return f"checkout local fast-forward a {new_remote[:12]}"
        except Exception as e:
            return f"WARN sync local: {e}"

    def _close_after_live_verification(
        self,
        issue,
        issue_commit: str,
        integration_commit: str,
        base_before: str,
        validation: dict,
        close: bool,
        expected_remote_head: str | None = None,
    ) -> bool:
        # Live branch verification happens immediately before the issue mutation.
        # In batch mode the issue-specific integration commit can be an ancestor
        # of the final accumulated remote head, so verify the explicit final head.
        expected = expected_remote_head or integration_commit
        live_sha = remote_branch_sha(str(self.repo), self.base_branch)
        if live_sha != expected:
            raise CommandError(
                f"Verificación live falló: origin/{self.base_branch}={live_sha[:12]}, "
                f"esperaba {expected[:12]}"
            )
        if self.cfg.get("integration", {}).get("comment_updates", True):
            ghx.comment(
                self.repo_slug,
                issue.number,
                f"Autopilot v{__version__} integró esta issue.\n\n"
                f"- Commit de issue: `{issue_commit}`\n"
                f"- Base previa: `{base_before}`\n"
                f"- Commit integrado: `{integration_commit}`\n"
                f"- Rama: `{self.base_branch}`\n"
                "- Validación de integración: PASS\n"
                f"- Fases: `{', '.join(validation.keys())}`",
            )

        # Required live fetch immediately before closure. We do not close epics.
        live_issue = ghx.get_issue(self.repo_slug, issue.number)
        if not close or not self.cfg.get("integration", {}).get("manual_close_after_success", True):
            return live_issue.state.upper() == "CLOSED"
        if live_issue.is_epic:
            raise CommandError("Refusing automatic epic closure from integration path.")
        if live_issue.state.upper() == "OPEN":
            if "agent:done" not in live_issue.labels:
                raise CommandError("La issue perdió agent:done antes del cierre; se deja abierta.")
            ghx.close_issue(self.repo_slug, issue.number)
        verify = ghx.get_issue(self.repo_slug, issue.number)
        if verify.state.upper() != "CLOSED":
            raise CommandError("GitHub no confirmó el cierre de la issue.")
        return True

    def _validate_existing_or_adopted_commit(
        self,
        issue,
        issue_commit: str,
        candidate_sha: str,
        base_before: str,
        run_dir: Path,
        close: bool,
        source: str,
        branch: str | None = None,
    ) -> IntegrationResult:
        wt = self.integration_wt_root / f"issue-{issue.number}-{candidate_sha[:10]}"
        if wt.exists():
            ok, msg = remove_detached_worktree_if_clean(self.repo, wt)
            if not ok:
                raise CommandError(f"Worktree temporal previo no se puede reciclar: {msg} · {wt}")
        create_detached_worktree(self.repo, wt, candidate_sha)
        self._save_state(
            issue.number,
            stage="validating",
            worktree=str(wt),
            issue_commit=issue_commit,
            candidate_sha=candidate_sha,
            base_before=base_before,
            source=source,
        )
        try:
            changed = changed_between(wt, base_before, candidate_sha)
            validation = self._validate(issue, wt, changed, run_dir, base_before)
            cleanup = self._ensure_validation_tree_clean(wt)

            if source == "adopt-current":
                # Candidate already exists locally; push that exact SHA after
                # verifying the remote base has not moved.
                live_before = remote_branch_sha(str(self.repo), self.base_branch)
                if live_before != base_before:
                    raise CommandError(
                        f"La base remota cambió durante la validación: {live_before[:12]} != {base_before[:12]}"
                    )
                git(wt, ["push", "origin", f"{candidate_sha}:refs/heads/{self.base_branch}"])
                live_after = remote_branch_sha(str(self.repo), self.base_branch)
                if live_after != candidate_sha:
                    raise CommandError("Push del commit local adoptado no quedó en el SHA esperado.")
            else:
                # already-integrated recovery path: no push required.
                live_after = remote_branch_sha(str(self.repo), self.base_branch)
                if live_after != candidate_sha and source == "already-integrated-head":
                    # If later commits exist, the current base head is still what
                    # we validated; candidate_sha is the current head in this path.
                    raise CommandError("La base remota se movió durante la validación de reconciliación.")

            closed = self._close_after_live_verification(
                issue, issue_commit, candidate_sha, base_before, validation, close
            )
            sync = self._maybe_sync_local_base(base_before, candidate_sha)
            validation["local_sync"] = sync
            ok, wt_msg = remove_detached_worktree_if_clean(self.repo, wt)
            if not ok:
                cleanup.append(f"WARN {wt_msg}: {wt}")
            else:
                cleanup.append(wt_msg)
            if branch:
                cleanup.extend(
                    delete_issue_branch_if_merged(self.repo, self.worktree_root, issue.number, branch, issue_commit)
                )
            self._clear_state(issue.number)
            return self._write_result(
                run_dir,
                IntegrationResult(
                    status="integrated" if source == "adopt-current" else "reconciled",
                    issue=issue.number,
                    issue_commit=issue_commit,
                    integration_commit=candidate_sha,
                    base_before=base_before,
                    base_after=candidate_sha,
                    worktree=str(wt),
                    closed=closed,
                    validation=validation,
                    cleanup=cleanup,
                ),
            )
        except Exception as e:
            self._save_state(
                issue.number,
                stage="validation_failed",
                worktree=str(wt),
                issue_commit=issue_commit,
                candidate_sha=candidate_sha,
                base_before=base_before,
                source=source,
                reason=str(e),
            )
            return self._write_result(
                run_dir,
                IntegrationResult(
                    status="blocked",
                    issue=issue.number,
                    issue_commit=issue_commit,
                    integration_commit=candidate_sha,
                    base_before=base_before,
                    worktree=str(wt),
                    reason=str(e),
                ),
            )

    def integrate_issue(self, number: int, *, close: bool = True, continue_existing: bool = False) -> IntegrationResult:
        run_dir = self._run_dir(number)
        issue, evidence = self._require_issue_ready_for_integration(number)
        if issue.state.upper() == "CLOSED":
            return self._write_result(
                run_dir,
                IntegrationResult(status="already-closed", issue=number, closed=True),
            )
        assert evidence is not None
        self._verify_issue_branch(evidence)
        fetch(str(self.repo))
        remote_base = remote_branch_sha(str(self.repo), self.base_branch)

        # Recovery/continue is intentionally explicit. Never abort or overwrite a
        # conflicted worktree behind the operator's back.
        if continue_existing:
            state = self._load_state(number)
            if not state:
                raise CommandError(f"No hay estado de integración pendiente para #{number}.")
            wt = Path(state["worktree"])
            if not wt.exists():
                raise CommandError(f"El worktree guardado ya no existe: {wt}")
            if state.get("stage") in {"conflict", "validation_failed"}:
                conflicts = unmerged_files(wt)
                if conflicts:
                    raise CommandError(
                        "Todavía hay conflictos sin resolver: " + ", ".join(conflicts)
                    )
                if cherry_pick_in_progress(wt):
                    p = git(wt, ["cherry-pick", "--continue"], check=False)
                    if p.returncode != 0:
                        raise CommandError("git cherry-pick --continue falló", p.returncode, p.stdout + p.stderr)
                candidate = rev_parse(wt, "HEAD")
                base_before = state["base_before"]
                changed = changed_between(wt, base_before, candidate)
                try:
                    validation = self._validate(issue, wt, changed, run_dir, base_before)
                    cleanup = self._ensure_validation_tree_clean(wt)
                    integrated_sha = push_head_to_base_if_unchanged(wt, self.base_branch, base_before)
                    closed = self._close_after_live_verification(
                        issue, evidence.commit, integrated_sha, base_before, validation, close
                    )
                    sync = self._maybe_sync_local_base(base_before, integrated_sha)
                    validation["local_sync"] = sync
                    ok, msg = remove_detached_worktree_if_clean(self.repo, wt)
                    cleanup.append(msg if ok else f"WARN {msg}: {wt}")
                    cleanup.extend(
                        delete_issue_branch_if_merged(
                            self.repo, self.worktree_root, number, evidence.branch, evidence.commit
                        )
                    )
                    self._clear_state(number)
                    return self._write_result(
                        run_dir,
                        IntegrationResult(
                            status="integrated",
                            issue=number,
                            issue_commit=evidence.commit,
                            integration_commit=integrated_sha,
                            base_before=base_before,
                            base_after=integrated_sha,
                            worktree=str(wt),
                            closed=closed,
                            validation=validation,
                            cleanup=cleanup,
                        ),
                    )
                except Exception as e:
                    updated_state = dict(state)
                    updated_state.update(stage="validation_failed", reason=str(e))
                    self._save_state(number, **updated_state)
                    return self._write_result(
                        run_dir,
                        IntegrationResult(
                            status="blocked", issue=number, issue_commit=evidence.commit,
                            base_before=base_before, worktree=str(wt), reason=str(e)
                        ),
                    )
            raise CommandError(
                f"El estado pendiente está en stage={state.get('stage')}; "
                "--continue sólo continúa conflictos resueltos por ahora."
            )

        # If the operator already cherry-picked exactly this issue locally (the
        # current #27 situation), adopt that one local commit instead of creating a
        # duplicate commit with a different SHA.
        local_candidate = self._matching_local_candidate(number, remote_base)
        if local_candidate:
            print(
                f"INTEGRATE: detecté commit local no publicado para #{number}: {local_candidate[:12]}. "
                "Lo valido en worktree aislado y, si queda verde, publico ese SHA exacto."
            )
            self._cleanup_generated(self.repo)
            if status_porcelain(self.repo).strip():
                raise CommandError(
                    "La rama local candidata tiene además cambios sin commit. Sólo puedo adoptar un commit limpio.\n"
                    + status_porcelain(self.repo)[-8000:]
                )
            return self._validate_existing_or_adopted_commit(
                issue, evidence.commit, local_candidate, remote_base, run_dir, close, "adopt-current",
                branch=evidence.branch,
            )

        # Detect a prior successful integration by its canonical commit subject.
        already = find_issue_commit_on_base(self.repo, self.base_branch, number)
        if already:
            # Validate the *current* remote head so later integrations are covered;
            # then reconcile/close the issue without duplicating a cherry-pick.
            current_head = remote_branch_sha(str(self.repo), self.base_branch)
            print(f"INTEGRATE: #{number} ya aparece en el historial remoto ({already[:12]}). Reconciliando.")
            parent = rev_parse(self.repo, f"{already}^")
            return self._validate_existing_or_adopted_commit(
                issue, evidence.commit, current_head, parent, run_dir, close, "already-integrated-head",
                branch=evidence.branch,
            )

        wt = self.integration_wt_root / f"issue-{number}-{remote_base[:10]}"
        if wt.exists():
            ok, msg = remove_detached_worktree_if_clean(self.repo, wt)
            if not ok:
                raise CommandError(f"Hay un worktree temporal previo que se preserva: {msg} · {wt}")
        create_detached_worktree(self.repo, wt, remote_base)
        self._save_state(
            number,
            stage="cherry-pick",
            worktree=str(wt),
            issue_commit=evidence.commit,
            base_before=remote_base,
            source="fresh",
        )

        if git(self.repo, ["merge-base", "--is-ancestor", evidence.commit, remote_base], check=False).returncode == 0:
            return self._validate_existing_or_adopted_commit(
                issue, evidence.commit, remote_base, remote_base, run_dir, close, "already-integrated-head",
                branch=evidence.branch,
            )

        print(f"\nINTEGRATE #{number} · base {remote_base[:12]} · issue {evidence.commit[:12]}")
        # Pick the whole range of commits unique to the issue branch, not just its
        # tip: a branch can carry more than one commit (e.g. a resumed run added a
        # second commit on top of an earlier one) and a partial base already
        # contains the intermediate commit's dependencies. Cherry-picking only the
        # tip then conflicts on content that never made it into the base.
        cp = git(wt, ["cherry-pick", f"{remote_base}..{evidence.commit}"], check=False)
        if cp.returncode != 0:
            conflicts = unmerged_files(wt)
            reason = (
                "Cherry-pick requiere intervención. No se hizo abort/reset/stash/rebase. "
                f"Worktree preservado: {wt}."
            )
            if conflicts:
                reason += " Conflictos: " + ", ".join(conflicts)
            else:
                reason += " Salida: " + ((cp.stdout or "") + (cp.stderr or ""))[-4000:]
            self._save_state(
                number,
                stage="conflict",
                worktree=str(wt),
                issue_commit=evidence.commit,
                base_before=remote_base,
                source="fresh",
                conflicts=conflicts,
                reason=reason,
            )
            return self._write_result(
                run_dir,
                IntegrationResult(
                    status="conflict",
                    issue=number,
                    issue_commit=evidence.commit,
                    base_before=remote_base,
                    worktree=str(wt),
                    reason=reason,
                ),
            )

        candidate = rev_parse(wt, "HEAD")
        self._save_state(
            number,
            stage="validating",
            worktree=str(wt),
            issue_commit=evidence.commit,
            candidate_sha=candidate,
            base_before=remote_base,
            source="fresh",
        )
        changed = changed_between(wt, remote_base, candidate)

        try:
            validation = self._validate(issue, wt, changed, run_dir, remote_base)
            cleanup = self._ensure_validation_tree_clean(wt)
            integrated_sha = push_head_to_base_if_unchanged(wt, self.base_branch, remote_base)
            closed = self._close_after_live_verification(
                issue, evidence.commit, integrated_sha, remote_base, validation, close
            )
            sync = self._maybe_sync_local_base(remote_base, integrated_sha)
            validation["local_sync"] = sync
            ok, wt_msg = remove_detached_worktree_if_clean(self.repo, wt)
            cleanup.append(wt_msg if ok else f"WARN {wt_msg}: {wt}")
            cleanup.extend(
                delete_issue_branch_if_merged(self.repo, self.worktree_root, number, evidence.branch, evidence.commit)
            )
            self._clear_state(number)
            return self._write_result(
                run_dir,
                IntegrationResult(
                    status="integrated",
                    issue=number,
                    issue_commit=evidence.commit,
                    integration_commit=integrated_sha,
                    base_before=remote_base,
                    base_after=integrated_sha,
                    worktree=str(wt),
                    closed=closed,
                    validation=validation,
                    cleanup=cleanup,
                ),
            )
        except Exception as e:
            self._save_state(
                number,
                stage="validation_failed",
                worktree=str(wt),
                issue_commit=evidence.commit,
                candidate_sha=candidate,
                base_before=remote_base,
                source="fresh",
                reason=str(e),
            )
            return self._write_result(
                run_dir,
                IntegrationResult(
                    status="blocked",
                    issue=number,
                    issue_commit=evidence.commit,
                    integration_commit=candidate,
                    base_before=remote_base,
                    worktree=str(wt),
                    reason=str(e),
                ),
            )

    def _batch_run_dir(self) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        p = self.integration_runtime / "batches" / ts
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _validate_batch_phase(
        self,
        issue,
        wt: Path,
        changed: list[str],
        phase: str,
        run_dir: Path,
        baseline_sha: str,
    ) -> str:
        results = self._run_phase_with_retry_and_baseline(
            issue, wt, changed, phase, run_dir, baseline_sha
        )
        text = summarize(results)
        print(f"\nBATCH {phase.upper()} SUMMARY\n{text}")
        if any(not r.passed for r in results):
            raise CommandError(f"Batch bloqueado por {phase} validation:\n{text}")
        return text

    def _integrate_batch(self, queue: list, *, close: bool) -> list[dict]:
        """Stage a whole agent:done queue, validate once cumulatively, then publish.

        Safety properties:
        - no issue is pushed or closed before the final accumulated gate passes;
        - each issue still gets targeted + cheap checks after its cherry-pick;
        - one detached worktree carries the cumulative batch;
        - a conflict is preserved in that worktree with no abort/reset/stash/rebase;
        - the final push is refused if origin/base moved meanwhile.
        """
        batch_cfg = self.cfg.get("batch_validation", {})
        run_dir = self._batch_run_dir()

        # Verify every issue and its exact agent commit before touching a worktree.
        prepared: list[tuple[object, object]] = []
        for issue in queue:
            live_issue, evidence = self._require_issue_ready_for_integration(issue.number)
            if live_issue.state.upper() == "CLOSED":
                continue
            assert evidence is not None
            self._verify_issue_branch(evidence)
            prepared.append((live_issue, evidence))

        if not prepared:
            return []

        fetch(str(self.repo))
        remote_base = remote_branch_sha(str(self.repo), self.base_branch)
        local_sha, ahead, behind = local_base_relation(self.repo, self.base_branch, remote_base)
        if ahead > 0:
            raise CommandError(
                f"La rama local {self.base_branch} tiene {ahead} commit(s) no publicados. "
                "Integralos/reconcílialos primero con `integrate --issue N`; el batch no los adopta."
            )

        wt = self.integration_wt_root / f"batch-{remote_base[:10]}-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        if wt.exists():
            ok, msg = remove_detached_worktree_if_clean(self.repo, wt)
            if not ok:
                raise CommandError(f"Hay un batch worktree previo que se preserva: {msg} · {wt}")
        create_detached_worktree(self.repo, wt, remote_base)

        staged: list[dict] = []
        previous = remote_base
        try:
            for issue, evidence in prepared:
                issue_run = run_dir / f"issue-{issue.number}"
                issue_run.mkdir(parents=True, exist_ok=True)
                print(
                    f"\nBATCH STAGE #{issue.number} · parent {previous[:12]} · "
                    f"issue {evidence.commit[:12]} · risk:{issue.risk}"
                )

                # An issue's commit can already be reachable from the accumulated
                # batch head (e.g. it landed via an earlier issue's diff, or via a
                # separate integration run that hasn't closed the GitHub issue yet).
                # Cherry-picking it again would either conflict or produce an empty
                # commit; detect it up front via ancestry proof and skip cleanly
                # instead of leaving a half-finished cherry-pick for a human.
                already_ancestor = git(
                    wt, ["merge-base", "--is-ancestor", evidence.commit, previous], check=False
                ).returncode == 0
                if already_ancestor:
                    print(f"BATCH SKIP #{issue.number}: el commit ya es ancestro de la base acumulada; nada que aplicar.")
                    staged.append({
                        "issue": issue,
                        "evidence": evidence,
                        "parent": previous,
                        "candidate": previous,
                        "changed": [],
                        "validation": {"validation": "SKIP: commit ya integrado en la base acumulada"},
                        "cleanup": [],
                    })
                    continue

                # Same range logic as the single-issue path: pick every commit
                # unique to this issue branch relative to the accumulated batch
                # head, not just its tip commit.
                cp = git(wt, ["cherry-pick", f"{previous}..{evidence.commit}"], check=False)
                if cp.returncode != 0:
                    conflicts = unmerged_files(wt)
                    reason = (
                        "Batch cherry-pick requiere intervención. No se hizo "
                        f"abort/reset/stash/rebase. Worktree preservado: {wt}."
                    )
                    if conflicts:
                        reason += " Conflictos: " + ", ".join(conflicts)
                    else:
                        reason += " Salida: " + ((cp.stdout or "") + (cp.stderr or ""))[-4000:]
                    self._save_state(
                        issue.number,
                        stage="batch_conflict",
                        worktree=str(wt),
                        issue_commit=evidence.commit,
                        base_before=remote_base,
                        candidate_before=previous,
                        batch_issues=[x.number for x, _ in prepared],
                        conflicts=conflicts,
                        reason=reason,
                    )
                    rows = [
                        {
                            "status": "staged-not-pushed",
                            "issue": x["issue"].number,
                            "issue_commit": x["evidence"].commit,
                            "integration_commit": x["candidate"],
                            "closed": False,
                            "validation": x["validation"],
                            "worktree": str(wt),
                        }
                        for x in staged
                    ]
                    rows.append({
                        "status": "conflict",
                        "issue": issue.number,
                        "issue_commit": evidence.commit,
                        "closed": False,
                        "worktree": str(wt),
                        "reason": reason,
                    })
                    (run_dir / "batch-result.json").write_text(
                        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    return rows

                candidate = rev_parse(wt, "HEAD")
                changed = changed_between(wt, previous, candidate)
                code_changed = any(
                    x.startswith("backend/") or x.startswith("frontend/") for x in changed
                )
                validation: dict[str, str] = {}

                boot = prepare_workspace(self.cfg, wt, changed, issue_run, force=False)
                failed_boot = [x for x in boot if not x.passed]
                if failed_boot:
                    raise CommandError(
                        "Bootstrap batch falló: " + "; ".join(
                            f"{x.name} rc={x.returncode} log={x.log}" for x in failed_boot
                        )
                    )

                if code_changed and self.cfg.get("integration", {}).get("validate_targeted", True):
                    validation["targeted"] = self._validate_batch_phase(
                        issue, wt, changed, "targeted", issue_run, remote_base
                    )
                if code_changed:
                    validation["batch_fast"] = self._validate_batch_phase(
                        issue, wt, changed, "batch_fast", issue_run, remote_base
                    )
                if not code_changed:
                    validation["validation"] = "SKIP docs/non-code only"

                # High-risk changes may demand an additional accumulated full gate
                # immediately after staging them. The final gate still runs once at end.
                if code_changed and issue.risk == "high" and batch_cfg.get("force_full_for_high_risk", True):
                    cumulative = changed_between(wt, remote_base, candidate)
                    validation["high_risk_full"] = self._validate_batch_phase(
                        issue, wt, cumulative, "batch_full", issue_run, remote_base
                    )

                git(wt, ["diff", "--check", f"{previous}..{candidate}"])
                cleanup = self._ensure_validation_tree_clean(wt)
                staged.append({
                    "issue": issue,
                    "evidence": evidence,
                    "parent": previous,
                    "candidate": candidate,
                    "changed": changed,
                    "validation": validation,
                    "cleanup": cleanup,
                })
                previous = candidate

            final_head = rev_parse(wt, "HEAD")
            cumulative_changed = changed_between(wt, remote_base, final_head)
            final_validation = "No code checks required."
            if any(x.startswith("backend/") or x.startswith("frontend/") for x in cumulative_changed):
                final_dir = run_dir / "final"
                final_dir.mkdir(parents=True, exist_ok=True)
                boot = prepare_workspace(self.cfg, wt, cumulative_changed, final_dir, force=False)
                failed_boot = [x for x in boot if not x.passed]
                if failed_boot:
                    raise CommandError(
                        "Bootstrap del gate final falló: " + "; ".join(
                            f"{x.name} rc={x.returncode} log={x.log}" for x in failed_boot
                        )
                    )
                # `batch_full` contains the expensive broad suites/build/E2E and
                # runs exactly once for the accumulated batch.
                final_validation = self._validate_batch_phase(
                    staged[-1]["issue"], wt, cumulative_changed,
                    "batch_full", final_dir, remote_base
                )
            git(wt, ["diff", "--check", f"{remote_base}..HEAD"])
            final_cleanup = self._ensure_validation_tree_clean(wt)

            # Publish only after every per-issue check and the final gate are green.
            pushed = push_head_to_base_if_unchanged(wt, self.base_branch, remote_base)
            if pushed != final_head:
                raise CommandError("El batch publicado no coincide con el HEAD validado.")

            results: list[dict] = []
            for row in staged:
                validation = dict(row["validation"])
                validation["batch_full_final"] = final_validation
                validation["git_show_check"] = "PASS"
                try:
                    closed = self._close_after_live_verification(
                        row["issue"], row["evidence"].commit, row["candidate"],
                        row["parent"], validation, close,
                        expected_remote_head=final_head,
                    )
                    status = "integrated"
                    reason = None
                except Exception as e:
                    # Git is already safely published. Leave this issue open for
                    # reconciliation rather than rolling anything back.
                    closed = False
                    status = "published-needs-reconcile"
                    reason = str(e)
                self._clear_state(row["issue"].number)
                branch_cleanup = delete_issue_branch_if_merged(
                    self.repo, self.worktree_root, row["issue"].number, row["evidence"].branch, row["evidence"].commit
                )
                results.append({
                    "status": status,
                    "issue": row["issue"].number,
                    "issue_commit": row["evidence"].commit,
                    "integration_commit": row["candidate"],
                    "batch_head": final_head,
                    "base_before": row["parent"],
                    "base_after": row["candidate"],
                    "worktree": str(wt),
                    "closed": closed,
                    "validation": validation,
                    "reason": reason,
                    "cleanup": row["cleanup"] + branch_cleanup,
                })

            sync = self._maybe_sync_local_base(remote_base, final_head)
            for row in results:
                row["validation"]["local_sync"] = sync
            ok, msg = remove_detached_worktree_if_clean(self.repo, wt)
            if ok:
                final_cleanup.append(msg)
            else:
                final_cleanup.append(f"WARN {msg}: {wt}")
            if results:
                results[-1].setdefault("cleanup", []).extend(final_cleanup)
            (run_dir / "batch-result.json").write_text(
                json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return results
        except Exception as e:
            # Do not destroy the worktree on validation failure. It is useful for
            # inspection and contains no remote mutation because push happens last.
            rows = [
                {
                    "status": "staged-not-pushed",
                    "issue": x["issue"].number,
                    "issue_commit": x["evidence"].commit,
                    "integration_commit": x["candidate"],
                    "closed": False,
                    "validation": x["validation"],
                    "worktree": str(wt),
                }
                for x in staged
            ]
            rows.append({
                "status": "blocked",
                "issue": staged[-1]["issue"].number if staged else prepared[0][0].number,
                "closed": False,
                "worktree": str(wt),
                "reason": str(e),
            })
            (run_dir / "batch-result.json").write_text(
                json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return rows

    def _change_count_for_issue(self, issue) -> int:
        evidence = ghx.get_completion_evidence(self.repo_slug, issue.number)
        if not evidence:
            return 10**9
        fetch(str(self.repo))
        if git(self.repo, ["cat-file", "-e", f"{evidence.commit}^{{commit}}"], check=False).returncode != 0:
            return 10**9
        out = git(
            self.repo,
            ["diff-tree", "--no-commit-id", "--name-only", "-r", evidence.commit],
            check=False,
        ).stdout.splitlines()
        return len([x for x in out if x.strip()])

    def integrate_done(self, *, issue_numbers: list[int] | None = None, execute: bool = False, close: bool = True) -> list[dict]:
        open_done = [x for x in ghx.list_done(self.repo_slug) if not x.is_epic]
        by_num = {x.number: x for x in open_done}
        if issue_numbers:
            missing = [n for n in issue_numbers if n not in by_num]
            if missing:
                raise CommandError("No están abiertas con agent:done: " + ", ".join(f"#{x}" for x in missing))
            queue = [by_num[n] for n in issue_numbers]
        else:
            # Small/narrow changes first, broad sweeps last. This reduces the chance
            # that a sweeping CSS/refactor commit creates avoidable conflicts for
            # small feature commits. Explicit --issues always wins over this heuristic.
            risk_rank = {"low": 0, "medium": 1, "high": 2}
            scored = []
            for issue in open_done:
                scored.append((self._change_count_for_issue(issue), risk_rank.get(issue.risk, 1), issue.number, issue))
            queue = [x[-1] for x in sorted(scored)]

        plan = [
            {
                "issue": i.number,
                "risk": i.risk,
                "title": i.title,
                "changed_files": self._change_count_for_issue(i),
            }
            for i in queue
        ]
        if not execute:
            print("Plan de integración (no ejecutado):")
            for row in plan:
                print(f"- #{row['issue']} · risk:{row['risk']} · {row['changed_files']} files · {row['title']}")
            print("Usá --execute para procesar secuencialmente; se detiene ante el primer conflicto/fallo.")
            return plan

        batch_cfg = self.cfg.get("batch_validation", {})
        if batch_cfg.get("enabled", True) and batch_cfg.get("full_suite_once_at_end", True):
            print(
                "INTEGRATE-DONE batch-aware: targeted + checks baratos por issue; "
                "suite completa una sola vez al final; push/cierre sólo después del gate final."
            )
            return self._integrate_batch(queue, close=close)

        # Compatibility fallback: v1.6 behavior, one complete integration per issue.
        results: list[dict] = []
        for issue in queue:
            result = self.integrate_issue(issue.number, close=close)
            row = asdict(result)
            results.append(row)
            if result.status not in ("integrated", "reconciled", "already-closed"):
                print(f"INTEGRATE-DONE detenido en #{issue.number}: {result.status}")
                break
        return results

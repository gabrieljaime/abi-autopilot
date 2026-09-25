# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gabriel Jaime
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from .shell import run_capture, CommandError
from .notice import with_footer

AGENT_STATE_LABELS = [
    "agent:ready","agent:running","agent:review","agent:fix",
    "agent:waiting-quota","agent:blocked","agent:implemented","agent:integrated","agent:done"
]

# Ciclo de vida después del review, una etiqueta por etapa:
#
#   agent:implemented  review + validación PASS, commit publicado en la rama de
#                      la issue; todavía no mergeado en ninguna rama compartida.
#   agent:integrated   mergeado en la rama de integración, no alcanzable desde
#                      la rama de despliegue. Sólo existe si son ramas distintas.
#   agent:done         entregado: alcanzable desde la rama de despliegue. Es la
#                      única etapa que cierra la issue.
#
# Antes `agent:done` también marcaba "implementado, esperando integración".
# Por compatibilidad, una issue ABIERTA con `agent:done` se sigue tratando como
# candidata a integrar (ver `list_integration_candidates`).
#
# `agent:integrated` = implementado y mergeado en la rama de integración, pero
# todavía no alcanzable desde la rama de despliegue. `agent:done` conserva su
# nombre pero cambia de significado: ahora es *entregado*, y sólo se aplica
# cuando el trabajo está en la rama canónica.
INTEGRATED_LABEL = "agent:integrated"
IMPLEMENTED_LABEL = "agent:implemented"
DONE_LABEL = "agent:done"
RISK_LABELS = ["risk:low","risk:medium","risk:high"]
SPECIAL_LABELS = ["needs:human","needs:product"]

@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str
    url: str
    labels: list[str]

    @property
    def risk(self) -> str:
        for r in ("low","medium","high"):
            if f"risk:{r}" in self.labels:
                return r
        return "medium"

    @property
    def is_epic(self) -> bool:
        return "[EPIC]" in self.title.upper()


@dataclass(frozen=True)
class CompletionEvidence:
    branch: str
    commit: str
    base: str
    reviewer_pass: bool
    targeted_fast_pass: bool
    full_pass: bool
    body: str


def _gh(repo: str, args: list[str], check: bool=True):
    return run_capture(["gh", *args, "-R", repo], check=check)


def get_issue(repo: str, number: int) -> Issue:
    p = _gh(repo, ["issue","view",str(number),"--json","number,title,body,state,url,labels"])
    j = json.loads(p.stdout)
    return Issue(j["number"], j["title"], j.get("body") or "", j["state"], j["url"], [x["name"] for x in j.get("labels",[])])


def _list_with_label(repo: str, label: str, limit: int=100) -> list[Issue]:
    p = _gh(repo, ["issue","list","--state","open","--label",label,"--limit",str(limit),"--json","number,title,body,state,url,labels"])
    arr = json.loads(p.stdout or "[]")
    return [Issue(x["number"],x["title"],x.get("body") or "",x["state"],x["url"],[l["name"] for l in x.get("labels",[])]) for x in arr]


def list_ready(repo: str, limit: int=100) -> list[Issue]:
    return _list_with_label(repo, "agent:ready", limit)


def list_waiting_quota(repo: str, limit: int=100) -> list[Issue]:
    return _list_with_label(repo, "agent:waiting-quota", limit)


def list_done(repo: str, limit: int=100) -> list[Issue]:
    return _list_with_label(repo, "agent:done", limit)


def list_integration_candidates(repo: str, limit: int=100) -> list[Issue]:
    """Issues abiertas listas para `integrate`: `agent:implemented` más las
    `agent:done` abiertas heredadas del ciclo anterior."""
    seen: dict[int, Issue] = {}
    for issue in _list_with_label(repo, IMPLEMENTED_LABEL, limit) + list_done(repo, limit):
        seen.setdefault(issue.number, issue)
    return [seen[n] for n in sorted(seen)]


def count_closed_with_label(repo: str, label: str, limit: int=1000) -> int:
    p = _gh(repo, ["issue","list","--state","closed","--label",label,"--limit",str(limit),"--json","number"])
    return len(json.loads(p.stdout or "[]"))


def list_actionable(repo: str, limit: int=100) -> list[Issue]:
    by_number = {i.number: i for i in list_ready(repo, limit)}
    for i in list_waiting_quota(repo, limit):
        by_number[i.number] = i
    return list(by_number.values())


def list_untriaged(repo: str, limit: int=100) -> list[Issue]:
    """Open issues that have no agent:* state label yet.

    These are candidates for auto-triage: fresh issues/epics created directly
    in GitHub that the operator has not run `mark-ready` on.
    """
    p = _gh(repo, ["issue", "list", "--state", "open", "--limit", str(limit), "--json", "number,title,body,state,url,labels"])
    arr = json.loads(p.stdout or "[]")
    issues = [Issue(x["number"], x["title"], x.get("body") or "", x["state"], x["url"], [l["name"] for l in x.get("labels", [])]) for x in arr]
    return [i for i in issues if not any(label in AGENT_STATE_LABELS for label in i.labels)]


SOFT_DEPENDENCY_MARKERS = re.compile(r"\b(recommended|optional|recomendad[oa]s?|opcional(?:es)?|nice-to-have|soft)\b", re.I)


def _named_ref_pattern(named_prefixes) -> re.Pattern | None:
    prefixes = [re.escape(str(p).strip()) for p in named_prefixes or () if str(p).strip()]
    if not prefixes:
        return None
    return re.compile(r"\b(?:" + "|".join(prefixes) + r")-\d+\b", re.I)


def dependency_refs_from_body(body: str, named_prefixes=()) -> list[str]:
    """Dependency references: `#N` always; `PREFIX-N` only for prefixes listed
    in `dependency_ref_prefixes` (e.g. `["UX", "API"]`), resolved by finding
    `[PREFIX-N]` in an issue title."""
    named_rx = _named_ref_pattern(named_prefixes)
    refs: set[str] = set()
    lines = body.splitlines()
    in_section = False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s*(dependencias|dependencies)\b", stripped, re.I):
            in_section = True
            continue
        if in_section and re.match(r"^#{1,6}\s+", stripped):
            in_section = False
        relevant = in_section or bool(re.search(r"\bDepends-On\s*:", line, re.I))
        if not relevant:
            continue
        # A reference is only a hard blocker if its own comma/period-separated
        # segment doesn't carry a soft-dependency qualifier (e.g. "#45
        # recomendada"). Segmenting keeps that qualifier from being misread as
        # applying to every reference on the line.
        for segment in re.split(r"[,;.]", line):
            if SOFT_DEPENDENCY_MARKERS.search(segment):
                continue
            refs.update(f"#{x}" for x in re.findall(r"#(\d+)", segment))
            if named_rx:
                refs.update(x.upper() for x in named_rx.findall(segment))
    return sorted(refs)


def dependencies_from_body(body: str) -> list[int]:
    """Backward-compatible numeric dependency parser used by existing tests."""
    return sorted(int(x[1:]) for x in dependency_refs_from_body(body) if x.startswith("#"))


def _all_issue_titles(repo: str, limit: int=500) -> list[dict]:
    p = _gh(repo, ["issue","list","--state","all","--limit",str(limit),"--json","number,title,state"])
    return json.loads(p.stdout or "[]")


def _resolve_named_refs(repo: str, refs: list[str]) -> dict[str, tuple[int,str]]:
    named = [r for r in refs if not r.startswith("#")]
    if not named:
        return {}
    issues = _all_issue_titles(repo)
    resolved: dict[str, tuple[int,str]] = {}
    for token in named:
        rx = re.compile(rf"\[{re.escape(token)}\]", re.I)
        matches = [x for x in issues if rx.search(x.get("title", ""))]
        if len(matches) == 1:
            resolved[token] = (int(matches[0]["number"]), str(matches[0]["state"]))
        elif len(matches) > 1:
            raise CommandError(f"Dependency {token} is ambiguous: " + ", ".join(f"#{x['number']} {x['title']}" for x in matches[:10]))
        else:
            raise CommandError(f"Could not resolve named dependency {token} to a GitHub issue")
    return resolved


def deps_status(repo: str, issue: Issue, named_prefixes=()) -> tuple[bool, list[tuple[str,str]]]:
    refs = dependency_refs_from_body(issue.body, named_prefixes)
    named = _resolve_named_refs(repo, refs)
    result: list[tuple[str,str]] = []
    ok = True
    for ref in refs:
        if ref.startswith("#"):
            dep = get_issue(repo, int(ref[1:]))
            state = dep.state
        else:
            _num, state = named[ref]
        result.append((ref, state))
        if state.upper() != "CLOSED":
            ok = False
    return ok, result


def get_completion_evidence(repo: str, number: int) -> CompletionEvidence | None:
    """Return the latest structured completion comment written by Autopilot.

    Manual integration is allowed only when the implementation phase already
    recorded reviewer + targeted/fast + full PASS. This makes the integration
    command a second safety gate rather than a shortcut around implementation.
    """
    p = _gh(repo, ["issue", "view", str(number), "--json", "comments"])
    data = json.loads(p.stdout or "{}")
    comments = data.get("comments", []) or []
    for item in reversed(comments):
        body = item.get("body") or ""
        # Comments written before the English translation say "completó" and
        # "Base inicial"; both formats stay valid so older issues can integrate.
        if not any(f"Autopilot {verb} #{number}." in body for verb in ("completed", "completó")):
            continue
        branch_m = re.search(r"- Branch:\s*`([^`]+)`", body)
        commit_m = re.search(r"- Commit:\s*`([0-9a-fA-F]{7,40})`", body)
        base_m = re.search(r"- Base(?: inicial)?:\s*`([0-9a-fA-F]{7,40})`", body)
        if not (branch_m and commit_m and base_m):
            continue
        return CompletionEvidence(
            branch=branch_m.group(1),
            commit=commit_m.group(1).lower(),
            base=base_m.group(1).lower(),
            reviewer_pass=bool(re.search(r"Reviewer:\s*PASS", body, re.I)),
            targeted_fast_pass=bool(re.search(r"Targeted/Fast validation:\s*PASS", body, re.I)),
            full_pass=bool(re.search(r"Full validation:\s*PASS", body, re.I)),
            body=body,
        )
    return None


def ensure_labels(repo: str):
    labels = {
        "agent:ready":("1f883d","Ready for Autopilot"),
        "agent:running":("0e8a16","Autopilot is implementing"),
        "agent:review":("5319e7","Automated review"),
        "agent:fix":("fbca04","Automated fix"),
        "agent:waiting-quota":("bf8700","Waiting for the provider quota to reset"),
        "agent:blocked":("b60205","Blocked by Autopilot"),
        "agent:implemented":("bfd4f2","Implemented and validated; waiting for integration"),
        "agent:integrated":("c5def5","In the integration branch; waiting for promotion"),
        "agent:done":("0e8a16","Delivered in the deployment branch"),
        "risk:low":("2da44e","Low risk"),
        "risk:medium":("d4c5f9","Medium risk"),
        "risk:high":("b60205","High risk"),
        "needs:human":("b60205","Needs human intervention"),
        "needs:product":("5319e7","Needs a product decision"),
    }
    for name,(color,desc) in labels.items():
        run_capture(["gh","label","create",name,"--repo",repo,"--color",color,"--description",desc,"--force"], check=True)


def set_state(repo: str, issue: Issue, state_label: str, add: list[str] | None=None, remove_special: bool=False):
    current = set(issue.labels)
    for label in AGENT_STATE_LABELS:
        if label in current and label != state_label:
            _gh(repo,["issue","edit",str(issue.number),"--remove-label",label],check=False)
    if state_label not in current:
        _gh(repo,["issue","edit",str(issue.number),"--add-label",state_label])
    if remove_special:
        for label in SPECIAL_LABELS:
            if label in current:
                _gh(repo,["issue","edit",str(issue.number),"--remove-label",label],check=False)
    for label in add or []:
        if label not in current:
            _gh(repo,["issue","edit",str(issue.number),"--add-label",label])


def set_risk(repo: str, issue: Issue, risk: str):
    current=set(issue.labels)
    for label in RISK_LABELS:
        if label in current and label != f"risk:{risk}":
            _gh(repo,["issue","edit",str(issue.number),"--remove-label",label],check=False)
    if f"risk:{risk}" not in current:
        _gh(repo,["issue","edit",str(issue.number),"--add-label",f"risk:{risk}"])


def comment(repo: str, number: int, body: str):
    _gh(repo,["issue","comment",str(number),"--body",with_footer(body)])


def close_issue(repo: str, number: int, comment_text: str | None=None):
    if comment_text:
        comment(repo,number,comment_text)
    _gh(repo,["issue","close",str(number),"--reason","completed"])

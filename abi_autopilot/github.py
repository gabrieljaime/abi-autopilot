from __future__ import annotations
import json
import re
from dataclasses import dataclass
from .shell import run_capture, CommandError

AGENT_STATE_LABELS = [
    "agent:ready","agent:running","agent:review","agent:fix",
    "agent:waiting-quota","agent:blocked","agent:integrated","agent:done"
]

# `agent:integrated` = implementado y mergeado en la rama de integración, pero
# todavía no alcanzable desde la rama de despliegue. `agent:done` conserva su
# nombre pero cambia de significado: ahora es *entregado*, y sólo se aplica
# cuando el trabajo está en la rama canónica.
INTEGRATED_LABEL = "agent:integrated"
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


SOFT_DEPENDENCY_MARKERS = re.compile(r"\b(recomendad[oa]s?|opcional(?:es)?|nice-to-have|soft)\b", re.I)


def dependency_refs_from_body(body: str) -> list[str]:
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
        # segment doesn't carry a soft-dependency qualifier (e.g. "UX-45
        # recomendada"). Segmenting keeps that qualifier from being misread as
        # applying to every reference on the line.
        for segment in re.split(r"[,;.]", line):
            if SOFT_DEPENDENCY_MARKERS.search(segment):
                continue
            refs.update(f"#{x}" for x in re.findall(r"#(\d+)", segment))
            refs.update(x.upper() for x in re.findall(r"\b(?:UX|COACH|MRAG)-\d+\b", segment, re.I))
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
            raise CommandError(f"Dependencia {token} es ambigua: " + ", ".join(f"#{x['number']} {x['title']}" for x in matches[:10]))
        else:
            raise CommandError(f"No pude resolver dependencia nombrada {token} a una issue GitHub")
    return resolved


def deps_status(repo: str, issue: Issue) -> tuple[bool, list[tuple[str,str]]]:
    refs = dependency_refs_from_body(issue.body)
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
        if f"Autopilot completó #{number}." not in body:
            continue
        branch_m = re.search(r"- Branch:\s*`([^`]+)`", body)
        commit_m = re.search(r"- Commit:\s*`([0-9a-fA-F]{7,40})`", body)
        base_m = re.search(r"- Base inicial:\s*`([0-9a-fA-F]{7,40})`", body)
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
        "agent:ready":("1f883d","Lista para Autopilot"),
        "agent:running":("0e8a16","Autopilot implementando"),
        "agent:review":("5319e7","Review automático"),
        "agent:fix":("fbca04","Corrección automática"),
        "agent:waiting-quota":("bf8700","Esperando reset de cuota del proveedor"),
        "agent:blocked":("b60205","Bloqueada por Autopilot"),
        "agent:integrated":("c5def5","Integrado en la rama de integración; pendiente de promoción"),
        "agent:done":("0e8a16","Entregado en la rama de despliegue"),
        "risk:low":("2da44e","Bajo riesgo"),
        "risk:medium":("d4c5f9","Riesgo medio"),
        "risk:high":("b60205","Alto riesgo"),
        "needs:human":("b60205","Requiere intervención humana"),
        "needs:product":("5319e7","Requiere decisión de producto"),
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
    _gh(repo,["issue","comment",str(number),"--body",body])


def close_issue(repo: str, number: int, comment_text: str | None=None):
    if comment_text:
        comment(repo,number,comment_text)
    _gh(repo,["issue","close",str(number),"--reason","completed"])

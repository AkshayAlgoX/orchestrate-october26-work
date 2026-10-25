"""Bounded, evidence-driven investigation controller for one deletion request.

The controller keeps an explicit investigation state and an agenda of typed tool calls.
Each tool has a declared contract (`TOOLS`) that is enforced on every call. What a tool
returns decides what is queued next: a direct record that names another resource queues
a reference check, an accepted catalog claim queues the references of the resource it
adds, an unresolved identity or a non-deletion request ends the investigation. The
deterministic policy kernel stays authoritative: the controller decides *which*
admissible queries to run and *when* to stop, never what a rule means, and it can only
emit steps that the kernel compiled from linked resources and admissible evidence.

Phases (a state machine; a phase starts only when the agenda of the previous one is empty):
  AUTHORIZE   -> classify_intent
  IDENTIFY    -> resolve_identity
  DISCOVER    -> search_direct_evidence, query_catalog (only when a claim names the
                 subject), follow_reference (only for references carried by linked
                 records, followed to closure, never from workflow-inferred resources)
  INVESTIGATE -> query_workflow_context (shared, settled across all requests), then
                 resolve_resource_state for every linked resource
  COMPILE     -> compile_obligations
  VALIDATE    -> validate_contract, check_admissibility

Stop conditions, recorded in the trace: the request does not authorize deletion; the
identity is unresolved; the agenda is empty (with or without unresolved workflow
candidates); the query budget is exhausted (fail closed: INCOMPLETE, no steps); a tool
failed or its contract was violated, or the compiled row failed validation (fall back to
the reference pipeline, re-validated; if that also fails, fail closed). The budget counts
executed queries rather than wall-clock time so the outcome stays deterministic.
Duplicate queries are suppressed and logged. No model or network call is made.

Shared context: competing requests are resolved globally before any single case is
investigated (`Auditor.prepare`). A single case cannot correctly settle globally
competing workflow resources on its own, so `AuditAgent` requires that prepared context.
"""

from __future__ import annotations

import heapq
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .audit import Auditor, CaseResult
from .intent import classify
from .linking import Link, catalog_links
from .loaders import norm_token
from .output import check_row, to_json
from .planner import ResourceState, plan_case

PHASES = ("AUTHORIZE", "IDENTIFY", "DISCOVER", "INVESTIGATE", "COMPILE", "VALIDATE")

STOP_NOT_AUTHORIZED = "request does not authorize deletion"
STOP_IDENTITY = "identity unresolved; no admissible query can continue"
STOP_BUDGET = "query budget exhausted"
STOP_DONE = "obligations compiled; agenda empty"
STOP_DONE_UNRESOLVED = ("obligations compiled; unresolved workflow candidates have no admissible "
                        "distinguishing query")


# -- tool contracts -------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    """Contract of one investigation tool, checked on every call."""
    name: str
    phase: str
    purpose: str
    inputs: Tuple[Tuple[str, str], ...]       # (field, type); types: str, str?, list[str]
    returns: str
    preconditions: str = ""
    precondition_id: Optional[str] = None

    def describe(self) -> Dict[str, Any]:
        d = {"name": self.name, "phase": self.phase, "purpose": self.purpose,
             "inputs": dict(self.inputs), "returns": self.returns, "preconditions": self.preconditions}
        if self.precondition_id is not None:
            d["precondition_id"] = self.precondition_id
        return d


# Typed precondition identifiers
PRECOND_AUTHORIZATION = "AUTHORIZATION_REQUIRED"
PRECOND_SUBJECT_RESOLVED = "SUBJECT_RESOLVED"
PRECOND_CATALOG_CLAIMS = "CATALOG_CLAIMS_NAMING_SUBJECT"
PRECOND_CONTENT_LINKED_SOURCE = "CONTENT_LINKED_SOURCE"
PRECOND_WORKFLOW_CONTEXT = "WORKFLOW_CONTEXT_PREPARED"
PRECOND_LINKED_RESOURCE = "LINKED_RESOURCE_REQUIRED"
PRECOND_STATES_RESOLVED = "ALL_LINKED_STATES_RESOLVED"
PRECOND_COMPILED_OUTPUT = "COMPILED_OUTPUT_REQUIRED"


TOOLS: Dict[str, ToolSpec] = {t.name: t for t in (
    ToolSpec("classify_intent", "AUTHORIZE", "Decide whether request_text authorizes deletion.",
             (("case_id", "str"),), "{authorizes_deletion: bool, reason: str}"),
    ToolSpec("resolve_identity", "IDENTIFY",
             "Map the request email (current or historical) to one subject and all its identifiers.",
             (("email", "str"),), "{subject: user_id|null, identifier_tokens: int}",
             "request authorizes deletion", PRECOND_AUTHORIZATION),
    ToolSpec("search_direct_evidence", "DISCOVER",
             "Records carrying the subject's exact identifiers in files captured inside the window.",
             (("subject", "str"), ("window", "list[str?]")), "{resources: [resource_id]}; cites file_ids",
             "subject resolved", PRECOND_SUBJECT_RESOLVED),
    ToolSpec("query_catalog", "DISCOVER",
             "Catalog claims naming the subject; accepted only if corroborated and not contradicted.",
             (("subject", "str"), ("claims", "list[str]")), "{accepted: [resource_id], rejected: [resource_id]}",
             "at least one catalog claim names one of the subject's identifiers", PRECOND_CATALOG_CLAIMS),
    ToolSpec("follow_reference", "DISCOVER",
             "System of a resource explicitly named by a linked record, unless attributed to another person.",
             (("from", "str"), ("ref", "str"), ("file", "str")), "{system: str|null}; cites the naming file",
             "`from` is content-linked (direct, catalog or reference) and its record names `ref`",
             PRECOND_CONTENT_LINKED_SOURCE),
    ToolSpec("query_workflow_context", "INVESTIGATE",
             "Identifier-less resources the deletion workflow acted on, settled across all open requests.",
             (("case_id", "str"),), "{assigned: [resource_id], candidates: [resource_id]}",
             "discovery finished; shared context prepared for this exact request",
             PRECOND_WORKFLOW_CONTEXT),
    ToolSpec("resolve_resource_state", "INVESTIGATE",
             "State of one linked resource at audit_at from admissible observations.",
             (("resource", "str"), ("how", "str")), "{category, exists, exposed, flow_enabled, restore_allowed, note}",
             "resource is linked to the subject", PRECOND_LINKED_RESOURCE),
    ToolSpec("compile_obligations", "COMPILE", "Apply deletion_rules.csv to the resolved states.",
             (("resources", "list[str]"),), "{status, steps, notes}", "every linked resource has a state",
             PRECOND_STATES_RESOLVED),
    ToolSpec("validate_contract", "VALIDATE", "Output-contract self-check of the compiled row.",
             (("case_id", "str"),), "{errors: [str]}", "row must be compiled", PRECOND_COMPILED_OUTPUT),
    ToolSpec("check_admissibility", "VALIDATE",
             "Every step names a linked resource; every citation is a known id available at audit_at.",
             (("case_id", "str"),), "{violations: [str]}", "row must be compiled", PRECOND_COMPILED_OUTPUT),
)}


class ToolContractError(ValueError):
    pass


class BudgetExhausted(Exception):
    pass


class ToolFailure(Exception):
    """A tool call that could not complete. kind "contract": the call broke its declared contract or a
    precondition (a controller defect); kind "runtime": the tool itself raised while evaluating evidence."""

    def __init__(self, tool: str, error: str, kind: str = "runtime"):
        super().__init__(f"{tool}: {error}")
        self.tool, self.error, self.kind = tool, error, kind


def check_inputs(spec: ToolSpec, inputs: Dict[str, Any]) -> None:
    """Raise ToolContractError unless `inputs` match the declared contract exactly."""
    declared = dict(spec.inputs)
    if set(inputs) != set(declared):
        raise ToolContractError(f"{spec.name}: inputs {sorted(inputs)} != contract {sorted(declared)}")
    for name, typ in declared.items():
        v = inputs[name]
        if typ == "str":
            ok = isinstance(v, str) and v != ""
        elif typ == "str?":
            ok = v is None or isinstance(v, str)
        elif typ.startswith("list["):
            inner = typ[5:-1]
            ok = isinstance(v, list) and all((x is None and inner == "str?") or isinstance(x, str) for x in v)
        else:
            ok = False
        if not ok:
            raise ToolContractError(f"{spec.name}: input {name!r}={v!r} is not {typ}")


def describe_tools() -> List[Dict[str, Any]]:
    return [TOOLS[n].describe() for n in TOOLS]


# -- investigation state ----------------------------------------------------------

@dataclass
class Query:
    n: int
    stage: str
    tool: str
    reason: str
    inputs: Dict[str, Any]
    evidence_refs: List[str] = field(default_factory=list)
    result: Any = None

    def as_dict(self) -> Dict[str, Any]:
        return {"n": self.n, "stage": self.stage, "tool": self.tool, "reason": self.reason,
                "inputs": self.inputs, "evidence_refs": self.evidence_refs, "result": self.result}


@dataclass(order=True)
class Task:
    phase_rank: int
    wave: int                 # reference-closure depth inside DISCOVER (0 for the first queries)
    key: str                  # ordering key inside a wave (source resource)
    seq: int
    tool: str = field(compare=False)
    reason: str = field(compare=False)
    inputs: Dict[str, Any] = field(compare=False)


@dataclass
class Investigation:
    req: Dict[str, str]
    phase: str = "AUTHORIZE"
    intent_reason: Optional[str] = None
    authorized: Optional[bool] = None
    subject: Optional[str] = None
    ident: Any = None
    links: Dict[str, Link] = field(default_factory=dict)
    named_references: Dict[str, Set[Tuple[str, str]]] = field(default_factory=lambda: defaultdict(set))
    extra_obs: Dict[str, list] = field(default_factory=lambda: defaultdict(list))
    cand_map: Dict[str, dict] = field(default_factory=dict)
    states: Dict[str, ResourceState] = field(default_factory=dict)
    unresolved_states: Dict[str, str] = field(default_factory=dict)   # resource -> runtime error
    unresolved: List[str] = field(default_factory=list)
    contradictions: List[Dict[str, str]] = field(default_factory=list)
    queries: List[Query] = field(default_factory=list)
    seen: set = field(default_factory=set)
    agenda: List[Task] = field(default_factory=list)
    compiled: Optional[Tuple[str, list, list]] = None
    errors: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)
    stop: Optional[Tuple[str, str]] = None     # (stop reason, status) once decided

    @property
    def executed(self) -> int:
        return sum(1 for q in self.queries if q.result != "skipped")


# -- semantic precondition guards -------------------------------------------------

def guard_authorize_before_identify(inv: Investigation, task: Task, auditor: Optional[Auditor] = None,
                                    tools: Optional[AuditTools] = None) -> None:
    if inv.intent_reason is None:
        raise ToolContractError("resolve_identity: request authorization must precede identity resolution")
    if inv.authorized is not True:
        raise ToolContractError("resolve_identity: request does not authorize deletion")


def guard_subject_resolved(inv: Investigation, task: Task, auditor: Optional[Auditor] = None,
                           tools: Optional[AuditTools] = None) -> None:
    if inv.subject is None or inv.ident is None:
        raise ToolContractError(f"{task.tool}: subject identity must be resolved first")
    task_subject = task.inputs.get("subject")
    if task_subject is not None and task_subject != inv.subject:
        raise ToolContractError(f"{task.tool}: task subject {task_subject!r} does not match investigation subject {inv.subject!r}")


def guard_catalog_claims(inv: Investigation, task: Task, auditor: Optional[Auditor] = None,
                         tools: Optional[AuditTools] = None) -> None:
    if inv.subject is None or inv.ident is None:
        raise ToolContractError(f"{task.tool}: subject identity must be resolved first")
    task_subject = task.inputs.get("subject")
    if task_subject is not None and task_subject != inv.subject:
        raise ToolContractError(f"{task.tool}: task subject {task_subject!r} does not match investigation subject {inv.subject!r}")
    claims = task.inputs.get("claims", [])
    if not isinstance(claims, list) or len(claims) == 0:
        raise ToolContractError("query_catalog: at least one catalog claim must be provided")
    if auditor is None or not hasattr(auditor, "claims"):
        raise ToolContractError("query_catalog: auditor catalog context required")
    # Enforce catalog policy: at least one claim must match the resolved subject's tokens
    subject_tokens = getattr(inv.ident, "link_tokens", set())
    matching_activity_ids = {
        c.activity_id for c in auditor.claims
        if norm_token(c.email) in subject_tokens or c.user.lower() in subject_tokens
    }
    if not any(cid in matching_activity_ids for cid in claims):
        raise ToolContractError("query_catalog: no claim in task matches the resolved subject's identifiers")


def guard_content_linked_source(inv: Investigation, task: Task, auditor: Optional[Auditor] = None,
                                tools: Optional[AuditTools] = None) -> None:
    src = task.inputs.get("from")
    ref = task.inputs.get("ref")
    fid = task.inputs.get("file")
    if src not in inv.links:
        raise ToolContractError(f"follow_reference: source resource {src!r} is not in linked resources")

    # Check if this reference was discovered and recorded from the source resource
    if (ref, fid) in inv.named_references.get(src, set()):
        return

    refs_fn = getattr(tools, "references_from", None) or getattr(auditor, "references_from", None)
    if refs_fn is None:
        raise ToolContractError("follow_reference: auditor context required for reference verification")
    lower = auditor._lower(inv.req) if auditor is not None and hasattr(auditor, "_lower") else None
    audit_at = inv.req["audit_at"]
    valid_refs = set(refs_fn(src, lower, audit_at))
    if (ref, fid) not in valid_refs:
        raise ToolContractError(f"follow_reference: record {src!r} does not name reference {ref!r} in {fid!r}")


def guard_workflow_context_prepared(inv: Investigation, task: Task, auditor: Optional[Auditor] = None,
                                    tools: Optional[AuditTools] = None) -> None:
    if inv.subject is None or inv.ident is None:
        raise ToolContractError("query_workflow_context: subject identity must be resolved first")
    cid = task.inputs.get("case_id")
    if cid != inv.req.get("case_id"):
        raise ToolContractError(f"query_workflow_context: case_id {cid!r} does not match investigation {inv.req.get('case_id')!r}")
    if auditor is None or not hasattr(auditor, "prepared_key") or not hasattr(auditor, "workflow"):
        raise ToolContractError("query_workflow_context: auditor preparation context is required")
    expected_key = auditor._key(inv.req)
    actual_key = auditor.prepared_key.get(cid)
    if actual_key is None:
        raise ToolContractError(f"query_workflow_context: shared context not prepared for case {cid!r}")
    if actual_key != expected_key:
        raise ToolContractError(f"query_workflow_context: shared context for case {cid!r} is stale or mismatched")


def guard_linked_resource(inv: Investigation, task: Task, auditor: Optional[Auditor] = None,
                          tools: Optional[AuditTools] = None) -> None:
    if inv.subject is None or inv.ident is None:
        raise ToolContractError("resolve_resource_state: subject identity must be resolved first")
    rid = task.inputs.get("resource")
    if not rid or rid not in inv.links:
        raise ToolContractError(f"resolve_resource_state: resource {rid!r} is not linked to the subject")


def guard_states_resolved(inv: Investigation, task: Task, auditor: Optional[Auditor] = None,
                          tools: Optional[AuditTools] = None) -> None:
    if inv.subject is None or inv.ident is None:
        raise ToolContractError("compile_obligations: subject identity must be resolved first")
    resources = task.inputs.get("resources", [])
    if not isinstance(resources, list):
        raise ToolContractError("compile_obligations: resources must be a list")
    missing_task = [r for r in resources if r not in inv.states]
    missing_linked = [r for r in inv.links if r not in inv.states and r not in inv.unresolved_states]
    missing = sorted(set(missing_task + missing_linked))
    if missing:
        raise ToolContractError(f"compile_obligations: states missing for linked resources: {missing[:3]}")


def guard_compiled_output(inv: Investigation, task: Task, auditor: Optional[Auditor] = None,
                          tools: Optional[AuditTools] = None) -> None:
    if inv.compiled is None:
        raise ToolContractError(f"{task.tool}: row must be compiled before {task.tool}")
    cid = task.inputs.get("case_id")
    if cid != inv.req.get("case_id"):
        raise ToolContractError(f"{task.tool}: case_id {cid!r} does not match investigation {inv.req.get('case_id')!r}")


PRECONDITION_GUARDS: Dict[str, Callable[..., None]] = {
    PRECOND_AUTHORIZATION: guard_authorize_before_identify,
    PRECOND_SUBJECT_RESOLVED: guard_subject_resolved,
    PRECOND_CATALOG_CLAIMS: guard_catalog_claims,
    PRECOND_CONTENT_LINKED_SOURCE: guard_content_linked_source,
    PRECOND_WORKFLOW_CONTEXT: guard_workflow_context_prepared,
    PRECOND_LINKED_RESOURCE: guard_linked_resource,
    PRECOND_STATES_RESOLVED: guard_states_resolved,
    PRECOND_COMPILED_OUTPUT: guard_compiled_output,
}


def check_preconditions(spec: ToolSpec, inv: Investigation, task: Task,
                        auditor: Optional[Auditor] = None,
                        tools: Optional[AuditTools] = None) -> None:
    """Enforce explicit semantic preconditions tied to ToolSpec before execution."""
    pid = spec.precondition_id
    if pid is None:
        return
    guard = PRECONDITION_GUARDS.get(pid)
    if guard is None:
        raise ToolContractError(f"{spec.name}: unknown precondition guard {pid!r}")
    guard(inv, task, auditor, tools)


# -- tools ------------------------------------------------------------------------

class AuditTools:
    """Typed tool interface over the shared request context (one per run)."""

    def __init__(self, auditor: Auditor):
        if not hasattr(auditor, "workflow"):
            raise ValueError("Auditor.prepare(universe) must run before investigating cases "
                             "(globally competing workflow resources need the shared context)")
        self.a = auditor
        # Token -> observation indices, preserving corpus order (search index).
        self.obs_by_token: Dict[str, List[int]] = defaultdict(list)
        for i, ob in enumerate(auditor.ev.observations):
            for tok in ob.link_values:
                self.obs_by_token[tok].append(i)
        # Catalog claims indexed by the identifiers they name.
        self.claims_by_token: Dict[str, List[int]] = defaultdict(list)
        for i, c in enumerate(auditor.claims):
            self.claims_by_token[norm_token(c.email)].append(i)
            self.claims_by_token[c.user.lower()].append(i)
        self.activity_time = {a["activity_id"]: a["event_time"] for a in auditor.ds.activity}

    def classify_intent(self, req):
        return classify(req["request_text"])

    def resolve_identity(self, email: str):
        uid = self.a.ds.identities.resolve_email(email)
        return (uid, self.a.ds.identities.by_user[uid]) if uid else (None, None)

    def search_direct_evidence(self, ident, lower: Optional[str], audit_at: str) -> Dict[str, Link]:
        """Observations carrying one of the subject's identifier tokens (exact match)."""
        from .linking import eligible
        idx = sorted({i for tok in ident.link_tokens for i in self.obs_by_token.get(tok, ())})
        links: Dict[str, Link] = {}
        for i in idx:
            ob = self.a.ev.observations[i]
            if not eligible(ob, lower, audit_at):
                continue
            link = links.setdefault(ob.resource_id, Link(ob.resource_id, ob.system, "direct"))
            if ob.file_id not in link.basis:
                link.basis.append(ob.file_id)
        return links

    def catalog_claims_naming(self, ident) -> List[int]:
        return sorted({i for tok in ident.link_tokens for i in self.claims_by_token.get(tok, ())})

    def query_catalog(self, ident, lower, audit_at):
        return catalog_links(ident, self.a.claims, self.a.by_resource, self.a.ev.documents, lower, audit_at,
                             self.a.ds.identities)

    def references_from(self, rid: str, lower, audit_at) -> List[Tuple[str, str]]:
        return self.a.references_from(rid, lower, audit_at)

    def follow_reference(self, ident, ref: str, audit_at: str) -> Optional[str]:
        return self.a.reference_system(ident.user_id, ref, audit_at)

    def workflow_context(self, req) -> Tuple[Dict[str, Link], Dict[str, dict]]:
        cid = req["case_id"]
        if self.a.prepared_key.get(cid) != self.a._key(req):
            return {}, {}
        return dict(self.a.workflow.get(cid, {})), dict(self.a.candidates.get(cid, {}))

    def resolve_resource_state(self, req, rid: str, link: Link, extra_obs, winfo: dict) -> ResourceState:
        return self.a.resource_state(req, rid, link, extra_obs, winfo)

    def compile_obligations(self, states, audit_at):
        return plan_case(states, self.a.policy, audit_at)

    def validate(self, case_id, status, steps_json):
        return check_row(case_id, status, steps_json)

    def _substantiates(self, req: Dict[str, Any], step: Dict[str, Any], linked_map: Any) -> bool:
        """Verify that the cited evidence_id actually substantiates the asserted step."""
        rid = step["resource_id"]
        ev = step.get("evidence_id")
        if not ev:
            return True
        op, state = step["operation"], step["state"]
        audit_at = req["audit_at"]

        # 1. Direct observations of the resource in the cited file
        direct_obs = [o for o in self.a.by_resource.get(rid, [])
                      if o.file_id == ev and o.file_captured_at <= audit_at and o.sort_time <= audit_at]
        for o in direct_obs:
            act = self.a.policy.action(o.category) if o.category else None
            if op == "REPORT_REMAINING_RECORD" and o.exists is True and act in ("DELETE", None):
                return True
            if op == "REPORT_EXPOSED_PII":
                if act == "REDACT" and o.exists is True and bool(o.pii):
                    return True
                if act == "ANONYMIZE" and o.exists is True and bool(o.pii or o.refs):
                    return True
            if op == "REPORT_ACTIVE_DATA_FLOW" and o.flow_enabled is True:
                return True
            if op == "REPORT_RESTORE_RISK" and (o.restore_allowed is True or (
                    o.category == "BACKUP_ARCHIVE" and o.exists is True and o.restore_allowed is not False)):
                return True
            if op == "VERIFY_RECORD_ABSENT" and o.exists is False:
                return True
            if op == "VERIFY_RECORD_PRESENT" and o.exists is True and act == "RETAIN":
                return True
            if op == "VERIFY_RECORD_REDACTED" and o.exists is True and act == "REDACT" and not o.pii:
                return True
            if op == "VERIFY_RECORD_ANONYMIZED" and o.exists is True and act == "ANONYMIZE" and not (o.pii or o.refs):
                return True
            if op == "VERIFY_DATA_FLOW_DISABLED" and o.flow_enabled is False:
                return True
            if op == "VERIFY_RESTORE_BLOCKED" and o.restore_allowed is False:
                return True

        # 2. Scoped snapshot absence
        if op == "VERIFY_RECORD_ABSENT":
            scopes = [sc for sc in self.a.ev.scopes
                      if sc.file_id == ev and sc.system == step["system_id"]
                      and sc.file_captured_at <= audit_at and sc.observed_at <= audit_at
                      and rid not in sc.resource_ids]
            if scopes:
                # Case A: Prior presence observations (replica drift deletion)
                pres_obs = [o for o in self.a.by_resource.get(rid, [])
                            if o.exists is True and o.file_captured_at <= audit_at]
                if pres_obs:
                    latest_t = max(o.sort_time for o in pres_obs)
                    if any(sc.observed_at > latest_t and any(o.store == sc.store for o in pres_obs)
                           for sc in scopes):
                        return True
                # Case B: Workflow resource action
                acts = [a for a in self.a.activity_by_resource.get(rid, [])
                        if a.get("source_system") == "deletion_worker"
                        and (a.get("target_system") == step["system_id"] or not a.get("target_system"))
                        and a.get("event_time") <= audit_at]
                if acts:
                    acted_at = min(a["event_time"] for a in acts)
                    if any(sc.observed_at >= acted_at for sc in scopes):
                        return True

        # 3. Catalog-backed proof (document behind accepted catalog claim)
        uid = self.a.ds.identities.resolve_email(req["email"])
        if uid:
            for claim in self.a.claims:
                if claim.resource_id == rid and (claim.user == uid or claim.email == req["email"]):
                    docs = self.a.ev.documents.get(claim.ref, [])
                    for d in docs:
                        if d.get("file_id") == ev:
                            obs_t = d.get("observed_at") or self.a.ev.files[ev].captured_at
                            if obs_t <= audit_at:
                                if op == "REPORT_REMAINING_RECORD":
                                    return True
                                if op == "VERIFY_RECORD_REDACTED" and not d.get("pii"):
                                    return True

        return False

    def check_admissibility(self, req, steps_json: str, linked) -> List[str]:
        """Steps may only name linked resources, cite valid pre-cutoff evidence, and substantiate the step."""
        audit_at, out = req["audit_at"], []
        linked_set = set(linked.keys()) if isinstance(linked, dict) else set(linked)
        for s in json.loads(steps_json):
            if s["resource_id"] not in linked_set:
                out.append(f"step {s['step_no']}: {s['resource_id']} is not linked to the subject")
            ev = s["evidence_id"]
            if not ev:
                continue
            when = (self.a.ds.manifest.get(ev, {}).get("captured_at") or self.activity_time.get(ev)
                    or self.a.ds.audit_events.get(ev))
            if when is None:
                out.append(f"step {s['step_no']}: unknown evidence id {ev}")
            elif when > audit_at:
                out.append(f"step {s['step_no']}: {ev} is dated {when}, after audit_at {audit_at}")
            elif not self._substantiates(req, s, linked):
                out.append(f"step {s['step_no']}: evidence {ev} does not substantiate {s['operation']} ({s['state']}) for {s['resource_id']}")
        return out


# -- controller -------------------------------------------------------------------

class AuditAgent:
    """Bounded, evidence-driven investigation of one request over the shared context."""

    def __init__(self, auditor: Auditor, max_queries: int = 256):
        self.tools = AuditTools(auditor)
        self.a = auditor
        self.max_queries = max_queries
        self._seq = 0
        # Each tool's result is routed by its handler, which may queue further tool calls.
        self.handlers: Dict[str, Callable[[Investigation, Any, Task], None]] = {
            "classify_intent": self._on_intent, "resolve_identity": self._on_identity,
            "search_direct_evidence": self._on_direct, "query_catalog": self._on_catalog,
            "follow_reference": self._on_reference, "query_workflow_context": self._on_workflow,
            "resolve_resource_state": self._on_state, "compile_obligations": self._on_compiled,
            "validate_contract": self._on_validated, "check_admissibility": self._on_admissibility,
        }

    # -- agenda ---------------------------------------------------------------------
    def _push(self, inv: Investigation, tool: str, reason: str, inputs: Dict[str, Any],
              wave: int = 0, key: str = "") -> None:
        spec = TOOLS[tool]
        check_inputs(spec, inputs)
        self._seq += 1
        heapq.heappush(inv.agenda, Task(PHASES.index(spec.phase), wave, key, self._seq, tool, reason, inputs))

    def _execute(self, inv: Investigation, task: Task):
        """Run one queued tool call: contract check, precondition check, de-duplication, budget, failure capture."""
        spec = TOOLS[task.tool]
        try:
            check_inputs(spec, task.inputs)
            check_preconditions(spec, inv, task, self.a, self.tools)
        except ToolContractError as exc:
            raise ToolFailure(task.tool, str(exc), kind="contract")
        key = (task.tool, json.dumps(task.inputs, sort_keys=True))
        if key in inv.seen:
            inv.queries.append(Query(len(inv.queries) + 1, spec.phase, task.tool, "duplicate query suppressed",
                                     task.inputs, [], "skipped"))
            return None, False
        if inv.executed >= self.max_queries:
            raise BudgetExhausted()
        inv.seen.add(key)
        runner, summarize, refs = self._runner(inv, task)
        try:
            result = runner()
        except Exception as exc:  # a failed tool must never leak a partial plan
            inv.queries.append(Query(len(inv.queries) + 1, spec.phase, task.tool, task.reason, task.inputs, [],
                                     {"error": f"{type(exc).__name__}: {exc}"}))
            raise ToolFailure(task.tool, f"{type(exc).__name__}: {exc}")
        inv.queries.append(Query(len(inv.queries) + 1, spec.phase, task.tool, task.reason, task.inputs,
                                 refs(result), summarize(result)))
        return result, True

    def _runner(self, inv: Investigation, task: Task):
        """(call, summarize, evidence refs) for a task; the only place tools are invoked."""
        t, req, x = self.tools, inv.req, task.inputs
        audit_at, lower = req["audit_at"], self.a._lower(req)
        link_lower = None if self.a.cfg.identity_lookback else lower
        none = lambda r: []  # noqa: E731
        if task.tool == "classify_intent":
            return (lambda: t.classify_intent(req),
                    lambda r: {"authorizes_deletion": r.authorizes_deletion, "reason": r.reason}, none)
        if task.tool == "resolve_identity":
            return (lambda: t.resolve_identity(req["email"]),
                    lambda r: {"subject": r[0], "identifier_tokens": len(r[1].link_tokens) if r[1] else 0}, none)
        if task.tool == "search_direct_evidence":
            return (lambda: t.search_direct_evidence(inv.ident, link_lower, audit_at),
                    lambda r: {"resources": sorted(r)}, lambda r: sorted({f for l in r.values() for f in l.basis}))
        if task.tool == "query_catalog":
            named = {self.a.claims[i].activity_id: self.a.claims[i].resource_id
                     for i in t.catalog_claims_naming(inv.ident)}
            return (lambda: t.query_catalog(inv.ident, lower, audit_at),
                    lambda r: {"accepted": sorted(r), "rejected": sorted(set(named.values()) - set(r))},
                    lambda r: sorted({b for v in r.values() for b in v["link"].basis}))
        if task.tool == "follow_reference":
            return (lambda: t.follow_reference(inv.ident, x["ref"], audit_at), lambda r: {"system": r},
                    lambda r: [x["file"]])
        if task.tool == "query_workflow_context":
            return (lambda: t.workflow_context(req), lambda r: {"assigned": sorted(r[0]), "candidates": sorted(r[1])},
                    lambda r: sorted({b for l in r[0].values() for b in l.basis}))
        if task.tool == "resolve_resource_state":
            rid = x["resource"]
            return (lambda: t.resolve_resource_state(req, rid, inv.links[rid], inv.extra_obs,
                                                     inv.cand_map.get(rid, {})),
                    lambda s: {"category": s.category, "exists": s.exists, "exposed": s.exposed,
                               "flow_enabled": s.flow_enabled, "restore_allowed": s.restore_allowed, "note": s.note},
                    lambda s: [s.evidence_id] if s.evidence_id else [])
        if task.tool == "compile_obligations":
            states = [inv.states[r] for r in x["resources"]]
            return (lambda: t.compile_obligations(states, audit_at),
                    lambda r: {"status": r[0], "steps": len(r[1]), "notes": r[2]}, none)
        if task.tool == "validate_contract":
            status, steps_json = self._row(inv)
            return (lambda: t.validate(req["case_id"], status, steps_json), lambda r: {"errors": r}, none)
        if task.tool == "check_admissibility":
            _, steps_json = self._row(inv)
            return (lambda: t.check_admissibility(req, steps_json, inv.links), lambda r: {"violations": r}, none)
        raise ToolContractError(f"no runner for tool {task.tool!r}")

    # -- routing: what each result makes necessary next -----------------------------
    def _on_intent(self, inv, r, task):
        inv.intent_reason = r.reason
        inv.authorized = r.authorizes_deletion
        if not r.authorizes_deletion:
            inv.stop = (STOP_NOT_AUTHORIZED, "NOT_APPLICABLE")
            return
        self._push(inv, "resolve_identity",
                   "map the request email to one subject and all current/historical identifiers",
                   {"email": inv.req["email"].strip().lower()})

    def _on_identity(self, inv, r, task):
        inv.subject, inv.ident = r
        if inv.subject is None:
            inv.stop = (STOP_IDENTITY, "INCOMPLETE")
            return
        lower = self.a._lower(inv.req)
        link_lower = None if self.a.cfg.identity_lookback else lower
        self._push(inv, "search_direct_evidence",
                   "records carrying the subject's exact identifiers inside the evidence window",
                   {"subject": inv.subject, "window": [link_lower, inv.req["audit_at"]]})
        naming = self.tools.catalog_claims_naming(inv.ident)
        if naming:
            self._push(inv, "query_catalog", f"{len(naming)} catalog claim(s) name the subject; "
                       "accept only if corroborated and not contradicted by displayed identifiers",
                       {"subject": inv.subject, "claims": [self.a.claims[i].activity_id for i in naming]})

    def _link(self, inv, link: Link, wave: int):
        """Record a content link and queue the references its records carry."""
        inv.links[link.resource_id] = link
        if not self.a.cfg.reference_links:
            return
        lower = self.a._lower(inv.req)
        for ref, fid in self.tools.references_from(link.resource_id, lower, inv.req["audit_at"]):
            inv.named_references[link.resource_id].add((ref, fid))
            if ref not in inv.links:
                self._push(inv, "follow_reference", f"{link.resource_id} explicitly names {ref} in {fid}",
                           {"from": link.resource_id, "ref": ref, "file": fid}, wave=wave + 1, key=link.resource_id)

    def _on_direct(self, inv, r, task):
        for rid in sorted(r):
            self._link(inv, r[rid], wave=0)

    def _on_catalog(self, inv, r, task):
        named = {self.a.claims[i].resource_id for i in self.tools.catalog_claims_naming(inv.ident)}
        for rid in sorted(named - set(r)):
            inv.contradictions.append({"resource": rid,
                                       "issue": "catalog claim not corroborated or contradicted by evidence"})
        for rid, info in r.items():
            if rid not in inv.links:
                self._link(inv, info["link"], wave=0)
            inv.extra_obs[rid] += self.a.document_observations(rid, info)

    def _on_reference(self, inv, system, task):
        if system:
            self._link(inv, Link(task.inputs["ref"], system, "reference", [task.inputs["file"]]), wave=task.wave)

    def _on_workflow(self, inv, r, task):
        assigned, inv.cand_map = r
        for rid, link in assigned.items():
            inv.links.setdefault(rid, link)   # workflow links are never expanded further
        inv.unresolved = sorted(rid for rid in inv.cand_map if rid not in inv.links)

    def _on_state(self, inv, st, task):
        inv.states[st.resource_id] = st

    def _on_compiled(self, inv, r, task):
        status, steps, notes = r
        if status == "INCOMPLETE" and self.a.cfg.post_cutoff_retention:
            steps = steps + self.a._post_cutoff_retention(inv.req, inv.subject, list(inv.states.values()))
        inv.compiled = (status, steps, notes)

    def _on_validated(self, inv, errors, task):
        inv.errors = errors

    def _on_admissibility(self, inv, violations, task):
        inv.violations = violations

    def _advance(self, inv: Investigation) -> bool:
        """Agenda empty: move to the next phase and queue its work. False when finished."""
        cid = inv.req["case_id"]
        if inv.phase in ("AUTHORIZE", "IDENTIFY", "DISCOVER"):
            if inv.phase != "DISCOVER" or inv.subject is None:
                return False   # AUTHORIZE / IDENTIFY end without a successor only on a stop
            inv.phase = "INVESTIGATE"
            if self.a.cfg.workflow_links:
                self._push(inv, "query_workflow_context",
                           "identifier-less resources the deletion workflow acted on are settled globally "
                           "across all open requests (shared context)", {"case_id": cid})
                inv.phase = "INVESTIGATE:context"
                return True
        if inv.phase in ("INVESTIGATE", "INVESTIGATE:context"):
            inv.phase = "INVESTIGATE:states"
            for rid, link in sorted(inv.links.items()):
                self._push(inv, "resolve_resource_state",
                           f"{link.how} link; establish the state at audit_at from admissible observations",
                           {"resource": rid, "how": link.how}, key=rid)
            if inv.agenda:
                return True
        if inv.phase == "INVESTIGATE:states":
            inv.phase = "COMPILE"
            self._push(inv, "compile_obligations", "apply deletion_rules.csv to the resolved states",
                       {"resources": [r for r in sorted(inv.states)]})
            return True
        if inv.phase == "COMPILE":
            inv.phase = "VALIDATE"
            self._push(inv, "validate_contract", "output contract self-check before emitting", {"case_id": cid})
            self._push(inv, "check_admissibility",
                       "no step may name an unlinked resource or cite evidence unavailable at audit_at",
                       {"case_id": cid})
            return True
        return False

    @staticmethod
    def _row(inv: Investigation) -> Tuple[str, str]:
        status, steps, _ = inv.compiled
        return status, to_json(steps)

    # -- main loop --------------------------------------------------------------------
    def investigate_case(self, req: Dict[str, str]) -> CaseResult:
        inv = Investigation(req)
        self._push(inv, "classify_intent", "deletion must be authorized before any planning",
                   {"case_id": req["case_id"]})
        try:
            while inv.stop is None:
                if not inv.agenda:
                    if not self._advance(inv):
                        break
                    continue
                task = heapq.heappop(inv.agenda)
                if task.tool == "follow_reference" and task.inputs["ref"] in inv.links:
                    continue   # already linked by an earlier query; nothing left to learn
                if inv.phase in ("AUTHORIZE", "IDENTIFY", "DISCOVER"):
                    inv.phase = TOOLS[task.tool].phase
                try:
                    result, ran = self._execute(inv, task)
                except ToolFailure as exc:
                    if task.tool != "resolve_resource_state" or exc.kind != "runtime":
                        raise
                    # Recovery is scoped to the one resource whose evidence could not be evaluated:
                    # its obligations are unknown, the other resources' evidence still stands.
                    inv.unresolved_states[task.inputs["resource"]] = exc.error
                    continue
                if not ran:
                    continue
                self.handlers[task.tool](inv, result, task)
        except BudgetExhausted:
            # Completion was not established, so COMPLETE cannot be claimed; no steps are
            # emitted because the obligations were never compiled and validated.
            return self._finish(inv, "INCOMPLETE", "[]", STOP_BUDGET,
                                {"investigation_incomplete": True,
                                 "error": "query budget exhausted before obligations were compiled"})
        except (ToolFailure, ToolContractError) as exc:
            return self._fallback(inv, f"tool failure: {exc}")
        except Exception as exc:  # a controller defect in one case must not abort the run
            return self._fallback(inv, f"controller error: {type(exc).__name__}: {exc}")
        if inv.stop:
            reason, status = inv.stop
            extra = {"error": "request email does not resolve to a unique identity"} if reason == STOP_IDENTITY else {}
            return self._finish(inv, status, "[]", reason, extra)
        if inv.errors or inv.violations:
            return self._fallback(inv, f"compiled row rejected: {(inv.errors + inv.violations)[:3]}")
        status, steps_json = self._row(inv)
        if inv.unresolved_states:
            names = sorted(inv.unresolved_states)
            # A failure in the controller's own tool layer is recovered by the independent reference
            # pipeline with the complete answer. If that fails too, the evidence for those resources
            # cannot be evaluated by the kernel, and recovery is scoped to them.
            ref, problems = self._reference(inv.req)
            if ref is not None and not problems:
                why = f"unresolved resource state(s) {names}"
                return self._finish(inv, ref.status, ref.steps_json, f"fallback to reference pipeline ({why})",
                                    {"fallback": "reference_pipeline", "fallback_reason": why})
            degraded = {"degraded": {"unresolved_states": dict(sorted(inv.unresolved_states.items())),
                                     "reference_pipeline": "; ".join(problems[:3])},
                        "investigation_incomplete": True}
            if status != "INCOMPLETE":
                # An unknown state may hide a remaining record, so completion is not established.
                why = f"state of {names} could not be resolved; completion not established"
                return self._finish(inv, "INCOMPLETE", "[]", f"failed closed ({why})",
                                    dict(degraded, fallback="failed_closed", fallback_reason=why,
                                         error="unresolved resource state"))
            # Every emitted step concerns another resource and is exactly what the full plan holds for it.
            return self._finish(inv, status, steps_json,
                                f"obligations compiled without {len(names)} unresolved resource state(s): {names}",
                                degraded)
        return self._finish(inv, status, steps_json, STOP_DONE_UNRESOLVED if inv.unresolved else STOP_DONE)

    def _reference(self, req):
        """The reference pipeline's row for `req`, re-validated: (result or None, problems)."""
        try:
            ref = self.a.audit(req)
            linked = {rid for rid in ref.trace.get("links", {})}
            problems = check_row(req["case_id"], ref.status, ref.steps_json) + \
                self.tools.check_admissibility(req, ref.steps_json, linked)
        except Exception as exc:  # the fallback must not crash the run either
            ref, problems = None, [f"reference pipeline failed: {type(exc).__name__}: {exc}"]
        return ref, problems

    def _fallback(self, inv: Investigation, why: str) -> CaseResult:
        """Deterministic safe fallback: the reference pipeline, re-validated; otherwise fail closed."""
        ref, problems = self._reference(inv.req)
        if ref is not None and not problems:
            return self._finish(inv, ref.status, ref.steps_json, f"fallback to reference pipeline ({why})",
                                {"fallback": "reference_pipeline", "fallback_reason": why})
        return self._finish(inv, "INCOMPLETE", "[]", f"failed closed ({why})",
                            {"fallback": "failed_closed", "fallback_reason": why, "investigation_incomplete": True,
                             "error": "; ".join(problems[:3])})

    def _finish(self, inv: Investigation, status: str, steps_json: str, stop: str,
                extra: Optional[Dict[str, Any]] = None) -> CaseResult:
        trace: Dict[str, Any] = {}
        if inv.intent_reason is not None:
            trace["intent"] = inv.intent_reason
        if inv.queries and any(q.tool == "resolve_identity" and q.result != "skipped" for q in inv.queries):
            trace["subject"] = inv.subject
        trace["investigation"] = {"queries": [q.as_dict() for q in inv.queries], "stop_reason": stop,
                                  "phase": inv.phase, "unresolved": inv.unresolved,
                                  "contradictions": inv.contradictions, "budget": self.max_queries}
        if inv.compiled is not None and "fallback" not in (extra or {}):
            states = [inv.states[r] for r in sorted(inv.states)]
            trace["links"] = {rid: (l.how, l.basis) for rid, l in inv.links.items()}
            trace["unresolved_workflow_candidates"] = inv.unresolved
            trace["states"] = {s.resource_id: (s.category, s.exists, s.exposed, s.flow_enabled, s.restore_allowed,
                                               s.evidence_id, s.note) for s in states}
            trace["notes"] = inv.compiled[2]
            cats = {s.category for s in states}
            accounts = [r for r in inv.unresolved if inv.cand_map.get(r, {}).get("category") == "USER_PROFILE"]
            trace["coverage"] = {
                "account": "established" if "USER_PROFILE" in cats else
                           (f"ambiguous({len(accounts)})" if accounts else "none"),
                "retained_security": "established" if "SECURITY_AUDIT_LOG" in cats else "none",
            }
            if not inv.compiled[1]:
                trace["insufficient_evidence"] = True
                trace["error"] = "no evidence-backed resources found"
        trace.update(extra or {})
        return CaseResult(inv.req["case_id"], status, steps_json, trace)

"""Per-case audit: resource state at audit_at -> findings, remediation, verification."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .linking import Link, eligible
from .model import Observation, Scope, Step
from .policy import Policy

# Categories whose rule says the whole record must go.
DEFAULT_CATEGORY_PRIORITY = (
    "SUPPORT_ATTACHMENT", "SECONDARY_IDENTIFIER", "MARKETING_PROFILE", "ACCOUNT_TOMBSTONE",
)


@dataclass
class ResourceState:
    resource_id: str
    system: str
    category: Optional[str]
    exists: Optional[bool]
    exposed: bool                 # prohibited identifiers still present (for REDACT/ANONYMIZE)
    flow_enabled: Optional[bool]
    restore_allowed: Optional[bool]
    retention_until: Optional[str]
    evidence_id: str              # file proving the state at audit_at ("" if none citable)
    basis: List[str] = field(default_factory=list)
    note: str = ""
    obligation_only: bool = False  # known only from the workflow; state unproven


def _category(obs: List[Observation]) -> Optional[str]:
    cats = [o.category for o in obs if o.category]
    for c in DEFAULT_CATEGORY_PRIORITY:
        if c in cats:
            return c
    return max(sorted(set(cats)), key=cats.count) if cats else None  # ties: alphabetical, hash-seed independent


def resolve_state(rid: str, link: Link, obs: List[Observation], scopes: List[Scope], lower: Optional[str],
                  audit_at: str, anonymize_categories, redact_categories,
                  fallback_category: Optional[str] = None, acted_at: Optional[str] = None) -> ResourceState:
    usable = [o for o in obs if eligible(o, lower, audit_at)]
    system = next((o.system for o in usable if o.system), link.system)
    # A record's type is a static attribute: when the in-window evidence leaves it unknown,
    # it may come from any admissible observation (captured and dated no later than
    # audit_at). It never overrides an in-window category, and post-cutoff files are never used.
    admissible = [o for o in obs if o.file_captured_at <= audit_at and o.sort_time <= audit_at]
    category = _category(usable) or _category(admissible) or fallback_category
    state = ResourceState(rid, system, category, None, False, None, None, None, "", list(link.basis))
    stated = [o for o in usable if o.exists is not None or o.flow_enabled is not None
              or o.restore_allowed is not None]
    if stated:
        # Latest observation time wins; within that instant, any physical store
        # still holding the record keeps it present (replica drift).
        latest_time = max(o.sort_time for o in stated)
        latest = [o for o in stated if o.sort_time == latest_time]
        # Per physical store, keep the latest observation; aggregate across stores
        # observed in the same latest file capture.
        latest_file = max(latest, key=lambda o: (o.file_captured_at, o.file_id)).file_id
        # Observations from different files at the same latest instant are simultaneous
        # evidence: every file contributing to that instant is aggregated per physical store.
        latest_files = {o.file_id for o in latest}
        same_file = [o for o in stated if o.file_id in latest_files]
        per_store: Dict[str, Observation] = {}
        for o in sorted(same_file, key=lambda o: o.sort_time):
            per_store[o.store] = o
        current = list(per_store.values())
        exists_vals = [o.exists for o in current if o.exists is not None]
        state.exists = any(exists_vals) if exists_vals else None
        if state.exists is None and any(o.flow_enabled is not None or o.restore_allowed is not None
                                        for o in current):
            state.exists = True
        live = [o for o in current if o.exists]
        if category in anonymize_categories:
            state.exposed = any(o.pii or o.refs for o in live)
        else:
            state.exposed = any(o.pii for o in live)
        flows = [o.flow_enabled for o in current if o.flow_enabled is not None]
        state.flow_enabled = any(flows) if flows else None
        rest = [o.restore_allowed for o in current if o.restore_allowed is not None]
        state.restore_allowed = any(rest) if rest else None
        rets = [o.retention_until for o in current if o.retention_until]
        state.retention_until = max(rets) if rets else None
        # Cite the file whose observation establishes the state: a still-present copy when the
        # record survives anywhere, otherwise the latest capture.
        state.evidence_id = (max(live, key=lambda o: (o.file_captured_at, o.file_id)).file_id
                             if (state.exists and live) else latest_file)
        # A later scoped snapshot that no longer lists the resource proves deletion.
        if state.exists:
            later = [s for s in scopes if s.system == system and s.observed_at > latest_time
                     and s.file_captured_at <= audit_at and (not lower or s.file_captured_at >= lower)
                     and rid not in s.resource_ids and _same_store_family(s, current)]
            if later:
                s = max(later, key=lambda s: (s.observed_at, s.file_id))
                state.exists, state.exposed, state.evidence_id = False, False, s.file_id
                state.note = f"absent from scoped snapshot {s.file_id}:{s.store}"
    if not usable and link.how in ("workflow", "reference"):
        # Resource known only from the deletion workflow. Absence can be shown by
        # an in-window scoped snapshot of the same system taken after the action
        # that enumerates the store without it; otherwise the state is unproven.
        cands = [s for s in scopes if s.system == system and s.file_captured_at <= audit_at
                 and s.observed_at <= audit_at and (not lower or s.file_captured_at >= lower)
                 and acted_at and s.observed_at >= acted_at]
        if cands:
            s = max(cands, key=lambda s: (s.observed_at, s.file_id))
            if rid not in s.resource_ids:
                state.exists, state.evidence_id = False, s.file_id
                state.note = f"absent from scoped snapshot {s.file_id}:{s.store} after workflow action"
        if state.exists is None:
            state.obligation_only = True
            state.note = "known only from workflow activity; state unproven"
    return state


def _same_store_family(scope: Scope, current: List[Observation]) -> bool:
    """A scope only speaks for the physical store it enumerates.

    Snapshots are partial captures (a per-batch inventory can list zero
    accounts), so absence from a different store, table or collection is not
    evidence that the record left the store where it was observed.
    """
    return any(o.store == scope.store for o in current)


def plan_case(states: List[ResourceState], policy: Policy, audit_at: Optional[str] = None
              ) -> Tuple[str, List[Step], List[str]]:
    findings: List[Step] = []
    remediation: List[Step] = []
    future_verify: List[Step] = []
    proven_verify: List[Step] = []
    obligations: List[Step] = []   # future checks without proof of current state
    notes: List[str] = []
    for st in states:
        cat = st.category
        action = policy.action(cat) if cat else "UNKNOWN"
        sysid, rid, ev = st.system, st.resource_id, st.evidence_id
        if action == "UNKNOWN":
            notes.append(f"{rid}: no category/policy; skipped")
            continue
        if cat == "BACKUP_ARCHIVE":
            if st.exists is False:
                continue
            if st.restore_allowed is False:
                proven_verify.append(Step("VERIFICATION", sysid, rid, "VERIFY_RESTORE_BLOCKED", ev))
            else:
                findings.append(Step("FINDING", sysid, rid, "REPORT_RESTORE_RISK", ev))
                remediation.append(Step("REMEDIATION", sysid, rid, "BLOCK_RESTORE"))
                future_verify.append(Step("VERIFICATION", sysid, rid, "VERIFY_RESTORE_BLOCKED"))
            continue
        if action == "DISABLE":
            if st.exists is False:
                continue
            if st.flow_enabled is False:
                proven_verify.append(Step("VERIFICATION", sysid, rid, "VERIFY_DATA_FLOW_DISABLED", ev))
            else:
                findings.append(Step("FINDING", sysid, rid, "REPORT_ACTIVE_DATA_FLOW", ev))
                remediation.append(Step("REMEDIATION", sysid, rid, "DISABLE_DATA_FLOW"))
                future_verify.append(Step("VERIFICATION", sysid, rid, "VERIFY_DATA_FLOW_DISABLED"))
            continue
        if action == "RETAIN":
            # rule_12/rule_13 retain only "within the supplied retention period" / while "a
            # supplied legal hold is active". A supplied end date before audit_at means the
            # condition is not met: no retention obligation. No other rule authorizes
            # deleting the record, so nothing is planned and the gap is reported.
            if st.retention_until and audit_at and st.retention_until < audit_at:
                notes.append(f"{rid}: {cat} retention ended {st.retention_until}; condition of "
                             f"{policy.rule_id(cat)} not met and no rule authorizes another action")
                continue
            if st.exists:
                proven_verify.append(Step("VERIFICATION", sysid, rid, "VERIFY_RECORD_PRESENT", ev))
            continue
        if st.exists is None:
            if st.obligation_only and action in ("DELETE", "REDACT", "ANONYMIZE"):
                op = {"DELETE": "VERIFY_RECORD_ABSENT", "REDACT": "VERIFY_RECORD_REDACTED",
                      "ANONYMIZE": "VERIFY_RECORD_ANONYMIZED"}[action]
                obligations.append(Step("VERIFICATION", sysid, rid, op))
                continue
            notes.append(f"{rid}: state at audit_at unknown; skipped")
            continue
        if action == "DELETE":
            if st.exists:
                findings.append(Step("FINDING", sysid, rid, "REPORT_REMAINING_RECORD", ev))
                remediation.append(Step("REMEDIATION", sysid, rid, "DELETE_RECORD"))
                future_verify.append(Step("VERIFICATION", sysid, rid, "VERIFY_RECORD_ABSENT"))
            else:
                proven_verify.append(Step("VERIFICATION", sysid, rid, "VERIFY_RECORD_ABSENT", ev))
        elif action in ("REDACT", "ANONYMIZE"):
            fix, ver = (("REDACT_RECORD", "VERIFY_RECORD_REDACTED") if action == "REDACT"
                        else ("ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"))
            if st.exists and st.exposed:
                findings.append(Step("FINDING", sysid, rid, "REPORT_EXPOSED_PII", ev))
                remediation.append(Step("REMEDIATION", sysid, rid, fix))
                future_verify.append(Step("VERIFICATION", sysid, rid, ver))
            elif st.exists:
                proven_verify.append(Step("VERIFICATION", sysid, rid, ver, ev))
            else:
                notes.append(f"{rid}: record absent although policy is {action}")
    if findings:
        # Future checks: every remediated resource plus already-compliant obligations.
        verify = future_verify + obligations + [Step(s.phase, s.system_id, s.resource_id, s.operation, "")
                                                for s in proven_verify]
        steps = findings + remediation + verify
        status = "INCOMPLETE"
    else:
        steps = [s for s in proven_verify if s.evidence_id]
        status = "COMPLETE"
        if obligations:
            notes.append(f"unproven workflow resources omitted from COMPLETE proof: "
                         f"{[s.resource_id for s in obligations]}")
    return status, steps, notes

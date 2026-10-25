"""Shared data structures and the published output vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# Operation -> state, in the published table order (problem_statement.md).
OPERATIONS = {
    "FINDING": [
        ("REPORT_REMAINING_RECORD", "PRESENT"),
        ("REPORT_EXPOSED_PII", "PRESENT"),
        ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE"),
        ("REPORT_RESTORE_RISK", "ACTIVE"),
    ],
    "REMEDIATION": [
        ("DISABLE_DATA_FLOW", "DISABLED"),
        ("BLOCK_RESTORE", "BLOCKED"),
        ("DELETE_RECORD", "ABSENT"),
        ("REDACT_RECORD", "REDACTED"),
        ("ANONYMIZE_RECORD", "ANONYMIZED"),
    ],
    "VERIFICATION": [
        ("VERIFY_RECORD_ABSENT", "ABSENT"),
        ("VERIFY_RECORD_PRESENT", "PRESENT"),
        ("VERIFY_RECORD_REDACTED", "REDACTED"),
        ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
        ("VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
        ("VERIFY_RESTORE_BLOCKED", "BLOCKED"),
    ],
}
STATE_OF = {op: st for ops in OPERATIONS.values() for op, st in ops}
PHASE_OF = {op: ph for ph, ops in OPERATIONS.items() for op, _ in ops}
OP_RANK = {op: i for ops in OPERATIONS.values() for i, (op, _) in enumerate(ops)}
PHASE_RANK = {ph: i for i, ph in enumerate(PHASES)}

STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


@dataclass
class Observation:
    """One evidence statement about one resource at one point in time.

    Times are kept separate on purpose:
      file_captured_at - manifest capture time of the whole evidence file
      observed_at      - record/line/sub-snapshot time inside the file
    """

    file_id: str
    file_captured_at: str
    observed_at: str
    system: str                      # logical system_id used in output
    resource_id: str
    store: str                       # physical store / table / collection
    locator: str                     # where inside the file
    exists: Optional[bool] = None    # True present, False confirmed absent
    category: Optional[str] = None   # deletion_rules.csv data_category hint
    pii: Dict[str, str] = field(default_factory=dict)       # direct identifiers still present
    refs: List[str] = field(default_factory=list)           # stable references (user_id style)
    link_values: FrozenSet[str] = frozenset()               # identifiers usable for subject linking
    references: List[str] = field(default_factory=list)     # other resources this record names
    flow_enabled: Optional[bool] = None
    restore_allowed: Optional[bool] = None
    retention_until: Optional[str] = None
    source_kind: str = "text"        # text | sql | json | log | pdf | image

    @property
    def sort_time(self) -> str:
        return self.observed_at or self.file_captured_at


@dataclass
class Scope:
    """A snapshot that enumerates every record of a system/collection at a time.

    Absence of a resource from a scope observed after its deletion is negative
    evidence that the resource no longer exists in that store.
    """

    file_id: str
    file_captured_at: str
    observed_at: str
    system: str
    store: str
    resource_ids: FrozenSet[str]


@dataclass
class Step:
    phase: str
    system_id: str
    resource_id: str
    operation: str
    evidence_id: str = ""

    @property
    def state(self) -> str:
        return STATE_OF[self.operation]

    def sort_key(self):
        return (PHASE_RANK[self.phase], OP_RANK[self.operation], self.system_id, self.resource_id)

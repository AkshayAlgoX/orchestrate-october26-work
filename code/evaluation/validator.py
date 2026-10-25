"""Output contract validator for HackerRank Orchestrate: Ghost Data Still Exists.

This module provides standard-library-first validation for output.csv and
prediction files against the challenge output contract specification.
"""

from __future__ import annotations

import csv
import os
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ==============================================================================
# Specification Constants
# ==============================================================================

REQUIRED_CSV_HEADER = ["case_id", "deletion_status", "steps_json"]

ALLOWED_STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")

ALLOWED_PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

ALLOWED_OPERATIONS: Dict[str, Dict[str, str]] = {
    "FINDING": {
        "REPORT_REMAINING_RECORD": "PRESENT",
        "REPORT_EXPOSED_PII": "PRESENT",
        "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
        "REPORT_RESTORE_RISK": "ACTIVE",
    },
    "REMEDIATION": {
        "DISABLE_DATA_FLOW": "DISABLED",
        "BLOCK_RESTORE": "BLOCKED",
        "DELETE_RECORD": "ABSENT",
        "REDACT_RECORD": "REDACTED",
        "ANONYMIZE_RECORD": "ANONYMIZED",
    },
    "VERIFICATION": {
        "VERIFY_RECORD_ABSENT": "ABSENT",
        "VERIFY_RECORD_PRESENT": "PRESENT",
        "VERIFY_RECORD_REDACTED": "REDACTED",
        "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED",
        "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
        "VERIFY_RESTORE_BLOCKED": "BLOCKED",
    },
}

PHASE_ORDER = {
    "FINDING": 0,
    "REMEDIATION": 1,
    "VERIFICATION": 2,
}

OPERATION_ORDER: Dict[str, int] = {
    # FINDING
    "REPORT_REMAINING_RECORD": 0,
    "REPORT_EXPOSED_PII": 1,
    "REPORT_ACTIVE_DATA_FLOW": 2,
    "REPORT_RESTORE_RISK": 3,
    # REMEDIATION
    "DISABLE_DATA_FLOW": 0,
    "BLOCK_RESTORE": 1,
    "DELETE_RECORD": 2,
    "REDACT_RECORD": 3,
    "ANONYMIZE_RECORD": 4,
    # VERIFICATION
    "VERIFY_RECORD_ABSENT": 0,
    "VERIFY_RECORD_PRESENT": 1,
    "VERIFY_RECORD_REDACTED": 2,
    "VERIFY_RECORD_ANONYMIZED": 3,
    "VERIFY_DATA_FLOW_DISABLED": 4,
    "VERIFY_RESTORE_BLOCKED": 5,
}

REQUIRED_STEP_FIELDS = (
    "step_no",
    "phase",
    "system_id",
    "resource_id",
    "operation",
    "state",
    "evidence_id",
)

REQUIRED_STEP_FIELD_SET = set(REQUIRED_STEP_FIELDS)


# ==============================================================================
# Validation Error / Warning / Result Dataclasses
# ==============================================================================

@dataclass
class ValidationError:
    """Represents a specification contract violation."""
    case_id: Optional[str]
    rule: str
    message: str
    line_number: Optional[int] = None
    step_no: Optional[int] = None

    def __str__(self) -> str:
        loc = []
        if self.line_number is not None:
            loc.append(f"line {self.line_number}")
        if self.case_id:
            loc.append(f"case {self.case_id}")
        if self.step_no is not None:
            loc.append(f"step {self.step_no}")
        loc_str = f" [{', '.join(loc)}]" if loc else ""
        return f"ERROR ({self.rule}){loc_str}: {self.message}"


@dataclass
class ValidationWarning:
    """Represents a disputed check, style deviation, or non-fatal anomaly."""
    case_id: Optional[str]
    category: str
    message: str
    line_number: Optional[int] = None
    step_no: Optional[int] = None

    def __str__(self) -> str:
        loc = []
        if self.line_number is not None:
            loc.append(f"line {self.line_number}")
        if self.case_id:
            loc.append(f"case {self.case_id}")
        if self.step_no is not None:
            loc.append(f"step {self.step_no}")
        loc_str = f" [{', '.join(loc)}]" if loc else ""
        return f"WARNING ({self.category}){loc_str}: {self.message}"


@dataclass
class ValidationResult:
    """Summary of validation execution."""
    is_valid: bool
    total_rows: int
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationWarning] = field(default_factory=list)
    disputed_findings: List[ValidationWarning] = field(default_factory=list)

    def summary(self) -> str:
        status = "PASSED" if self.is_valid else "FAILED"
        lines = [
            f"Validation Result: {status}",
            f"Total Rows Checked: {self.total_rows}",
            f"Total Errors: {len(self.errors)}",
            f"Total Warnings: {len(self.warnings)}",
            f"Disputed Checks Tracked: {len(self.disputed_findings)}",
        ]
        if self.errors:
            lines.append("\nErrors Encountered:")
            for err in self.errors[:25]:
                lines.append(f"  * {err}")
            if len(self.errors) > 25:
                lines.append(f"  ... and {len(self.errors) - 25} more errors.")
        if self.warnings:
            lines.append("\nWarnings:")
            for warn in self.warnings[:15]:
                lines.append(f"  * {warn}")
            if len(self.warnings) > 15:
                lines.append(f"  ... and {len(self.warnings) - 15} more warnings.")
        if self.disputed_findings:
            lines.append("\nDisputed Findings (separated from hard contract errors):")
            for df in self.disputed_findings[:15]:
                lines.append(f"  * {df}")
            if len(self.disputed_findings) > 15:
                lines.append(f"  ... and {len(self.disputed_findings) - 15} more disputed checks.")
        return "\n".join(lines)


# ==============================================================================
# Validator Class
# ==============================================================================

class OutputValidator:
    """Deterministic validator for Ghost Data Still Exists output CSV.

    Validates:
      1. Exact CSV headers, row counts, and no extraneous columns.
      2. Exactly matching request case IDs with no duplicates, missing, or extras.
      3. Valid deletion statuses (COMPLETE, INCOMPLETE, NOT_APPLICABLE).
      4. Valid JSON array serialization for steps_json.
      5. Empty JSON array ([]) for NOT_APPLICABLE cases.
      6. Exact step object fields with no extra or missing keys.
      7. Strictly consecutive step numbering starting at 1.
      8. Allowed phase / operation / state combinations.
      9. Status-specific phase constraints (e.g. COMPLETE contains only VERIFICATION).
     10. Phase and canonical operation ordering within each phase.
     11. Strict evidence citation rules:
         - COMPLETE: non-empty citation proving compliant state.
         - INCOMPLETE: non-empty citation for findings; empty string for future remediation/verification.
     12. Permitted citation namespaces (file_id, activity_id, audit_id).
     13. Eligible citation times (evidence timestamp <= case audit_at).
     14. Disputed assumptions kept explicitly separated:
         - Lower citation bound (timestamp >= requested_at): recorded as disputed check,
           not silently failing submissions unless enforce_requested_at_bound is set.
    """

    def __init__(
        self,
        dataset_dir: Optional[Path | str] = None,
        expected_case_ids: Optional[List[str]] = None,
        enforce_requested_at_bound: bool = False,
    ):
        """Initialize validator with dataset context.

        Args:
            dataset_dir: Path to directory containing participant datasets (evidence_files.csv,
                         system_activity.csv, user_audit_log.csv, deletion_requests.csv).
                         If None, searches standard locations relative to repo root.
            expected_case_ids: Optional explicit list of expected case_ids in order.
            enforce_requested_at_bound: If True, treats evidence captured before requested_at
                                        as an error. Default is False (disputed assumption).
        """
        self.dataset_dir = self._resolve_dataset_dir(dataset_dir)
        self.enforce_requested_at_bound = enforce_requested_at_bound
        self.expected_case_ids = expected_case_ids

        # Citation namespaces and timestamps: id -> iso_timestamp
        self.evidence_timestamps: Dict[str, str] = {}
        # Case temporal context: case_id -> {requested_at, audit_at}
        self.case_context: Dict[str, Dict[str, str]] = {}

        if self.dataset_dir and self.dataset_dir.is_dir():
            self._load_evidence_namespaces()
            self._load_case_metadata()

    def _resolve_dataset_dir(self, dataset_dir: Optional[Path | str]) -> Optional[Path]:
        if dataset_dir:
            p = Path(dataset_dir)
            return p if p.is_dir() else None
        # Try typical candidate paths
        candidates = [
            Path(os.environ["GHOST_AUDIT_DATASET"]) if os.environ.get("GHOST_AUDIT_DATASET") else None,
            Path("dataset"),
            Path(__file__).resolve().parent.parent.parent / "dataset",
        ]
        candidates = [c for c in candidates if c is not None]
        for c in candidates:
            if c.is_dir() and (c / "evidence_files.csv").is_file():
                return c
        return None

    def _load_evidence_namespaces(self) -> None:
        """Load file_id, activity_id, and audit_id timestamps."""
        assert self.dataset_dir is not None

        # 1. evidence_files.csv: file_id -> captured_at
        p_ev = self.dataset_dir / "evidence_files.csv"
        if p_ev.is_file():
            with open(p_ev, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    fid = row.get("file_id")
                    cat = row.get("captured_at")
                    if fid and cat:
                        self.evidence_timestamps[fid] = cat

        # 2. system_activity.csv: activity_id -> event_time
        p_act = self.dataset_dir / "system_activity.csv"
        if p_act.is_file():
            with open(p_act, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    aid = row.get("activity_id")
                    etime = row.get("event_time")
                    if aid and etime:
                        self.evidence_timestamps[aid] = etime

        # 3. user_audit_log.csv: audit_id -> event_time
        p_aud = self.dataset_dir / "user_audit_log.csv"
        if p_aud.is_file():
            with open(p_aud, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    uid = row.get("audit_id")
                    etime = row.get("event_time")
                    if uid and etime:
                        self.evidence_timestamps[uid] = etime

    def _load_case_metadata(self) -> None:
        """Load case timestamps (requested_at, audit_at) from deletion requests."""
        assert self.dataset_dir is not None

        # Try both full evaluation requests and sample requests
        for fname in ("deletion_requests.csv", "sample_deletion_requests.csv"):
            p = self.dataset_dir / fname
            if p.is_file():
                with open(p, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        cid = row.get("case_id")
                        if cid and cid not in self.case_context:
                            self.case_context[cid] = {
                                "requested_at": row.get("requested_at", ""),
                                "audit_at": row.get("audit_at", ""),
                            }

        if self.expected_case_ids is None:
            # Default to production deletion_requests.csv if available
            p_prod = self.dataset_dir / "deletion_requests.csv"
            if p_prod.is_file():
                with open(p_prod, "r", encoding="utf-8") as f:
                    self.expected_case_ids = [
                        row["case_id"] for row in csv.DictReader(f) if "case_id" in row
                    ]

    def validate_file(self, file_path: Path | str) -> ValidationResult:
        """Validate an output CSV file from disk."""
        path = Path(file_path)
        if not path.is_file():
            return ValidationResult(
                is_valid=False,
                total_rows=0,
                errors=[
                    ValidationError(
                        case_id=None,
                        rule="FILE_NOT_FOUND",
                        message=f"Output file does not exist: {path}",
                    )
                ],
            )

        with open(path, "r", encoding="utf-8", newline="") as f:
            content = f.read()
        return self.validate_content(content)

    def validate_content(self, csv_content: str) -> ValidationResult:
        """Validate raw CSV text content against the output contract."""
        errors: List[ValidationError] = []
        warnings: List[ValidationWarning] = []
        disputed_findings: List[ValidationWarning] = []

        if not csv_content.strip():
            return ValidationResult(
                is_valid=False,
                total_rows=0,
                errors=[
                    ValidationError(
                        case_id=None,
                        rule="EMPTY_FILE",
                        message="CSV content is empty.",
                        line_number=1,
                    )
                ],
            )

        # Step 1: Parse CSV structure with standard library reader
        try:
            reader = csv.reader(io.StringIO(csv_content))
            rows = list(reader)
        except Exception as e:
            return ValidationResult(
                is_valid=False,
                total_rows=0,
                errors=[
                    ValidationError(
                        case_id=None,
                        rule="MALFORMED_CSV",
                        message=f"CSV parsing error: {e}",
                    )
                ],
            )

        if not rows:
            return ValidationResult(
                is_valid=False,
                total_rows=0,
                errors=[
                    ValidationError(
                        case_id=None,
                        rule="NO_HEADER",
                        message="File contains no rows.",
                        line_number=1,
                    )
                ],
            )

        # Step 2: Validate Header
        header = rows[0]
        if header != REQUIRED_CSV_HEADER:
            errors.append(
                ValidationError(
                    case_id=None,
                    rule="INVALID_HEADER",
                    message=(
                        f"Expected exact header {REQUIRED_CSV_HEADER}, "
                        f"got {header}"
                    ),
                    line_number=1,
                )
            )

        data_rows = rows[1:]
        total_rows = len(data_rows)

        # Step 3: Validate Row Count against expected case list if specified
        observed_case_ids: List[str] = []
        seen_case_ids: Set[str] = set()

        if self.expected_case_ids is not None:
            expected_count = len(self.expected_case_ids)
            if total_rows != expected_count:
                errors.append(
                    ValidationError(
                        case_id=None,
                        rule="ROW_COUNT_MISMATCH",
                        message=(
                            f"Expected exactly {expected_count} rows, "
                            f"found {total_rows} data rows."
                        ),
                    )
                )

        # Step 4: Validate each row
        for row_idx, row in enumerate(data_rows, start=2):
            if len(row) != 3:
                errors.append(
                    ValidationError(
                        case_id=row[0] if row else None,
                        rule="COLUMN_COUNT_MISMATCH",
                        message=f"Expected 3 columns, found {len(row)}.",
                        line_number=row_idx,
                    )
                )
                continue

            case_id, deletion_status, steps_json_raw = row[0].strip(), row[1].strip(), row[2]

            # Duplicate check
            if case_id in seen_case_ids:
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="DUPLICATE_CASE_ID",
                        message=f"Duplicate case_id '{case_id}' encountered.",
                        line_number=row_idx,
                    )
                )
            seen_case_ids.add(case_id)
            observed_case_ids.append(case_id)

            # Status check
            if deletion_status not in ALLOWED_STATUSES:
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="INVALID_STATUS",
                        message=(
                            f"Invalid deletion_status '{deletion_status}'. "
                            f"Must be one of {ALLOWED_STATUSES}."
                        ),
                        line_number=row_idx,
                    )
                )

            # JSON Parsing check
            try:
                steps = json.loads(steps_json_raw)
            except Exception as e:
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="INVALID_JSON",
                        message=f"Could not parse steps_json as JSON: {e}",
                        line_number=row_idx,
                    )
                )
                continue

            if not isinstance(steps, list):
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="STEPS_NOT_ARRAY",
                        message=f"steps_json must be a JSON array (list), got {type(steps).__name__}.",
                        line_number=row_idx,
                    )
                )
                continue

            # NOT_APPLICABLE contract check: steps_json must be []
            if deletion_status == "NOT_APPLICABLE":
                if len(steps) != 0:
                    errors.append(
                        ValidationError(
                            case_id=case_id,
                            rule="NOT_APPLICABLE_NONEMPTY_STEPS",
                            message=(
                                f"NOT_APPLICABLE case must have empty steps array [], "
                                f"found {len(steps)} steps."
                            ),
                            line_number=row_idx,
                        )
                    )
                continue

            # Non-empty steps requirement for COMPLETE and INCOMPLETE
            if deletion_status in ("COMPLETE", "INCOMPLETE") and len(steps) == 0:
                warnings.append(
                    ValidationWarning(
                        case_id=case_id,
                        category="EMPTY_STEPS_ON_ACTIVE_CASE",
                        message=f"Status is {deletion_status} but steps_json is empty array [].",
                        line_number=row_idx,
                    )
                )

            # Validate steps array contents and ordering
            self._validate_case_steps(
                case_id=case_id,
                deletion_status=deletion_status,
                steps=steps,
                line_number=row_idx,
                errors=errors,
                warnings=warnings,
                disputed_findings=disputed_findings,
            )

        # Step 5: Validate case IDs set and order against expected
        if self.expected_case_ids is not None:
            expected_set = set(self.expected_case_ids)
            missing = expected_set - seen_case_ids
            extra = seen_case_ids - expected_set

            if missing:
                errors.append(
                    ValidationError(
                        case_id=None,
                        rule="MISSING_CASE_IDS",
                        message=f"Missing {len(missing)} expected case IDs (e.g. {sorted(missing)[:5]}).",
                    )
                )
            if extra:
                errors.append(
                    ValidationError(
                        case_id=None,
                        rule="EXTRA_CASE_IDS",
                        message=f"Encountered {len(extra)} unexpected case IDs (e.g. {sorted(extra)[:5]}).",
                    )
                )

            # Order check
            if observed_case_ids != self.expected_case_ids:
                # Check whether difference is just natural sort vs string sort
                string_sorted = sorted(self.expected_case_ids)
                if observed_case_ids == string_sorted:
                    disputed_findings.append(
                        ValidationWarning(
                            case_id=None,
                            category="LEXICOGRAPHICAL_SORT_DETECTED",
                            message=(
                                "Cases are sorted lexicographically by string instead of "
                                "the original request file natural order."
                            ),
                        )
                    )
                else:
                    warnings.append(
                        ValidationWarning(
                            case_id=None,
                            category="CASE_ID_ORDER_MISMATCH",
                            message="Row sequence does not match expected request order.",
                        )
                    )

        is_valid = len(errors) == 0
        return ValidationResult(
            is_valid=is_valid,
            total_rows=total_rows,
            errors=errors,
            warnings=warnings,
            disputed_findings=disputed_findings,
        )

    def _validate_case_steps(
        self,
        case_id: str,
        deletion_status: str,
        steps: List[Any],
        line_number: int,
        errors: List[ValidationError],
        warnings: List[ValidationWarning],
        disputed_findings: List[ValidationWarning],
    ) -> None:
        """Validate individual steps in a case."""
        case_ctx = self.case_context.get(case_id, {})
        audit_at = case_ctx.get("audit_at", "")
        requested_at = case_ctx.get("requested_at", "")

        prev_order_key: Optional[Tuple[int, int, str, str]] = None
        has_finding = False
        has_remediation = False
        has_verification = False

        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="STEP_NOT_OBJECT",
                        message=f"Step {idx} is not a JSON object, got {type(step).__name__}.",
                        line_number=line_number,
                        step_no=idx,
                    )
                )
                continue

            # Field presence and exact keys check
            keys = set(step.keys())
            if keys != REQUIRED_STEP_FIELD_SET:
                missing = REQUIRED_STEP_FIELD_SET - keys
                extra = keys - REQUIRED_STEP_FIELD_SET
                msg_parts = []
                if missing:
                    msg_parts.append(f"missing: {sorted(missing)}")
                if extra:
                    msg_parts.append(f"extra: {sorted(extra)}")
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="STEP_FIELD_MISMATCH",
                        message=f"Step fields mismatch ({'; '.join(msg_parts)}).",
                        line_number=line_number,
                        step_no=idx,
                    )
                )

            step_no = step.get("step_no")
            phase = step.get("phase")
            system_id = step.get("system_id", "")
            resource_id = step.get("resource_id", "")
            operation = step.get("operation")
            state = step.get("state")
            evidence_id = step.get("evidence_id", "")

            # Step numbering: starts at 1, strictly consecutive
            if step_no != idx:
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="NONCONSECUTIVE_STEP_NO",
                        message=f"Expected step_no={idx}, found {step_no}.",
                        line_number=line_number,
                        step_no=idx,
                    )
                )

            # Phase check
            if phase not in ALLOWED_PHASES:
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="INVALID_PHASE",
                        message=f"Invalid phase '{phase}'. Must be one of {ALLOWED_PHASES}.",
                        line_number=line_number,
                        step_no=idx,
                    )
                )
                continue

            if phase == "FINDING":
                has_finding = True
            elif phase == "REMEDIATION":
                has_remediation = True
            elif phase == "VERIFICATION":
                has_verification = True

            # Phase constraints per status
            if deletion_status == "COMPLETE" and phase != "VERIFICATION":
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="COMPLETE_CASE_CONTAINS_NON_VERIFICATION",
                        message=f"COMPLETE case cannot contain {phase} step.",
                        line_number=line_number,
                        step_no=idx,
                    )
                )

            # Operation & State validity
            allowed_ops = ALLOWED_OPERATIONS[phase]
            if operation not in allowed_ops:
                errors.append(
                    ValidationError(
                        case_id=case_id,
                        rule="INVALID_OPERATION_FOR_PHASE",
                        message=(
                            f"Operation '{operation}' is not allowed in phase '{phase}'. "
                            f"Allowed: {sorted(allowed_ops.keys())}."
                        ),
                        line_number=line_number,
                        step_no=idx,
                    )
                )
            else:
                expected_state = allowed_ops[operation]
                if state != expected_state:
                    errors.append(
                        ValidationError(
                            case_id=case_id,
                            rule="INVALID_STATE_FOR_OPERATION",
                            message=(
                                f"Operation '{operation}' requires state '{expected_state}', "
                                f"got '{state}'."
                            ),
                            line_number=line_number,
                            step_no=idx,
                        )
                    )

            # Evidence ID citation constraints
            if deletion_status == "COMPLETE":
                if not evidence_id:
                    errors.append(
                        ValidationError(
                            case_id=case_id,
                            rule="COMPLETE_MISSING_EVIDENCE_CITATION",
                            message="COMPLETE verification step must cite valid evidence_id.",
                            line_number=line_number,
                            step_no=idx,
                        )
                    )
            elif deletion_status == "INCOMPLETE":
                if phase == "FINDING" and not evidence_id:
                    errors.append(
                        ValidationError(
                            case_id=case_id,
                            rule="INCOMPLETE_FINDING_MISSING_EVIDENCE",
                            message="INCOMPLETE FINDING step must cite participant evidence_id.",
                            line_number=line_number,
                            step_no=idx,
                        )
                    )
                elif phase in ("REMEDIATION", "VERIFICATION") and evidence_id != "":
                    errors.append(
                        ValidationError(
                            case_id=case_id,
                            rule="FUTURE_STEP_HAS_EVIDENCE_CITATION",
                            message=(
                                f"{phase} step on INCOMPLETE case represents proposed work "
                                f"and must have empty evidence_id (''), got '{evidence_id}'."
                            ),
                            line_number=line_number,
                            step_no=idx,
                        )
                    )

            # Namespace & Temporal checks on cited evidence
            if evidence_id:
                if self.evidence_timestamps:
                    if evidence_id not in self.evidence_timestamps:
                        errors.append(
                            ValidationError(
                                case_id=case_id,
                                rule="UNKNOWN_EVIDENCE_ID",
                                message=(
                                    f"Evidence ID '{evidence_id}' not found in evidence_files.csv, "
                                    f"system_activity.csv, or user_audit_log.csv."
                                ),
                                line_number=line_number,
                                step_no=idx,
                            )
                        )
                    else:
                        ev_time = self.evidence_timestamps[evidence_id]

                        # Upper bound: evidence_timestamp <= audit_at (HARD CONTRACT SPEC)
                        if audit_at and ev_time > audit_at:
                            errors.append(
                                ValidationError(
                                    case_id=case_id,
                                    rule="FUTURE_CITATION",
                                    message=(
                                        f"Evidence '{evidence_id}' timestamp ({ev_time}) is after "
                                        f"case audit cutoff audit_at ({audit_at})."
                                    ),
                                    line_number=line_number,
                                    step_no=idx,
                                )
                            )

                        # Lower bound: evidence_timestamp >= requested_at (DISPUTED ASSUMPTION)
                        if requested_at and ev_time < requested_at:
                            if self.enforce_requested_at_bound:
                                errors.append(
                                    ValidationError(
                                        case_id=case_id,
                                        rule="CITATION_BEFORE_REQUESTED_AT",
                                        message=(
                                            f"Evidence '{evidence_id}' timestamp ({ev_time}) is before "
                                            f"requested_at ({requested_at}) [enforce_requested_at_bound=True]."
                                        ),
                                        line_number=line_number,
                                        step_no=idx,
                                    )
                                )
                            else:
                                disputed_findings.append(
                                    ValidationWarning(
                                        case_id=case_id,
                                        category="CITATION_BEFORE_REQUESTED_AT",
                                        message=(
                                            f"Evidence '{evidence_id}' captured at {ev_time} is prior "
                                            f"to requested_at {requested_at}. (Disputed check: tracked "
                                            f"without failing contract)."
                                        ),
                                        line_number=line_number,
                                        step_no=idx,
                                    )
                                )

            # Order check: (phase_order, op_order, system_id, resource_id)
            p_order = PHASE_ORDER.get(phase, 999)
            o_order = OPERATION_ORDER.get(operation, 999)
            order_key = (p_order, o_order, str(system_id), str(resource_id))

            if prev_order_key is not None:
                if order_key < prev_order_key:
                    errors.append(
                        ValidationError(
                            case_id=case_id,
                            rule="STEP_ORDER_VIOLATION",
                            message=(
                                f"Step {idx} ({phase}:{operation}:{system_id}:{resource_id}) "
                                f"is out of order relative to step {idx - 1}."
                            ),
                            line_number=line_number,
                            step_no=idx,
                        )
                    )
            prev_order_key = order_key

        # INCOMPLETE completeness check
        if deletion_status == "INCOMPLETE" and len(steps) > 0:
            if not has_finding:
                warnings.append(
                    ValidationWarning(
                        case_id=case_id,
                        category="INCOMPLETE_WITHOUT_FINDINGS",
                        message="INCOMPLETE case has no FINDING steps.",
                        line_number=line_number,
                    )
                )
            if not has_remediation:
                warnings.append(
                    ValidationWarning(
                        case_id=case_id,
                        category="INCOMPLETE_WITHOUT_REMEDIATION",
                        message="INCOMPLETE case has no REMEDIATION steps.",
                        line_number=line_number,
                    )
                )
            if not has_verification:
                warnings.append(
                    ValidationWarning(
                        case_id=case_id,
                        category="INCOMPLETE_WITHOUT_VERIFICATION",
                        message="INCOMPLETE case has no post-remediation VERIFICATION steps.",
                        line_number=line_number,
                    )
                )

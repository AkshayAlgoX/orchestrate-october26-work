"""Public-sample evaluation harness for HackerRank Orchestrate: Ghost Data Still Exists.

This module evaluates predictions against dataset/sample_output.csv across multiple
orthogonal dimensions:
  1. Case-level Deletion Status Accuracy
  2. Exact-Array Match Accuracy
  3. Step-level Precision, Recall, and F1
  4. Field-level Accuracy (phase, operation, state, system_id, resource_id, evidence_id)
  5. Sequence & Order Inversion Errors
  6. Evidence Citation Agreement

IMPORTANT: All metrics produced by this module are public-sample metrics. They are
evaluated exclusively against dataset/sample_output.csv (32 sample cases) and MUST NOT
be represented as estimates or guarantees of hidden evaluation test scores.
"""

from __future__ import annotations

import csv
import os
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class FieldAccuracyStats:
    phase_matches: int = 0
    operation_matches: int = 0
    state_matches: int = 0
    system_id_matches: int = 0
    resource_id_matches: int = 0
    evidence_id_matches: int = 0
    total_aligned_steps: int = 0

    def accuracy(self, matches: int) -> float:
        return (matches / self.total_aligned_steps) if self.total_aligned_steps > 0 else 1.0


@dataclass
class EvaluationReport:
    """Detailed evaluation report on public samples."""
    total_cases: int = 0
    status_matches: int = 0
    exact_array_matches: int = 0
    
    # Status confusion matrix: golden -> pred -> count
    status_confusion: Dict[str, Dict[str, int]] = field(default_factory=dict)

    # Step-level metrics
    tp_steps: int = 0
    fp_steps: int = 0
    fn_steps: int = 0

    # Field-level statistics
    field_stats: FieldAccuracyStats = field(default_factory=FieldAccuracyStats)

    # Order errors
    cases_with_order_errors: int = 0
    total_order_inversions: int = 0

    # Citation agreement on cited steps
    citation_matches: int = 0
    total_cited_steps: int = 0

    # Supplementary metrics (added): the aligned-step field accuracy above aligns on
    # the full (phase, system_id, resource_id, operation, state) key, so those five
    # fields match by construction. These metrics align more loosely to expose
    # wrong operations/citations on the right resource, and resource discovery.
    resource_tp: int = 0
    resource_fp: int = 0
    resource_fn: int = 0
    loose_aligned: int = 0
    loose_operation_matches: int = 0
    loose_evidence_matches: int = 0

    # Per-case details
    case_results: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def status_accuracy(self) -> float:
        return (self.status_matches / self.total_cases) if self.total_cases > 0 else 0.0

    @property
    def exact_array_accuracy(self) -> float:
        return (self.exact_array_matches / self.total_cases) if self.total_cases > 0 else 0.0

    @property
    def step_precision(self) -> float:
        denom = self.tp_steps + self.fp_steps
        return (self.tp_steps / denom) if denom > 0 else 1.0

    @property
    def step_recall(self) -> float:
        denom = self.tp_steps + self.fn_steps
        return (self.tp_steps / denom) if denom > 0 else 1.0

    @property
    def step_f1(self) -> float:
        p, r = self.step_precision, self.step_recall
        return (2 * p * r / (p + r)) if (p + r) > 0 else 0.0

    @property
    def resource_precision(self) -> float:
        d = self.resource_tp + self.resource_fp
        return self.resource_tp / d if d else 1.0

    @property
    def resource_recall(self) -> float:
        d = self.resource_tp + self.resource_fn
        return self.resource_tp / d if d else 1.0

    @property
    def citation_agreement(self) -> float:
        return (self.citation_matches / self.total_cited_steps) if self.total_cited_steps > 0 else 1.0

    def to_markdown(self) -> str:
        lines = [
            "# Public Sample Evaluation Report",
            "",
            "> [!IMPORTANT]",
            "> **PUBLIC SAMPLE METRICS ONLY**: Evaluated exclusively against `dataset/sample_output.csv` (32 cases).",
            "> This benchmark provides regression tracking on documented sample cases and does **not** estimate",
            "> or guarantee performance on the 518 hidden evaluation cases.",
            "",
            "## Summary Metrics",
            "",
            "| Metric | Score | Detail |",
            "|---|---|---|",
            f"| **Status Accuracy** | **{self.status_accuracy:.2%}** | {self.status_matches} / {self.total_cases} cases |",
            f"| **Exact-Array Accuracy** | **{self.exact_array_accuracy:.2%}** | {self.exact_array_matches} / {self.total_cases} cases |",
            f"| **Step Precision** | **{self.step_precision:.2%}** | TP={self.tp_steps}, FP={self.fp_steps} |",
            f"| **Step Recall** | **{self.step_recall:.2%}** | TP={self.tp_steps}, FN={self.fn_steps} |",
            f"| **Step F1 Score** | **{self.step_f1:.2%}** | Harmonic mean of precision & recall |",
            f"| **Citation Agreement** | **{self.citation_agreement:.2%}** | {self.citation_matches} / {self.total_cited_steps} cited steps |",
            f"| **Sequence Order Errors** | **{self.cases_with_order_errors} cases** | {self.total_order_inversions} pairwise inversions |",
            f"| **Resource Precision** | **{self.resource_precision:.2%}** | distinct (system_id, resource_id): TP={self.resource_tp}, FP={self.resource_fp} |",
            f"| **Resource Recall** | **{self.resource_recall:.2%}** | FN={self.resource_fn} |",
            f"| **Loose-aligned operation accuracy** | **{(self.loose_operation_matches / self.loose_aligned if self.loose_aligned else 1.0):.2%}** | aligned on (phase, system_id, resource_id): {self.loose_operation_matches}/{self.loose_aligned} |",
            f"| **Loose-aligned evidence_id accuracy** | **{(self.loose_evidence_matches / self.loose_aligned if self.loose_aligned else 1.0):.2%}** | {self.loose_evidence_matches}/{self.loose_aligned} |",
            "",
            "## Field-Level Accuracy (on aligned steps)",
            "",
            f"Total Aligned Steps Evaluated: {self.field_stats.total_aligned_steps}",
            "",
            "| Field | Accuracy | Matches / Total |",
            "|---|---|---|",
            f"| `phase` | {self.field_stats.accuracy(self.field_stats.phase_matches):.2%} | {self.field_stats.phase_matches} / {self.field_stats.total_aligned_steps} |",
            f"| `operation` | {self.field_stats.accuracy(self.field_stats.operation_matches):.2%} | {self.field_stats.operation_matches} / {self.field_stats.total_aligned_steps} |",
            f"| `state` | {self.field_stats.accuracy(self.field_stats.state_matches):.2%} | {self.field_stats.state_matches} / {self.field_stats.total_aligned_steps} |",
            f"| `system_id` | {self.field_stats.accuracy(self.field_stats.system_id_matches):.2%} | {self.field_stats.system_id_matches} / {self.field_stats.total_aligned_steps} |",
            f"| `resource_id` | {self.field_stats.accuracy(self.field_stats.resource_id_matches):.2%} | {self.field_stats.resource_id_matches} / {self.field_stats.total_aligned_steps} |",
            f"| `evidence_id` | {self.field_stats.accuracy(self.field_stats.evidence_id_matches):.2%} | {self.field_stats.evidence_id_matches} / {self.field_stats.total_aligned_steps} |",
            "",
            "## Status Breakdown (Confusion Matrix)",
            "",
            "| Golden Status | Predicted COMPLETE | Predicted INCOMPLETE | Predicted NOT_APPLICABLE | Total |",
            "|---|---|---|---|---|",
        ]

        all_statuses = ["COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"]
        for g_status in all_statuses:
            row_dict = self.status_confusion.get(g_status, {})
            c_pred = row_dict.get("COMPLETE", 0)
            i_pred = row_dict.get("INCOMPLETE", 0)
            n_pred = row_dict.get("NOT_APPLICABLE", 0)
            tot = c_pred + i_pred + n_pred
            lines.append(f"| **{g_status}** | {c_pred} | {i_pred} | {n_pred} | {tot} |")

        return "\n".join(lines)


class SampleEvaluator:
    """Evaluator comparing prediction rows against sample_output.csv."""

    def __init__(self, golden_path: Optional[Path | str] = None):
        self.golden_path = self._resolve_golden_path(golden_path)
        self.golden_cases: Dict[str, Dict[str, Any]] = {}
        if self.golden_path and self.golden_path.is_file():
            self._load_golden_cases()

    def _resolve_golden_path(self, golden_path: Optional[Path | str]) -> Optional[Path]:
        if golden_path:
            p = Path(golden_path)
            return p if p.is_file() else None
        candidates = [
            Path(os.environ["GHOST_AUDIT_DATASET"]) / "sample_output.csv"
            if os.environ.get("GHOST_AUDIT_DATASET") else None,
            Path("dataset/sample_output.csv"),
            Path(__file__).resolve().parent.parent.parent / "dataset" / "sample_output.csv",
        ]
        candidates = [c for c in candidates if c is not None]
        for c in candidates:
            if c.is_file():
                return c
        return None

    def _load_golden_cases(self) -> None:
        assert self.golden_path is not None
        with open(self.golden_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cid = row["case_id"]
                try:
                    steps = json.loads(row["steps_json"])
                except Exception:
                    steps = []
                self.golden_cases[cid] = {
                    "deletion_status": row["deletion_status"],
                    "steps": steps,
                }

    def evaluate_file(self, prediction_path: Path | str) -> EvaluationReport:
        path = Path(prediction_path)
        if not path.is_file():
            raise FileNotFoundError(f"Prediction file does not exist: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return self.evaluate_content(f.read())

    def evaluate_content(self, prediction_csv: str) -> EvaluationReport:
        reader = csv.DictReader(io.StringIO(prediction_csv))
        predictions: Dict[str, Dict[str, Any]] = {}
        for row in reader:
            cid = row.get("case_id")
            if not cid:
                continue
            try:
                steps = json.loads(row.get("steps_json", "[]"))
            except Exception:
                steps = []
            predictions[cid] = {
                "deletion_status": row.get("deletion_status", ""),
                "steps": steps,
            }

        report = EvaluationReport()

        for cid, golden in self.golden_cases.items():
            report.total_cases += 1
            pred = predictions.get(cid)
            if not pred:
                # Missing case counted as FN
                g_steps = golden["steps"]
                report.fn_steps += len(g_steps)
                g_stat = golden["deletion_status"]
                report.status_confusion.setdefault(g_stat, {}).setdefault("MISSING", 0)
                report.status_confusion[g_stat]["MISSING"] += 1
                continue

            g_status = golden["deletion_status"]
            p_status = pred["deletion_status"]
            report.status_confusion.setdefault(g_status, {}).setdefault(p_status, 0)
            report.status_confusion[g_status][p_status] += 1

            status_match = (g_status == p_status)
            if status_match:
                report.status_matches += 1

            g_steps = golden["steps"]
            p_steps = pred["steps"]

            # Exact array match: status match AND steps identical in content and order
            exact_array_match = False
            if status_match and self._are_steps_identical(g_steps, p_steps):
                exact_array_match = True
                report.exact_array_matches += 1

            # Step-level and Field-level evaluation
            self._evaluate_case_steps(
                golden_steps=g_steps,
                pred_steps=p_steps,
                report=report,
            )

        return report

    def _are_steps_identical(self, steps1: List[Dict[str, Any]], steps2: List[Dict[str, Any]]) -> bool:
        if len(steps1) != len(steps2):
            return False
        for s1, s2 in zip(steps1, steps2):
            # Compare canonical fields
            for f in ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"):
                if str(s1.get(f, "")) != str(s2.get(f, "")):
                    return False
        return True

    def _step_key(self, step: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
        """Core semantic identity of a step."""
        return (
            str(step.get("phase", "")),
            str(step.get("system_id", "")),
            str(step.get("resource_id", "")),
            str(step.get("operation", "")),
            str(step.get("state", "")),
        )

    def _evaluate_case_steps(
        self,
        golden_steps: List[Dict[str, Any]],
        pred_steps: List[Dict[str, Any]],
        report: EvaluationReport,
    ) -> None:
        """Calculate step precision/recall, field accuracy, order errors, and citations."""
        golden_keys = [self._step_key(s) for s in golden_steps]
        pred_keys = [self._step_key(s) for s in pred_steps]

        matched_golden_indices: Set[int] = set()
        matched_pred_indices: Set[int] = set()
        aligned_pairs: List[Tuple[int, int]] = []

        # Greedy match on exact step semantic key
        for p_idx, p_key in enumerate(pred_keys):
            for g_idx, g_key in enumerate(golden_keys):
                if g_idx not in matched_golden_indices and p_key == g_key:
                    matched_golden_indices.add(g_idx)
                    matched_pred_indices.add(p_idx)
                    aligned_pairs.append((g_idx, p_idx))
                    break

        tp = len(aligned_pairs)
        fp = len(pred_steps) - tp
        fn = len(golden_steps) - tp

        report.tp_steps += tp
        report.fp_steps += fp
        report.fn_steps += fn

        # Field-level accuracy on aligned steps
        for g_idx, p_idx in aligned_pairs:
            gs = golden_steps[g_idx]
            ps = pred_steps[p_idx]

            report.field_stats.total_aligned_steps += 1
            if gs.get("phase") == ps.get("phase"):
                report.field_stats.phase_matches += 1
            if gs.get("operation") == ps.get("operation"):
                report.field_stats.operation_matches += 1
            if gs.get("state") == ps.get("state"):
                report.field_stats.state_matches += 1
            if gs.get("system_id") == ps.get("system_id"):
                report.field_stats.system_id_matches += 1
            if gs.get("resource_id") == ps.get("resource_id"):
                report.field_stats.resource_id_matches += 1
            if gs.get("evidence_id", "") == ps.get("evidence_id", ""):
                report.field_stats.evidence_id_matches += 1

            # Citation agreement for steps that require citations
            if gs.get("evidence_id"):
                report.total_cited_steps += 1
                if ps.get("evidence_id") == gs.get("evidence_id"):
                    report.citation_matches += 1

        # Supplementary: resource discovery and loose alignment on (phase, system, resource).
        g_res = {(s.get("system_id"), s.get("resource_id")) for s in golden_steps}
        p_res = {(s.get("system_id"), s.get("resource_id")) for s in pred_steps}
        report.resource_tp += len(g_res & p_res)
        report.resource_fp += len(p_res - g_res)
        report.resource_fn += len(g_res - p_res)
        g_loose = {(s.get("phase"), s.get("system_id"), s.get("resource_id")): s for s in golden_steps}
        for ps in pred_steps:
            gs = g_loose.get((ps.get("phase"), ps.get("system_id"), ps.get("resource_id")))
            if gs is None:
                continue
            report.loose_aligned += 1
            report.loose_operation_matches += int(gs.get("operation") == ps.get("operation"))
            report.loose_evidence_matches += int(gs.get("evidence_id", "") == ps.get("evidence_id", ""))

        # Order error check: if all steps match as a set, are they in same relative order?
        # Relative order is checked among all matched steps, including cases where some
        # steps are missing or extra (previously only full matches were checked, which
        # hid inversions in partially matching cases).
        if len(aligned_pairs) > 1:
            # Check for inversions
            inversions = 0
            for i in range(len(aligned_pairs)):
                for j in range(i + 1, len(aligned_pairs)):
                    g_i, p_i = aligned_pairs[i]
                    g_j, p_j = aligned_pairs[j]
                    if (g_i < g_j and p_i > p_j) or (g_i > g_j and p_i < p_j):
                        inversions += 1
            if inversions > 0:
                report.cases_with_order_errors += 1
                report.total_order_inversions += inversions

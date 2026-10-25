"""Unit tests for the Ghost Data Still Exists output contract validator.

Tests all failure modes, schema requirements, ordering constraints, evidence rules,
and disputed assumptions.
"""

import json
import sys
import os
import unittest
from pathlib import Path

# Add current directory to path to avoid conflict with standard library 'code' module
CURR_DIR = Path(__file__).resolve().parent
if str(CURR_DIR) not in sys.path:
    sys.path.insert(0, str(CURR_DIR))

try:
    from .validator import OutputValidator
except ImportError:
    from validator import OutputValidator


class TestOutputValidator(unittest.TestCase):
    """Test suite for OutputValidator."""

    def setUp(self):
        # Initialize validator with repo dataset
        # Repository-relative dataset location (override with GHOST_AUDIT_DATASET).
        self.dataset_dir = Path(os.environ.get("GHOST_AUDIT_DATASET")
                                or Path(__file__).resolve().parent.parent.parent / "dataset")
        required = ["sample_deletion_requests.csv", "sample_output.csv", "deletion_requests.csv",
                    "evidence_files.csv", "system_activity.csv", "user_audit_log.csv"]
        missing = [f for f in required if not (self.dataset_dir / f).is_file()]
        if missing:
            self.skipTest(f"participant dataset not available at {self.dataset_dir} (missing {missing}); "
                          "pass --dataset PATH or set GHOST_AUDIT_DATASET")
        # Load sample case IDs
        sample_file = self.dataset_dir / "sample_deletion_requests.csv"
        with open(sample_file, "r", encoding="utf-8") as f:
            import csv
            self.sample_case_ids = [r["case_id"] for r in csv.DictReader(f)]

        self.validator = OutputValidator(
            dataset_dir=self.dataset_dir,
            expected_case_ids=self.sample_case_ids,
        )

    def test_sample_output_is_valid(self):
        """dataset/sample_output.csv must pass contract validation with 0 errors."""
        sample_output_path = self.dataset_dir / "sample_output.csv"
        result = self.validator.validate_file(sample_output_path)
        self.assertTrue(result.is_valid, f"Validation failed with errors: {result.errors}")
        self.assertEqual(len(result.errors), 0)
        self.assertEqual(result.total_rows, 32)

    def test_malformed_csv_quoting(self):
        """Malformed CSV quoting must produce MALFORMED_CSV error."""
        bad_csv = 'case_id,deletion_status,steps_json\ncase_01,COMPLETE,"[{"unterminated_quote\n'
        result = self.validator.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        error_rules = [e.rule for e in result.errors]
        self.assertTrue(
            "MALFORMED_CSV" in error_rules or "INVALID_JSON" in error_rules,
            f"Expected parsing error, got {error_rules}",
        )

    def test_invalid_header(self):
        """Incorrect CSV header must produce INVALID_HEADER error."""
        bad_csv = "id,status,steps\ncase_01,NOT_APPLICABLE,[]\n"
        result = self.validator.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "INVALID_HEADER" for e in result.errors))

    def test_duplicate_case_ids(self):
        """Duplicate case_id must produce DUPLICATE_CASE_ID error."""
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            "case_01,NOT_APPLICABLE,[]\n"
            "case_01,NOT_APPLICABLE,[]\n"
        )
        result = self.validator.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "DUPLICATE_CASE_ID" for e in result.errors))

    def test_row_count_mismatch(self):
        """Mismatched row count must produce ROW_COUNT_MISMATCH error."""
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            "case_01,NOT_APPLICABLE,[]\n"
        )
        result = self.validator.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "ROW_COUNT_MISMATCH" for e in result.errors))

    def test_invalid_status_value(self):
        """Invalid deletion_status values must produce INVALID_STATUS error."""
        for invalid_stat in ("PARTIAL", "PARTIALLY_COMPLETE", "UNKNOWN", "complete"):
            bad_csv = (
                f"case_id,deletion_status,steps_json\n"
                f"case_01,{invalid_stat},[]\n"
            )
            v = OutputValidator(expected_case_ids=["case_01"])
            result = v.validate_content(bad_csv)
            self.assertFalse(result.is_valid)
            self.assertTrue(
                any(e.rule == "INVALID_STATUS" for e in result.errors),
                f"Expected INVALID_STATUS for '{invalid_stat}'",
            )

    def test_invalid_json_steps(self):
        """Unparsable JSON in steps_json must produce INVALID_JSON error."""
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            "case_01,COMPLETE,{not_json}\n"
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "INVALID_JSON" for e in result.errors))

    def test_steps_not_array(self):
        """steps_json serialized as a JSON object or scalar must produce STEPS_NOT_ARRAY."""
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            'case_01,COMPLETE,"{""step_no"": 1}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "STEPS_NOT_ARRAY" for e in result.errors))

    def test_not_applicable_nonempty_steps(self):
        """NOT_APPLICABLE with non-empty steps array must produce NOT_APPLICABLE_NONEMPTY_STEPS."""
        step = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "evidence_54",
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,NOT_APPLICABLE,"{json.dumps([step]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "NOT_APPLICABLE_NONEMPTY_STEPS" for e in result.errors))

    def test_step_field_mismatch_missing_field(self):
        """Missing required step field (e.g. state) must produce STEP_FIELD_MISMATCH."""
        step = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "VERIFY_RECORD_ABSENT",
            # missing "state"
            "evidence_id": "evidence_54",
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,COMPLETE,"{json.dumps([step]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "STEP_FIELD_MISMATCH" for e in result.errors))

    def test_step_field_mismatch_extra_field(self):
        """Extra step field must produce STEP_FIELD_MISMATCH."""
        step = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "evidence_54",
            "comment": "extra notes",
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,COMPLETE,"{json.dumps([step]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "STEP_FIELD_MISMATCH" for e in result.errors))

    def test_nonconsecutive_step_numbers(self):
        """Step numbers starting at 0 or skipping numbers must produce NONCONSECUTIVE_STEP_NO."""
        step1 = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "evidence_54",
        }
        step2 = dict(step1, step_no=3, resource_id="account_02")
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,COMPLETE,"{json.dumps([step1, step2]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "NONCONSECUTIVE_STEP_NO" for e in result.errors))

    def test_wrong_state_for_operation(self):
        """Operation with incorrect state must produce INVALID_STATE_FOR_OPERATION."""
        step = {
            "step_no": 1,
            "phase": "FINDING",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "REPORT_REMAINING_RECORD",
            "state": "ABSENT",  # Should be PRESENT
            "evidence_id": "evidence_54",
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,INCOMPLETE,"{json.dumps([step]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "INVALID_STATE_FOR_OPERATION" for e in result.errors))

    def test_phase_order_violation(self):
        """VERIFICATION before FINDING must produce STEP_ORDER_VIOLATION."""
        step1 = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "",
        }
        step2 = {
            "step_no": 2,
            "phase": "FINDING",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "REPORT_REMAINING_RECORD",
            "state": "PRESENT",
            "evidence_id": "evidence_54",
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,INCOMPLETE,"{json.dumps([step1, step2]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "STEP_ORDER_VIOLATION" for e in result.errors))

    def test_operation_order_violation_within_phase(self):
        """Within FINDING, REPORT_RESTORE_RISK before REPORT_REMAINING_RECORD violates order."""
        step1 = {
            "step_no": 1,
            "phase": "FINDING",
            "system_id": "backup_service",
            "resource_id": "backup_01",
            "operation": "REPORT_RESTORE_RISK",
            "state": "ACTIVE",
            "evidence_id": "evidence_93",
        }
        step2 = {
            "step_no": 2,
            "phase": "FINDING",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "REPORT_REMAINING_RECORD",
            "state": "PRESENT",
            "evidence_id": "evidence_54",
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,INCOMPLETE,"{json.dumps([step1, step2]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "STEP_ORDER_VIOLATION" for e in result.errors))

    def test_future_remediation_cannot_have_evidence_id(self):
        """REMEDIATION step on INCOMPLETE case must have empty evidence_id."""
        step1 = {
            "step_no": 1,
            "phase": "FINDING",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "REPORT_REMAINING_RECORD",
            "state": "PRESENT",
            "evidence_id": "evidence_54",
        }
        step2 = {
            "step_no": 2,
            "phase": "REMEDIATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "DELETE_RECORD",
            "state": "ABSENT",
            "evidence_id": "evidence_54",  # ERROR: Must be ""
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,INCOMPLETE,"{json.dumps([step1, step2]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        v = OutputValidator(expected_case_ids=["case_01"])
        result = v.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "FUTURE_STEP_HAS_EVIDENCE_CITATION" for e in result.errors))

    def test_unknown_evidence_id(self):
        """Evidence ID not found in files, activity, or audit logs must produce UNKNOWN_EVIDENCE_ID."""
        step = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "evidence_imaginary_9999",
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,COMPLETE,"{json.dumps([step]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        result = self.validator.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "UNKNOWN_EVIDENCE_ID" for e in result.errors))

    def test_future_citation(self):
        """Evidence timestamp after audit_at must produce FUTURE_CITATION error."""
        # For case_01, audit_at is 2026-09-03T11:30:00Z.
        # evidence_03 captured_at is 2026-10-01T12:00:00Z (which is AFTER audit_at).
        step = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "evidence_03",  # Captured in October, audit_at is September 3
        }
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,COMPLETE,"{json.dumps([step]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        result = self.validator.validate_content(bad_csv)
        self.assertFalse(result.is_valid)
        self.assertTrue(any(e.rule == "FUTURE_CITATION" for e in result.errors))

    def test_disputed_requested_at_bound_separated(self):
        """Evidence before requested_at is tracked as disputed finding by default, not hard error."""
        # Create an artificial case where requested_at is 2026-09-20, evidence_01 is 2026-09-15.
        v_default = OutputValidator(dataset_dir=self.dataset_dir, expected_case_ids=["case_01"])
        v_default.case_context["case_01"] = {
            "requested_at": "2026-09-20T00:00:00Z",
            "audit_at": "2026-09-30T00:00:00Z",
        }
        step = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_01",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "evidence_01",  # captured 2026-09-15
        }
        csv_text = (
            "case_id,deletion_status,steps_json\n"
            f'case_01,COMPLETE,"{json.dumps([step]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        # Default mode: should be valid with a disputed finding
        res_default = v_default.validate_content(csv_text)
        self.assertTrue(res_default.is_valid)
        self.assertTrue(any(df.category == "CITATION_BEFORE_REQUESTED_AT" for df in res_default.disputed_findings))

        # Enforced mode: should fail with CITATION_BEFORE_REQUESTED_AT error
        v_enforce = OutputValidator(
            dataset_dir=self.dataset_dir,
            expected_case_ids=["case_01"],
            enforce_requested_at_bound=True,
        )
        v_enforce.case_context["case_01"] = v_default.case_context["case_01"]
        res_enforce = v_enforce.validate_content(csv_text)
        self.assertFalse(res_enforce.is_valid)
        self.assertTrue(any(e.rule == "CITATION_BEFORE_REQUESTED_AT" for e in res_enforce.errors))


if __name__ == "__main__":
    unittest.main()

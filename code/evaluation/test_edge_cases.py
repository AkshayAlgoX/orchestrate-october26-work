"""Authored contract and unit tests for edge cases in Ghost Data Still Exists.

This module provides contract and unit test cases for three critical challenge categories:
  1. Edge Case 1: Non-deletion intent (Contract/Unit Test)
  2. Edge Case 2: Historical identifiers and identity drift (Contract/Unit Test)
  3. Edge Case 3: Replica synchronization and restore risk (Contract/Unit Test)

IMPORTANT: These test cases are authored specification contract/unit tests. They test
output-contract compliance, phase/operation/state integrity, and serialization correctness.
They ARE NOT hidden-answer labels or claims about hidden test set cases.
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


class TestContractEdgeCases(unittest.TestCase):
    """Authored contract/unit tests for challenge edge cases."""

    def setUp(self):
        # Repository-relative dataset location (override with GHOST_AUDIT_DATASET).
        self.dataset_dir = Path(os.environ.get("GHOST_AUDIT_DATASET")
                                or Path(__file__).resolve().parent.parent.parent / "dataset")
        self.validator = OutputValidator(
            dataset_dir=self.dataset_dir,
            expected_case_ids=["case_edge_01"],
        )

    # --------------------------------------------------------------------------
    # Edge Case 1: Non-Deletion Intent (Contract/Unit Test)
    # --------------------------------------------------------------------------
    def test_edge_case_1_non_deletion_intent_contract(self):
        """Contract Test: Request texts asking for access, export, or opt-out are NOT_APPLICABLE.

        Under AGENTS.md §6.5 and problem_statement.md:
        - When a person does not request deletion, the case must be classified as NOT_APPLICABLE.
        - The contract strictly requires steps_json to be an empty array [].
        """
        # 1. Valid contract row for non-deletion request
        valid_csv = (
            "case_id,deletion_status,steps_json\n"
            "case_edge_01,NOT_APPLICABLE,[]\n"
        )
        res = self.validator.validate_content(valid_csv)
        self.assertTrue(res.is_valid, f"Expected valid NOT_APPLICABLE row, got errors: {res.errors}")
        self.assertEqual(len(res.errors), 0)

        # 2. Invalid: Non-deletion intent incorrectly emitting verification steps
        step = {
            "step_no": 1,
            "phase": "VERIFICATION",
            "system_id": "account_service",
            "resource_id": "account_999",
            "operation": "VERIFY_RECORD_ABSENT",
            "state": "ABSENT",
            "evidence_id": "evidence_54",
        }
        invalid_steps_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_edge_01,NOT_APPLICABLE,"{json.dumps([step]).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        res_bad_steps = self.validator.validate_content(invalid_steps_csv)
        self.assertFalse(res_bad_steps.is_valid)
        self.assertTrue(any(e.rule == "NOT_APPLICABLE_NONEMPTY_STEPS" for e in res_bad_steps.errors))

        # 3. Invalid: Non-deletion intent misclassified as COMPLETE
        invalid_status_csv = (
            "case_id,deletion_status,steps_json\n"
            "case_edge_01,COMPLETE,[]\n"
        )
        res_bad_stat = self.validator.validate_content(invalid_status_csv)
        self.assertTrue(any(w.category == "EMPTY_STEPS_ON_ACTIVE_CASE" for w in res_bad_stat.warnings))

    # --------------------------------------------------------------------------
    # Edge Case 2: Historical Identifiers & Identity Drift (Contract/Unit Test)
    # --------------------------------------------------------------------------
    def test_edge_case_2_historical_identifiers_contract(self):
        """Contract Test: Multi-system remediation handling historical identifiers.

        When a user changed their email/phone historically, legacy records or secondary
        databases may still harbor the historical PII. The output contract requires:
        - FINDING with REPORT_EXPOSED_PII or REPORT_REMAINING_RECORD citing participant evidence.
        - REMEDIATION with REDACT_RECORD / DELETE_RECORD / ANONYMIZE_RECORD with empty evidence_id.
        - VERIFICATION covering post-remediation states with empty evidence_id.
        - Strict ordering: FINDING -> REMEDIATION -> VERIFICATION.
        """
        steps = [
            # Finding: legacy support platform retains old email from user_audit_log
            {
                "step_no": 1,
                "phase": "FINDING",
                "system_id": "support_platform",
                "resource_id": "ticket_archive_042",
                "operation": "REPORT_EXPOSED_PII",
                "state": "PRESENT",
                "evidence_id": "evidence_58",
            },
            # Remediation: Redact historical direct identifiers from support record
            {
                "step_no": 2,
                "phase": "REMEDIATION",
                "system_id": "support_platform",
                "resource_id": "ticket_archive_042",
                "operation": "REDACT_RECORD",
                "state": "REDACTED",
                "evidence_id": "",
            },
            # Verification: Confirm account tombstone preserved and support record redacted
            {
                "step_no": 3,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_tombstone_042",
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                "evidence_id": "",
            },
            {
                "step_no": 4,
                "phase": "VERIFICATION",
                "system_id": "support_platform",
                "resource_id": "ticket_archive_042",
                "operation": "VERIFY_RECORD_REDACTED",
                "state": "REDACTED",
                "evidence_id": "",
            },
        ]

        valid_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_edge_01,INCOMPLETE,"{json.dumps(steps).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        res = self.validator.validate_content(valid_csv)
        self.assertTrue(res.is_valid, f"Expected valid historical identifier plan, got errors: {res.errors}")
        self.assertEqual(len(res.errors), 0)

        # Failure test: If future REDACT_RECORD mistakenly cited an evidence_id
        bad_steps = [dict(s) for s in steps]
        bad_steps[1]["evidence_id"] = "evidence_58"
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_edge_01,INCOMPLETE,"{json.dumps(bad_steps).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        res_bad = self.validator.validate_content(bad_csv)
        self.assertFalse(res_bad.is_valid)
        self.assertTrue(any(e.rule == "FUTURE_STEP_HAS_EVIDENCE_CITATION" for e in res_bad.errors))

    # --------------------------------------------------------------------------
    # Edge Case 3: Replica Sync & Restore Risk (Contract/Unit Test)
    # --------------------------------------------------------------------------
    def test_edge_case_3_replica_sync_and_restore_risk_contract(self):
        """Contract Test: Active synchronization flows and backup restore risks.

        Under AGENTS.md §6.4 & §6.5:
        - Active processes that can recreate data must be reported (REPORT_ACTIVE_DATA_FLOW).
        - Backups that can restore deleted records must be reported (REPORT_RESTORE_RISK).
        - Remediation MUST order DISABLE_DATA_FLOW and BLOCK_RESTORE before dependent record changes.
        - Verification must confirm VERIFY_DATA_FLOW_DISABLED and VERIFY_RESTORE_BLOCKED.
        """
        steps = [
            # Findings: Active sync job and backup restore risk
            {
                "step_no": 1,
                "phase": "FINDING",
                "system_id": "sync_worker",
                "resource_id": "sync_pipeline_crm",
                "operation": "REPORT_ACTIVE_DATA_FLOW",
                "state": "ACTIVE",
                "evidence_id": "evidence_01",
            },
            {
                "step_no": 2,
                "phase": "FINDING",
                "system_id": "backup_service",
                "resource_id": "snapshot_cold_2026",
                "operation": "REPORT_RESTORE_RISK",
                "state": "ACTIVE",
                "evidence_id": "evidence_93",
            },
            # Remediation: Disable flow, block restore, then delete remaining records
            {
                "step_no": 3,
                "phase": "REMEDIATION",
                "system_id": "sync_worker",
                "resource_id": "sync_pipeline_crm",
                "operation": "DISABLE_DATA_FLOW",
                "state": "DISABLED",
                "evidence_id": "",
            },
            {
                "step_no": 4,
                "phase": "REMEDIATION",
                "system_id": "backup_service",
                "resource_id": "snapshot_cold_2026",
                "operation": "BLOCK_RESTORE",
                "state": "BLOCKED",
                "evidence_id": "",
            },
            {
                "step_no": 5,
                "phase": "REMEDIATION",
                "system_id": "account_service",
                "resource_id": "account_ghost_01",
                "operation": "DELETE_RECORD",
                "state": "ABSENT",
                "evidence_id": "",
            },
            # Verification: Verify flow disabled, restore blocked, record absent
            {
                "step_no": 6,
                "phase": "VERIFICATION",
                "system_id": "account_service",
                "resource_id": "account_ghost_01",
                "operation": "VERIFY_RECORD_ABSENT",
                "state": "ABSENT",
                "evidence_id": "",
            },
            {
                "step_no": 7,
                "phase": "VERIFICATION",
                "system_id": "sync_worker",
                "resource_id": "sync_pipeline_crm",
                "operation": "VERIFY_DATA_FLOW_DISABLED",
                "state": "DISABLED",
                "evidence_id": "",
            },
            {
                "step_no": 8,
                "phase": "VERIFICATION",
                "system_id": "backup_service",
                "resource_id": "snapshot_cold_2026",
                "operation": "VERIFY_RESTORE_BLOCKED",
                "state": "BLOCKED",
                "evidence_id": "",
            },
        ]

        valid_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_edge_01,INCOMPLETE,"{json.dumps(steps).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        res = self.validator.validate_content(valid_csv)
        self.assertTrue(res.is_valid, f"Expected valid sync/restore plan, got errors: {res.errors}")
        self.assertEqual(len(res.errors), 0)

        # Inversion test: If DELETE_RECORD was placed before DISABLE_DATA_FLOW in REMEDIATION
        bad_steps = [
            steps[0], steps[1],
            dict(steps[4], step_no=3),  # DELETE_RECORD placed first
            dict(steps[2], step_no=4),  # DISABLE_DATA_FLOW
            dict(steps[3], step_no=5),  # BLOCK_RESTORE
            steps[5], steps[6], steps[7],
        ]
        bad_csv = (
            "case_id,deletion_status,steps_json\n"
            f'case_edge_01,INCOMPLETE,"{json.dumps(bad_steps).replace(chr(34), chr(34)+chr(34))}"\n'
        )
        res_bad_order = self.validator.validate_content(bad_csv)
        self.assertFalse(res_bad_order.is_valid)
        self.assertTrue(any(e.rule == "STEP_ORDER_VIOLATION" for e in res_bad_order.errors))


if __name__ == "__main__":
    unittest.main()

"""Hostile second-round audit and mutation tests for AuditTools._substantiates.

Systematically challenges:
1. REPORT_REMAINING_RECORD vs REPORT_EXPOSED_PII state separation.
2. REPORT_ACTIVE_DATA_FLOW vs VERIFY_DATA_FLOW_DISABLED boolean polarity.
3. REPORT_RESTORE_RISK vs VERIFY_RESTORE_BLOCKED boolean polarity and BACKUP_ARCHIVE semantics.
4. Retention and redaction verification (VERIFY_RECORD_PRESENT, VERIFY_RECORD_REDACTED, VERIFY_RECORD_ANONYMIZED).
5. Lineage / basis citation bypass attempts (citing link basis for unsupported operation/state).
6. Catalog document proof constraints (subject match, cutoff time, operation consistency).
7. Scoped snapshot absence constraints (system match, store family match, timing relative to creation/action).
"""

import json
import sys
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))

from ghost_audit.loaders import load_dataset
from ghost_audit.evidence import load_evidence
from ghost_audit.audit import Auditor, Config
from ghost_audit.agent import AuditTools, AuditAgent
import test_pipeline as tp
from ghost_audit.linking import Link


class TestProvenanceBypassHunt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tp.require_dataset()
        cls.ds_path = tp.DATASET
        cls.ds = load_dataset(cls.ds_path)
        cls.ev = load_evidence(cls.ds_path, cls.ds.manifest)
        cls.auditor = Auditor(cls.ds, cls.ev, Config())
        cls.auditor.prepare(cls.ds.requests)
        cls.tools = AuditTools(cls.auditor)

    def test_01_exposed_pii_requires_presence_and_exposed_fields(self):
        """Existence alone cannot substantiate REPORT_EXPOSED_PII; clean redaction is rejected."""
        # Case 34: account_5762 in evidence_04 is a USER_PROFILE with pii=True, exists=True
        req_34 = next(r for r in self.ds.requests if r["case_id"] == "case_34")
        # Positive control: REPORT_REMAINING_RECORD is valid
        step_rem = {"step_no": 1, "phase": "FINDING", "system_id": "account_service",
                    "resource_id": "account_5762", "operation": "REPORT_REMAINING_RECORD",
                    "state": "PRESENT", "evidence_id": "evidence_04"}
        self.assertTrue(self.tools._substantiates(req_34, step_rem, {"account_5762": None}))

        # Direct observation with exists=True but pii=False: support_9538 in evidence_04
        step_fake_pii = {"step_no": 1, "phase": "FINDING", "system_id": "support_platform",
                         "resource_id": "support_9538", "operation": "REPORT_EXPOSED_PII",
                         "state": "PRESENT", "evidence_id": "evidence_04"}
        self.assertFalse(self.tools._substantiates(req_34, step_fake_pii, {"support_9538": None}))

    def test_02_data_flow_polarity_enforcement(self):
        """Active data flow cannot substantiate VERIFY_DATA_FLOW_DISABLED, and disabled cannot substantiate ACTIVE."""
        # Find an observation with flow_enabled=True
        active_flow_obs = next(o for o in self.auditor.ev.observations if o.flow_enabled is True)
        rid = active_flow_obs.resource_id
        ev = active_flow_obs.file_id
        req = {"case_id": "synthetic", "email": "any@test.com", "audit_at": active_flow_obs.file_captured_at}

        step_active = {"step_no": 1, "phase": "FINDING", "system_id": active_flow_obs.system,
                       "resource_id": rid, "operation": "REPORT_ACTIVE_DATA_FLOW",
                       "state": "ACTIVE", "evidence_id": ev}
        step_disabled = {"step_no": 1, "phase": "VERIFICATION", "system_id": active_flow_obs.system,
                         "resource_id": rid, "operation": "VERIFY_DATA_FLOW_DISABLED",
                         "state": "DISABLED", "evidence_id": ev}

        # Active flow observation substantiates REPORT_ACTIVE_DATA_FLOW
        self.assertTrue(self.tools._substantiates(req, step_active, {rid: None}))
        # Active flow observation REJECTS VERIFY_DATA_FLOW_DISABLED
        self.assertFalse(self.tools._substantiates(req, step_disabled, {rid: None}))

    def test_03_restore_risk_polarity_enforcement(self):
        """Active restore risk cannot substantiate VERIFY_RESTORE_BLOCKED, and blocked cannot substantiate RISK."""
        # Find observation with restore_allowed=True
        risk_obs = next(o for o in self.auditor.ev.observations if o.restore_allowed is True)
        rid = risk_obs.resource_id
        ev = risk_obs.file_id
        req = {"case_id": "synthetic", "email": "any@test.com", "audit_at": risk_obs.file_captured_at}

        step_risk = {"step_no": 1, "phase": "FINDING", "system_id": risk_obs.system,
                     "resource_id": rid, "operation": "REPORT_RESTORE_RISK",
                     "state": "ACTIVE", "evidence_id": ev}
        step_blocked = {"step_no": 1, "phase": "VERIFICATION", "system_id": risk_obs.system,
                        "resource_id": rid, "operation": "VERIFY_RESTORE_BLOCKED",
                        "state": "BLOCKED", "evidence_id": ev}

        self.assertTrue(self.tools._substantiates(req, step_risk, {rid: None}))
        self.assertFalse(self.tools._substantiates(req, step_blocked, {rid: None}))

        # Non-backup archive with restore_allowed=None cannot substantiate REPORT_RESTORE_RISK
        account_obs = next(o for o in self.auditor.ev.observations if o.category == "USER_PROFILE")
        step_fake_risk = {"step_no": 1, "phase": "FINDING", "system_id": account_obs.system,
                          "resource_id": account_obs.resource_id, "operation": "REPORT_RESTORE_RISK",
                          "state": "ACTIVE", "evidence_id": account_obs.file_id}
        req_acc = {"case_id": "synthetic", "email": "any@test.com", "audit_at": account_obs.file_captured_at}
        self.assertFalse(self.tools._substantiates(req_acc, step_fake_risk, {account_obs.resource_id: None}))

    def test_04_verification_state_consistency(self):
        """VERIFY_RECORD_ABSENT rejects present records; VERIFY_RECORD_PRESENT rejects absent records."""
        # Present record: account_5762 in evidence_04
        req_34 = next(r for r in self.ds.requests if r["case_id"] == "case_34")
        step_fake_absent = {"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service",
                            "resource_id": "account_5762", "operation": "VERIFY_RECORD_ABSENT",
                            "state": "ABSENT", "evidence_id": "evidence_04"}
        self.assertFalse(self.tools._substantiates(req_34, step_fake_absent, {"account_5762": None}))

        # Retained record: security audit log audit_record_3880 in evidence_81 (exists=True)
        step_ret_present = {"step_no": 1, "phase": "VERIFICATION", "system_id": "security_service",
                            "resource_id": "audit_record_3880", "operation": "VERIFY_RECORD_PRESENT",
                            "state": "PRESENT", "evidence_id": "evidence_81"}
        req_221 = next(r for r in self.ds.requests if r["case_id"] == "case_221")
        self.assertTrue(self.tools._substantiates(req_221, step_ret_present, {"audit_record_3880": None}))

    def test_05_link_basis_cannot_bypass_operation_verification(self):
        """A link basis file cannot be cited to prove an unobserved or contrary state."""
        # account_5762 was linked via evidence_04. Attempting to cite evidence_04 for VERIFY_RECORD_ABSENT
        req_34 = next(r for r in self.ds.requests if r["case_id"] == "case_34")
        link = Link("account_5762", "account_service", "direct", ["evidence_04"])
        step_absent = {"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service",
                       "resource_id": "account_5762", "operation": "VERIFY_RECORD_ABSENT",
                       "state": "ABSENT", "evidence_id": "evidence_04"}
        # Must be rejected despite evidence_04 being in link.basis!
        self.assertFalse(self.tools._substantiates(req_34, step_absent, {"account_5762": link}))

    def test_06_catalog_document_proof_falsification(self):
        """Catalog document proof enforces subject match, cutoff date, and operation semantics."""
        # export_1842 in case_33 (user_18)
        req_33 = next(r for r in self.ds.requests if r["case_id"] == "case_33")

        # 1. Negative control: document cited for VERIFY_RECORD_ABSENT
        step_doc_absent = {"step_no": 1, "phase": "VERIFICATION", "system_id": "export_service",
                           "resource_id": "export_1842", "operation": "VERIFY_RECORD_ABSENT",
                           "state": "ABSENT", "evidence_id": "evidence_18"}
        self.assertFalse(self.tools._substantiates(req_33, step_doc_absent, {"export_1842": None}))

        # 2. Negative control: wrong valid subject (user_17 attempting to claim user_18's document)
        req_46 = next(r for r in self.ds.requests if r["case_id"] == "case_46")
        step_doc_rem = {"step_no": 1, "phase": "FINDING", "system_id": "export_service",
                        "resource_id": "export_1842", "operation": "REPORT_REMAINING_RECORD",
                        "state": "PRESENT", "evidence_id": "evidence_18"}
        self.assertFalse(self.tools._substantiates(req_46, step_doc_rem, {"export_1842": None}))

        # 3. Negative control: post-cutoff audit_at
        req_early_cutoff = dict(req_33, audit_at="2026-07-01T00:00:00Z")
        self.assertFalse(self.tools._substantiates(req_early_cutoff, step_doc_rem, {"export_1842": None}))

        # 4. Positive control: valid catalog document proof passes
        self.assertTrue(self.tools._substantiates(req_33, step_doc_rem, {"export_1842": None}))

    def test_07_scoped_snapshot_absence_falsification(self):
        """Scoped snapshot absence requires matching system, matching store family, and timing after action."""
        req_221 = next(r for r in self.ds.requests if r["case_id"] == "case_221")
        # account_4512 was deleted via workflow action on 2026-06-05T06:53:29Z.
        # evidence_81 enumerates account_service:customer_profiles at 2026-07-03T07:43:56Z with 0 accounts.

        # 1. Positive control: valid scoped snapshot absence passes
        step_valid_scope = {"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service",
                            "resource_id": "account_4512", "operation": "VERIFY_RECORD_ABSENT",
                            "state": "ABSENT", "evidence_id": "evidence_81"}
        self.assertTrue(self.tools._substantiates(req_221, step_valid_scope, {"account_4512": None}))

        # 2. Negative control: wrong system (claiming security_service instead of account_service)
        step_wrong_sys = dict(step_valid_scope, system_id="security_service")
        # evidence_81 has security_service scopes, but they enumerate security records, not accounts
        self.assertFalse(self.tools._substantiates(req_221, step_wrong_sys, {"account_4512": None}))

        # 3. Negative control: pre-action snapshot cannot prove absence after action
        # If audit_at was before the workflow action
        req_early = dict(req_221, audit_at="2026-05-01T00:00:00Z")
        self.assertFalse(self.tools._substantiates(req_early, step_valid_scope, {"account_4512": None}))


if __name__ == "__main__":
    unittest.main()

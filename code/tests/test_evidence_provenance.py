"""Hostile provenance tests for evidence citations in check_admissibility.

Run: python3 code/tests/test_evidence_provenance.py [--dataset /path/to/dataset]
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_pipeline as tp
from ghost_audit.agent import AuditTools
from ghost_audit.audit import Auditor, Config
from ghost_audit.evidence import load_evidence
from ghost_audit.loaders import load_dataset


class TestCitationProvenance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tp.require_dataset()
        cls.ds = load_dataset(tp.DATASET)
        cls.ev = load_evidence(tp.DATASET, cls.ds.manifest)
        cls.auditor = Auditor(cls.ds, cls.ev, Config())
        cls.auditor.prepare(cls.ds.requests)
        cls.tools = AuditTools(cls.auditor)
        cls.req_33 = next(r for r in cls.ds.requests if r["case_id"] == "case_33")
        cls.req_34 = next(r for r in cls.ds.requests if r["case_id"] == "case_34")

    # -- Reproduction of Codex defect & Negative Controls ------------------------

    def test_reproduction_unrelated_file_citation_is_rejected(self):
        """Codex defect: evidence_20 cited for export_1842 in case_33 must be rejected."""
        steps = [
            {"step_no": 1, "phase": "FINDING", "system_id": "export_service", "resource_id": "export_1842",
             "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_20"}
        ]
        violations = self.tools.check_admissibility(self.req_33, json.dumps(steps), {"export_1842"})
        self.assertEqual(len(violations), 1)
        self.assertIn("does not substantiate", violations[0])
        self.assertIn("evidence_20", violations[0])

    def test_negative_control_file_containing_resource_but_wrong_state_rejected(self):
        """Negative control 2: File containing resource but not asserted state (e.g. absent record claimed present)."""
        # Find a resource that has a verified absent observation before audit_at
        absent_obs = [o for o in self.auditor.ev.observations
                      if o.exists is False and o.file_captured_at <= self.req_33["audit_at"]]
        self.assertTrue(absent_obs, "Must have absent observations")
        ob = absent_obs[0]
        steps = [
            {"step_no": 1, "phase": "FINDING", "system_id": ob.system, "resource_id": ob.resource_id,
             "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": ob.file_id}
        ]
        # In this file, ob.exists is False, so REPORT_REMAINING_RECORD (PRESENT) must be rejected
        violations = self.tools.check_admissibility(self.req_33, json.dumps(steps), {ob.resource_id})
        self.assertTrue(any("does not substantiate" in v for v in violations))

    def test_negative_control_correct_resource_wrong_subject_rejected(self):
        """Negative control 3: Resource cited in request for a different subject."""
        # export_1842 belongs to case_33 (user_18); attempt to cite it in case_34 (user_21)
        steps = [
            {"step_no": 1, "phase": "FINDING", "system_id": "export_service", "resource_id": "export_1842",
             "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_18"}
        ]
        # export_1842 is not linked to case_34's subject
        violations = self.tools.check_admissibility(self.req_34, json.dumps(steps), {"account_5762", "support_9538"})
        self.assertTrue(any("not linked to the subject" in v for v in violations))

    def test_negative_control_post_cutoff_citation_rejected(self):
        """Negative control 4: Citation captured after audit_at."""
        # In case_33, audit_at is 2026-07-19T13:00:00Z. Find a file captured after this date.
        post_files = [m for m in self.ev.files.values() if m.captured_at > self.req_33["audit_at"]]
        self.assertTrue(post_files)
        post_ev = post_files[0].file_id
        steps = [
            {"step_no": 1, "phase": "FINDING", "system_id": "export_service", "resource_id": "export_1842",
             "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": post_ev}
        ]
        violations = self.tools.check_admissibility(self.req_33, json.dumps(steps), {"export_1842"})
        self.assertTrue(any("after audit_at" in v for v in violations))

    def test_negative_control_forged_evidence_id_rejected(self):
        """Negative control 5: Completely forged evidence ID."""
        steps = [
            {"step_no": 1, "phase": "FINDING", "system_id": "export_service", "resource_id": "export_1842",
             "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_9999_forged"}
        ]
        violations = self.tools.check_admissibility(self.req_33, json.dumps(steps), {"export_1842"})
        self.assertTrue(any("unknown evidence id" in v for v in violations))

    # -- Positive Controls -------------------------------------------------------

    def test_positive_control_genuine_catalog_document_accepted(self):
        """Positive control: evidence_18 for export_1842 in case_33 passes with 0 violations."""
        steps = [
            {"step_no": 1, "phase": "FINDING", "system_id": "export_service", "resource_id": "export_1842",
             "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_18"},
            {"step_no": 2, "phase": "REMEDIATION", "system_id": "export_service", "resource_id": "export_1842",
             "operation": "DELETE_RECORD", "state": "ABSENT", "evidence_id": ""},
            {"step_no": 3, "phase": "VERIFICATION", "system_id": "export_service", "resource_id": "export_1842",
             "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": ""}
        ]
        violations = self.tools.check_admissibility(self.req_33, json.dumps(steps), {"export_1842"})
        self.assertEqual(violations, [])

    def test_positive_control_direct_observation_accepted(self):
        """Positive control: Direct observation of account_5762 in case_34 passes."""
        ref_34 = self.auditor.audit(self.req_34)
        violations = self.tools.check_admissibility(self.req_34, ref_34.steps_json, set(ref_34.trace["links"]))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    argv = list(sys.argv)
    if "--dataset" in argv:
        i = argv.index("--dataset")
        tp.DATASET = Path(argv[i + 1]).resolve()
        os.environ["GHOST_AUDIT_DATASET"] = str(tp.DATASET)
        del argv[i:i + 2]
    unittest.main(verbosity=2, argv=argv)

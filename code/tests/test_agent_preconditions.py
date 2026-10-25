"""Comprehensive regression, contract, and positive control tests for AuditAgent semantic precondition guards.

Verifies:
1. Every confirmed red-team semantic weakness is caught and rejected before execution:
   - Non-deletion requests cannot execute resolve_identity even if intent_reason is populated.
   - Mismatched subjects in discovery tasks are rejected.
   - Catalog claims not matching the subject's identifiers under catalog policy are rejected.
   - Forged references not explicitly named by the linked source record are rejected.
   - Absent, stale, or mismatched workflow contexts are rejected (and auditor=None fails closed).
2. Spies and mocks prove that rejected calls never invoke the underlying tool runner.
3. Positive controls prove legitimate calls satisfy all guards and execute normally.
4. Investigation fallback handles precondition failures safely and deterministically.
"""

from unittest import mock
import unittest
from pathlib import Path

import os
import sys
CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))

from ghost_audit.agent import (
    AuditAgent, AuditTools, Investigation, Task, TOOLS, ToolContractError, ToolFailure,
    check_preconditions,
    PRECOND_AUTHORIZATION, PRECOND_SUBJECT_RESOLVED, PRECOND_CATALOG_CLAIMS,
    PRECOND_CONTENT_LINKED_SOURCE, PRECOND_WORKFLOW_CONTEXT, PRECOND_LINKED_RESOURCE,
    PRECOND_STATES_RESOLVED, PRECOND_COMPILED_OUTPUT, PRECONDITION_GUARDS
)
from ghost_audit.audit import Auditor, Config
from ghost_audit.evidence import load_evidence
from ghost_audit.linking import Link
from ghost_audit.loaders import load_dataset
from ghost_audit.planner import ResourceState

ROOT = Path(__file__).resolve().parent.parent.parent


class TestPreconditionGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dataset_dir = Path(os.environ.get("GHOST_AUDIT_DATASET") or (ROOT / "dataset"))
        if not (dataset_dir / "evidence_files.csv").is_file():
            raise unittest.SkipTest("Dataset not available")
        cls.ds = load_dataset(dataset_dir)
        cls.ev = load_evidence(dataset_dir, cls.ds.manifest)
        cls.auditor = Auditor(cls.ds, cls.ev, Config())
        cls.auditor.prepare(cls.ds.requests)
        cls.agent = AuditAgent(cls.auditor)

    def setUp(self):
        self.req = self.ds.requests[0]

    def test_precondition_registry_integrity(self):
        """All non-entry tools declare a typed precondition identifier with a registered guard."""
        self.assertIsNone(TOOLS["classify_intent"].precondition_id)
        for name, spec in TOOLS.items():
            if name == "classify_intent":
                continue
            self.assertIsNotNone(spec.precondition_id, f"Tool {name} missing precondition_id")
            self.assertIn(spec.precondition_id, PRECONDITION_GUARDS,
                          f"Tool {name} precondition {spec.precondition_id} not registered")

    # -- Regression 1: Authorization before resolve_identity ------------------------
    def test_regression_non_deletion_request_rejects_resolve_identity(self):
        """A non-deletion request must not execute resolve_identity, even if intent_reason is populated."""
        inv = Investigation(self.req)
        inv.intent_reason = "Subject requested access/export, not deletion"
        inv.authorized = False  # Explicit non-deletion state

        task = Task(1, 0, "", 1, "resolve_identity", "test", {"email": self.req["email"]})

        with mock.patch.object(self.agent.tools, "resolve_identity", wraps=self.agent.tools.resolve_identity) as spy:
            with self.assertRaises(ToolFailure) as ctx:
                self.agent._execute(inv, task)
            self.assertIn("does not authorize deletion", str(ctx.exception))
            spy.assert_not_called()

    def test_positive_authorized_request_allows_resolve_identity(self):
        """Positive control: legitimate authorized request satisfies authorization guard."""
        inv = Investigation(self.req)
        inv.intent_reason = "deletion authorized"
        inv.authorized = True
        task = Task(1, 0, "", 1, "resolve_identity", "test", {"email": self.req["email"]})

        with mock.patch.object(self.agent.tools, "resolve_identity", wraps=self.agent.tools.resolve_identity) as spy:
            res, ran = self.agent._execute(inv, task)
            self.assertTrue(ran)
            self.assertIsNotNone(res)
            spy.assert_called_once()

    # -- Regression 2: Subject matching in discovery tasks -------------------------
    def test_regression_subject_mismatch_rejects_search_direct_evidence(self):
        """search_direct_evidence must reject tasks whose subject differs from inv.subject."""
        inv = Investigation(self.req)
        inv.intent_reason = "deletion requested"
        inv.authorized = True
        uid, ident = self.agent.tools.resolve_identity(self.req["email"])
        inv.subject, inv.ident = uid, ident

        mismatched_uid = "different_user_99999"
        task = Task(2, 0, "", 1, "search_direct_evidence", "test",
                    {"subject": mismatched_uid, "window": ["2026-01-01T00:00:00Z", self.req["audit_at"]]})

        with mock.patch.object(self.agent.tools, "search_direct_evidence", wraps=self.agent.tools.search_direct_evidence) as spy:
            with self.assertRaises(ToolFailure) as ctx:
                self.agent._execute(inv, task)
            self.assertIn("does not match investigation subject", str(ctx.exception))
            spy.assert_not_called()

    def test_positive_matching_subject_allows_search_direct_evidence(self):
        """Positive control: matching subject satisfies search_direct_evidence guard."""
        inv = Investigation(self.req)
        inv.intent_reason = "deletion requested"
        inv.authorized = True
        uid, ident = self.agent.tools.resolve_identity(self.req["email"])
        inv.subject, inv.ident = uid, ident

        task = Task(2, 0, "", 1, "search_direct_evidence", "test",
                    {"subject": uid, "window": ["2026-01-01T00:00:00Z", self.req["audit_at"]]})

        with mock.patch.object(self.agent.tools, "search_direct_evidence", wraps=self.agent.tools.search_direct_evidence) as spy:
            res, ran = self.agent._execute(inv, task)
            self.assertTrue(ran)
            self.assertIsNotNone(res)
            spy.assert_called_once()

    # -- Regression 3: Catalog claim matching under catalog policy -----------------
    def test_regression_unmatching_catalog_claims_rejected(self):
        """A nonempty catalog claim list must be rejected unless at least one claim matches the subject."""
        inv = Investigation(self.req)
        inv.intent_reason = "deletion requested"
        inv.authorized = True
        uid, ident = self.agent.tools.resolve_identity(self.req["email"])
        inv.subject, inv.ident = uid, ident

        # Forged claims that do not name this subject
        forged_claims = ["act_unrelated_claim_1", "act_unrelated_claim_2"]
        task = Task(2, 0, "", 1, "query_catalog", "test",
                    {"subject": uid, "claims": forged_claims})

        with mock.patch.object(self.agent.tools, "query_catalog", wraps=self.agent.tools.query_catalog) as spy:
            with self.assertRaises(ToolFailure) as ctx:
                self.agent._execute(inv, task)
            self.assertIn("no claim in task matches", str(ctx.exception))
            spy.assert_not_called()

    def test_positive_matching_catalog_claims_allowed(self):
        """Positive control: genuine catalog claims naming the subject satisfy the guard."""
        # Find a case in the dataset that actually has catalog claims naming the subject
        matched_case = None
        for r in self.ds.requests:
            uid, ident = self.agent.tools.resolve_identity(r["email"])
            if uid and ident:
                naming = self.agent.tools.catalog_claims_naming(ident)
                if naming:
                    matched_case = (r, uid, ident, [self.auditor.claims[i].activity_id for i in naming])
                    break

        if matched_case is None:
            self.skipTest("No request with catalog claims in dataset")

        req, uid, ident, claims = matched_case
        inv = Investigation(req)
        inv.intent_reason = "deletion requested"
        inv.authorized = True
        inv.subject, inv.ident = uid, ident

        task = Task(2, 0, "", 1, "query_catalog", "test", {"subject": uid, "claims": claims})

        with mock.patch.object(self.agent.tools, "query_catalog", wraps=self.agent.tools.query_catalog) as spy:
            res, ran = self.agent._execute(inv, task)
            self.assertTrue(ran)
            self.assertIsNotNone(res)
            spy.assert_called_once()

    # -- Regression 4: Content-linked reference verification -----------------------
    def test_regression_forged_reference_rejected(self):
        """follow_reference must reject a reference unless the linked source record explicitly names it."""
        inv = Investigation(self.req)
        inv.intent_reason = "deletion requested"
        inv.authorized = True
        uid, ident = self.agent.tools.resolve_identity(self.req["email"])
        inv.subject, inv.ident = uid, ident

        direct = self.agent.tools.search_direct_evidence(ident, None, self.req["audit_at"])
        self.assertTrue(len(direct) > 0)
        real_rid = sorted(direct.keys())[0]
        inv.links[real_rid] = direct[real_rid]

        # Forged reference not named by real_rid in evidence
        forged_ref = "forged_ref_99999"
        fid = direct[real_rid].basis[0] if direct[real_rid].basis else "evidence_01"
        task = Task(2, 1, "", 1, "follow_reference", "test",
                    {"from": real_rid, "ref": forged_ref, "file": fid})

        with mock.patch.object(self.agent.tools, "follow_reference", wraps=self.agent.tools.follow_reference) as spy:
            with self.assertRaises(ToolFailure) as ctx:
                self.agent._execute(inv, task)
            self.assertIn("does not name reference", str(ctx.exception))
            spy.assert_not_called()

    def test_positive_genuine_reference_allowed(self):
        """Positive control: genuine reference explicitly named by source record in evidence is allowed."""
        req_copy = dict(self.req)
        req_copy["audit_at"] = "2026-12-31T00:00:00Z"

        inv = Investigation(req_copy)
        inv.intent_reason = "deletion requested"
        inv.authorized = True
        inv.subject = "user_06"
        inv.ident = self.ds.identities.by_user["user_06"]
        inv.links["search_001"] = Link("search_001", "search_service", "direct", ["evidence_57"])

        task = Task(2, 1, "", 1, "follow_reference", "test",
                    {"from": "search_001", "ref": "account_006", "file": "evidence_57"})

        with mock.patch.object(self.agent.tools, "follow_reference", wraps=self.agent.tools.follow_reference) as spy:
            res, ran = self.agent._execute(inv, task)
            self.assertTrue(ran)
            spy.assert_called_once()

    # -- Regression 5: Shared workflow context fail-closed validation --------------
    def test_regression_query_workflow_context_rejects_unprepared_or_stale_context(self):
        """query_workflow_context must reject absent, stale, or mismatched context, and fail closed if auditor missing."""
        inv = Investigation(self.req)
        inv.intent_reason = "deletion requested"
        inv.authorized = True
        uid, ident = self.agent.tools.resolve_identity(self.req["email"])
        inv.subject, inv.ident = uid, ident

        task = Task(3, 0, "", 1, "query_workflow_context", "test", {"case_id": self.req["case_id"]})
        spec = TOOLS["query_workflow_context"]

        # Sub-test A: auditor is None -> fails closed (no silent bypass)
        with self.assertRaises(ToolContractError) as ctx:
            check_preconditions(spec, inv, task, auditor=None)
        self.assertIn("auditor preparation context is required", str(ctx.exception))

        # Sub-test B: case not in auditor.prepared_key (absent context)
        mock_auditor = mock.MagicMock(spec=Auditor)
        mock_auditor.prepared_key = {}
        mock_auditor.workflow = {}
        mock_auditor._key = lambda r: "key_1"
        with self.assertRaises(ToolContractError) as ctx:
            check_preconditions(spec, inv, task, auditor=mock_auditor)
        self.assertIn("not prepared for case", str(ctx.exception))

        # Sub-test C: case in auditor.prepared_key has stale/mismatched key
        mock_auditor.prepared_key = {self.req["case_id"]: "stale_old_key"}
        with self.assertRaises(ToolContractError) as ctx:
            check_preconditions(spec, inv, task, auditor=mock_auditor)
        self.assertIn("stale or mismatched", str(ctx.exception))

    def test_positive_properly_prepared_workflow_context_allowed(self):
        """Positive control: properly prepared context satisfies the guard."""
        inv = Investigation(self.req)
        inv.intent_reason = "deletion requested"
        inv.authorized = True
        uid, ident = self.agent.tools.resolve_identity(self.req["email"])
        inv.subject, inv.ident = uid, ident

        task = Task(3, 0, "", 1, "query_workflow_context", "test", {"case_id": self.req["case_id"]})

        with mock.patch.object(self.agent.tools, "workflow_context", wraps=self.agent.tools.workflow_context) as spy:
            res, ran = self.agent._execute(inv, task)
            self.assertTrue(ran)
            self.assertIsNotNone(res)
            spy.assert_called_once()

    # -- Additional Precondition Category Tests -------------------------------------
    def test_unknown_guard_identifier_raises_contract_error(self):
        """Unknown precondition identifier fails closed with ToolContractError."""
        inv = Investigation(self.req)
        task = Task(1, 0, "", 1, "custom_tool", "test", {})
        custom_spec = mock.MagicMock()
        custom_spec.name = "custom_tool"
        custom_spec.precondition_id = "UNKNOWN_GUARD_ID_999"

        with self.assertRaises(ToolContractError) as ctx:
            check_preconditions(custom_spec, inv, task, self.auditor)
        self.assertIn("unknown precondition guard", str(ctx.exception))

    def test_precondition_failure_routes_to_deterministic_fallback(self):
        """A precondition failure in investigate_case safely routes to validated reference fallback."""
        # Inject an adversarial task directly into agent agenda
        req = self.ds.requests[0]
        agent = AuditAgent(self.auditor)

        original_push = agent._push
        injected = False
        def hooked_push(inv, tool, reason, inputs, wave=0, key=""):
            nonlocal injected
            original_push(inv, tool, reason, inputs, wave, key)
            if tool == "resolve_identity" and not injected:
                injected = True
                # Intentionally corrupt authorized flag to trigger precondition failure on execute
                inv.authorized = False

        agent._push = hooked_push
        res = agent.investigate_case(req)

        # Confirm fallback took over deterministically
        self.assertEqual(res.trace.get("fallback"), "reference_pipeline")
        self.assertIn("does not authorize deletion", res.trace.get("fallback_reason", ""))
        self.assertIn(res.status, ("COMPLETE", "INCOMPLETE"))

    def test_valid_investigation_path_satisfies_all_preconditions(self):
        """End-to-end investigation of a valid case satisfies every precondition without fallback."""
        res = self.agent.investigate_case(self.req)
        self.assertNotIn("fallback", res.trace)
        self.assertIn(res.status, ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"))
        self.assertEqual(res.case_id, self.req["case_id"])
        queries = res.trace["investigation"]["queries"]
        self.assertTrue(len(queries) >= 3)
        for q in queries:
            if q["result"] != "skipped":
                self.assertNotIn("error", q.get("result", {}))


if __name__ == "__main__":
    unittest.main()

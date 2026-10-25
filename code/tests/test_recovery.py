"""Failure recovery in the investigation controller, and honest run status.

Run:  python3 code/tests/test_recovery.py [--dataset /path/to/dataset]

A fault while evaluating one resource's evidence used to discard the whole case: the reference
pipeline calls the same kernel, failed identically, and the case became INCOMPLETE [] while the
run exited 0 with a "query budget exhausted" warning. Recovery is now scoped to the failing
resource, never claims COMPLETE without every state, and the run reports exit status 5.
Synthetic scenarios need the official deletion_rules.csv; the real-data test needs the dataset.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import main as cli  # noqa: E402
import test_pipeline as tp  # noqa: E402
from ghost_audit import agent as agent_mod  # noqa: E402
from ghost_audit import audit as audit_mod  # noqa: E402
from ghost_audit.agent import AuditAgent  # noqa: E402
from ghost_audit.audit import Auditor, Config  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.loaders import load_dataset  # noqa: E402

IVO = "ivo.brandt@example.org"
REQ = ("case_r1", IVO, "2026-05-01T00:00:00Z", "COMPLETE", "2026-05-05T12:00:00Z", "2026-05-20T00:00:00Z",
       tp.DELETE_TEXT)
CAPTURED = "2026-05-10T12:00:00Z"
# A remaining search document (finding) and a retained security record (verified present).
BUNDLE = tp._bundle(CAPTURED, [
    ("search_service", "customer_search_documents", [{"resource_id": "search_9901", "document": {"email": IVO}}]),
    ("security_service", "security_audit_events", [{"resource_id": "audit_record_9801",
                                                    "subject_reference": "user_903"}]),
])
EVIDENCE = [("evidence_r1", "evidence/records/r.json", CAPTURED, BUNDLE)]


def kernel_fault(*resources):
    """Make the shared kernel (used by the controller *and* the reference pipeline) fail for resources."""
    original = Auditor.resource_state

    def faulty(self, req, rid, *args, **kwargs):
        if rid in resources:
            raise ValueError(f"malformed observation for {rid}")
        return original(self, req, rid, *args, **kwargs)
    return mock.patch.object(Auditor, "resource_state", faulty)


def without(steps_json, resource):
    """The full plan's steps minus one resource, renumbered (what a scoped recovery must emit)."""
    steps = [s for s in json.loads(steps_json) if s["resource_id"] != resource]
    for i, s in enumerate(steps, 1):
        s["step_no"] = i
    return steps


class Harness(unittest.TestCase):
    def agent(self, evidence=EVIDENCE, max_queries=256):
        sc = tp.Scenario([REQ], evidence)
        self.addCleanup(sc.close)
        self.dir = sc.dir
        ds = load_dataset(sc.dir)
        auditor = Auditor(ds, load_evidence(sc.dir, ds.manifest), Config())
        auditor.prepare(ds.requests)
        self.req = ds.requests[0]
        return AuditAgent(auditor, max_queries=max_queries)


class ScopedRecovery(Harness):
    def test_kernel_fault_on_one_resource_keeps_the_other_evidence(self):
        ag = self.agent()
        full = ag.investigate_case(self.req)
        self.assertEqual(full.status, "INCOMPLETE")
        with kernel_fault("audit_record_9801"):
            res = ag.investigate_case(self.req)
        self.assertEqual(res.status, "INCOMPLETE")
        self.assertEqual(json.loads(res.steps_json), without(full.steps_json, "audit_record_9801"))
        self.assertEqual(list(res.trace["degraded"]["unresolved_states"]), ["audit_record_9801"])
        self.assertIn("reference pipeline failed", res.trace["degraded"]["reference_pipeline"])
        self.assertNotIn("fallback", res.trace)
        checks = [q["result"] for q in res.trace["investigation"]["queries"] if q["tool"] == "check_admissibility"]
        self.assertEqual(checks, [{"violations": []}])        # emitted citations stay admissible

    def test_unresolved_state_never_allows_complete(self):
        ag = self.agent()
        with kernel_fault("search_9901"):       # the only finding; what remains would read COMPLETE
            res = ag.investigate_case(self.req)
        self.assertEqual((res.status, res.steps_json), ("INCOMPLETE", "[]"))
        self.assertEqual(res.trace["fallback"], "failed_closed")
        self.assertEqual(list(res.trace["degraded"]["unresolved_states"]), ["search_9901"])

    def test_every_state_unresolved_fails_closed(self):
        ag = self.agent()
        with kernel_fault("search_9901", "audit_record_9801"):
            res = ag.investigate_case(self.req)
        self.assertEqual((res.status, res.steps_json), ("INCOMPLETE", "[]"))
        self.assertEqual(res.trace["fallback"], "failed_closed")

    def test_controller_layer_fault_is_recovered_with_the_complete_answer(self):
        ag = self.agent()
        full = ag.investigate_case(self.req)
        real = ag.tools.resolve_resource_state

        def wrapper_fault(req, rid, *args):
            if rid == "audit_record_9801":
                raise RuntimeError("tool wrapper defect")
            return real(req, rid, *args)
        with mock.patch.object(ag.tools, "resolve_resource_state", wrapper_fault):
            res = ag.investigate_case(self.req)
        self.assertEqual((res.status, res.steps_json), (full.status, full.steps_json))
        self.assertEqual(res.trace["fallback"], "reference_pipeline")
        self.assertNotIn("degraded", res.trace)

    def test_contract_violation_is_not_treated_as_a_scoped_evidence_fault(self):
        ag = self.agent()
        full = ag.investigate_case(self.req)
        on_workflow = ag._on_workflow

        def inject(inv, r, task):
            on_workflow(inv, r, task)
            ag._push(inv, "resolve_resource_state", "forged", {"resource": "account_0000", "how": "direct"}, key="a")
        with mock.patch.object(ag, "_on_workflow", inject):
            ag.handlers["query_workflow_context"] = inject
            res = ag.investigate_case(self.req)
        ag.handlers["query_workflow_context"] = on_workflow
        self.assertEqual(res.trace["fallback"], "reference_pipeline")
        self.assertIn("not linked to the subject", res.trace["fallback_reason"])
        self.assertNotIn("degraded", res.trace)
        self.assertEqual(res.steps_json, full.steps_json)
        self.assertNotIn("account_0000", res.steps_json)

    def test_malformed_state_result_cannot_reach_output(self):
        ag = self.agent()
        full = ag.investigate_case(self.req)
        with mock.patch.object(ag.tools, "resolve_resource_state", lambda *a, **k: {"exists": True}):
            res = ag.investigate_case(self.req)
        self.assertEqual(res.trace["fallback"], "reference_pipeline")
        self.assertEqual(res.steps_json, full.steps_json)


class RunStatus(Harness):
    def invoke(self, *flags):
        tmp = Path(tempfile.mkdtemp(prefix="ghost_recovery_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = tmp / "out.csv"
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = cli.main(["--dataset", str(self.dir), "--output", str(out), *flags])
        return rc, buf_err.getvalue(), out

    def test_clean_run_exits_zero(self):
        self.agent()
        rc, err, out = self.invoke()
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.is_file())

    def test_scoped_recovery_is_reported_and_not_a_success(self):
        self.agent()
        with kernel_fault("audit_record_9801"):
            rc, err, out = self.invoke()
        self.assertEqual(rc, 5)
        self.assertIn("could not be resolved", err)
        self.assertIn("audit_record_9801", err)
        self.assertNotIn("query budget exhausted", err)
        self.assertFalse(out.is_file())

    def test_scoped_recovery_preserves_preexisting_output(self):
        self.agent()
        tmp = Path(tempfile.mkdtemp(prefix="ghost_recovery_pre_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = tmp / "out.csv"
        out.write_text("sentinel output\n", encoding="utf-8")
        with kernel_fault("audit_record_9801"):
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = cli.main(["--dataset", str(self.dir), "--output", str(out)])
        self.assertEqual(rc, 5)
        self.assertEqual(out.read_text(encoding="utf-8"), "sentinel output\n")

    def test_failed_closed_case_is_reported_and_not_a_success(self):
        self.agent()
        with kernel_fault("search_9901", "audit_record_9801"):
            rc, err, _ = self.invoke()
        self.assertEqual(rc, 5)
        self.assertIn("failed closed", err)
        self.assertNotIn("query budget exhausted", err)

    def test_budget_exhaustion_is_reported_accurately(self):
        self.agent()
        rc, err, _ = self.invoke("--max-queries", "2")
        self.assertEqual(rc, 5)
        self.assertIn("query budget exhausted for 1 case", err)


class RealDataRecovery(unittest.TestCase):
    """E1 from the sprint: a kernel fault on support_9538 must not erase case_34's account finding."""

    @classmethod
    def setUpClass(cls):
        tp.require_dataset()

    def test_kernel_fault_keeps_case_34_account_evidence_and_nothing_else_changes(self):
        normal, _, _ = audit_mod.run(tp.DATASET)
        with kernel_fault("support_9538"):
            faulted, _, _ = audit_mod.run(tp.DATASET)
        before = {r.case_id: r for r in normal}
        after = {r.case_id: r for r in faulted}
        self.assertEqual(json.loads(after["case_34"].steps_json), without(before["case_34"].steps_json, "support_9538"))
        self.assertTrue(json.loads(after["case_34"].steps_json))
        self.assertEqual([c for c in before if c != "case_34" and
                          (before[c].status, before[c].steps_json) != (after[c].status, after[c].steps_json)], [])


if __name__ == "__main__":
    argv = list(sys.argv)
    if "--dataset" in argv:
        i = argv.index("--dataset")
        tp.DATASET = Path(argv[i + 1]).resolve()
        os.environ["GHOST_AUDIT_DATASET"] = str(tp.DATASET)
        del argv[i:i + 2]
    unittest.main(verbosity=2, argv=argv)

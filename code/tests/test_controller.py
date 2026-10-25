"""Behaviour tests for the evidence-driven investigation controller (ghost_audit.agent).

Run:  python3 code/tests/test_controller.py [--dataset /path/to/dataset]

Synthetic scenarios need only the official deletion_rules.csv; the real-data checks need
the full participant dataset. Both are skipped, with the missing files named, otherwise.
"""

from __future__ import annotations

import contextlib
import io
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

import test_pipeline as tp  # noqa: E402
from ghost_audit import agent as agent_mod  # noqa: E402
from ghost_audit.agent import (STOP_BUDGET, STOP_IDENTITY, STOP_NOT_AUTHORIZED, TOOLS, AuditAgent,  # noqa: E402
                               ToolContractError, ToolSpec, check_inputs, describe_tools)
from ghost_audit.audit import Auditor, Config, run  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.loaders import load_dataset  # noqa: E402
from ghost_audit.model import Step  # noqa: E402

IVO = "ivo.brandt@example.org"
REQ = ("case_k1", IVO, "2026-05-01T00:00:00Z", "COMPLETE", "2026-05-05T12:00:00Z", "2026-05-20T00:00:00Z",
       tp.DELETE_TEXT)
CAPTURED = "2026-05-10T12:00:00Z"


def _search(rid, **extra):
    return {"resource_id": rid, "document": {"email": IVO}, **extra}


class Harness(unittest.TestCase):
    """Build a synthetic dataset, prepare the shared context and return a controller."""

    def agent(self, evidence, requests=(REQ,), activity=(), max_queries=256, cfg=None):
        sc = tp.Scenario(list(requests), evidence, activity)
        self.addCleanup(sc.close)
        ds = load_dataset(sc.dir)
        ev = load_evidence(sc.dir, ds.manifest)
        auditor = Auditor(ds, ev, cfg or Config())
        auditor.prepare(ds.requests)
        self.req = {r["case_id"]: r for r in ds.requests}
        return AuditAgent(auditor, max_queries=max_queries)

    @staticmethod
    def tools_run(res):
        return [q["tool"] for q in res.trace["investigation"]["queries"] if q["result"] != "skipped"]

    @staticmethod
    def follows(res):
        return [(q["inputs"]["from"], q["inputs"]["ref"]) for q in res.trace["investigation"]["queries"]
                if q["tool"] == "follow_reference" and q["result"] != "skipped"]

    @staticmethod
    def resources(res):
        return {s["resource_id"] for s in json.loads(res.steps_json)}

    def chain_evidence(self, with_reference=True, cycle=False):
        """search_9901 (subject's email) -> account_9902 -> export_9903 (or back to search_9901)."""
        doc = _search("search_9901", source_resource_id="account_9902") if with_reference else _search("search_9901")
        nxt = "search_9901" if cycle else "export_9903"
        snap = tp._bundle(CAPTURED, [
            ("search_service", "customer_search_documents", [doc]),
            ("account_service", "customer_profiles", [{"resource_id": "account_9902", "archive_resource_id": nxt}]),
            ("export_service", "user_exports", [{"resource_id": "export_9903", "object_key": "exports/x.zip"}]),
        ])
        return [("evidence_k1", "evidence/records/k.json", CAPTURED, snap)]


class EvidenceDrivenRouting(Harness):
    def test_reference_chain_followed_to_closure(self):
        ag = self.agent(self.chain_evidence())
        res = ag.investigate_case(self.req["case_k1"])
        self.assertEqual(self.follows(res), [("search_9901", "account_9902"), ("account_9902", "export_9903")])
        self.assertEqual({r: h for r, (h, _) in res.trace["links"].items()},
                         {"search_9901": "direct", "account_9902": "reference", "export_9903": "reference"})
        # The reference pipeline follows the same closure.
        ref = ag.a.audit(self.req["case_k1"])
        self.assertEqual((res.status, res.steps_json), (ref.status, ref.steps_json))

    def test_no_reference_in_evidence_means_no_reference_query(self):
        res = self.agent(self.chain_evidence(with_reference=False)).investigate_case(self.req["case_k1"])
        self.assertNotIn("follow_reference", self.tools_run(res))
        self.assertEqual(set(res.trace["links"]), {"search_9901"})

    def test_reference_cycle_terminates(self):
        res = self.agent(self.chain_evidence(cycle=True)).investigate_case(self.req["case_k1"])
        self.assertEqual(self.follows(res), [("search_9901", "account_9902")])
        self.assertEqual(res.trace["investigation"]["stop_reason"], agent_mod.STOP_DONE)

    def test_catalog_queried_only_when_a_claim_names_the_subject(self):
        snap = tp._bundle(CAPTURED, [("search_service", "customer_search_documents", [
            _search("search_9960"), {"resource_id": "search_9961", "document": {"email": None}}])])
        ev = [("evidence_k2", "evidence/records/c.json", CAPTURED, snap)]
        claim = ("activity_k1", "2026-05-09T00:00:00Z", "LINK_RESOURCE_IDENTITY", "data_catalog", "search_service",
                 "search_9961", "SUCCESS", f"Data catalog linked search_9961 (search_9961) to {IVO} and user_903.")
        with_claim = self.agent(ev, activity=[claim]).investigate_case(self.req["case_k1"])
        without = self.agent(ev).investigate_case(self.req["case_k1"])
        self.assertIn("query_catalog", self.tools_run(with_claim))
        self.assertIn("search_9961", with_claim.trace["links"])
        self.assertNotIn("query_catalog", self.tools_run(without))
        self.assertNotIn("search_9961", without.trace["links"])

    def test_contradicted_catalog_claim_is_recorded_not_linked(self):
        snap = tp._bundle(CAPTURED, [("search_service", "customer_search_documents", [
            _search("search_9960"), {"resource_id": "search_9961", "document": {"email": None}}])])
        claim = ("activity_k1", "2026-05-09T00:00:00Z", "LINK_RESOURCE_IDENTITY", "data_catalog", "search_service",
                 "search_9961", "SUCCESS", f"Data catalog linked search_9961 (search_9961) to {IVO} and user_905.")
        res = self.agent([("evidence_k2", "evidence/records/c.json", CAPTURED, snap)],
                         activity=[claim]).investigate_case(self.req["case_k1"])
        self.assertIn({"resource": "search_9961", "issue": "catalog claim not corroborated or contradicted by evidence"},
                      res.trace["investigation"]["contradictions"])
        self.assertNotIn("search_9961", self.resources(res))

    def test_non_deletion_request_stops_after_authorization(self):
        req = REQ[:6] + ("Could you send me a copy of the information held on my account?",)
        res = self.agent(self.chain_evidence(), requests=[req]).investigate_case(self.req["case_k1"])
        self.assertEqual((res.status, res.steps_json), ("NOT_APPLICABLE", "[]"))
        self.assertEqual(self.tools_run(res), ["classify_intent"])
        self.assertEqual(res.trace["investigation"]["stop_reason"], STOP_NOT_AUTHORIZED)

    def test_unknown_identity_stops_after_identification(self):
        req = ("case_k1", "nobody@example.invalid") + REQ[2:]
        res = self.agent(self.chain_evidence(), requests=[req]).investigate_case(self.req["case_k1"])
        self.assertEqual((res.status, res.steps_json), ("INCOMPLETE", "[]"))
        self.assertEqual(self.tools_run(res), ["classify_intent", "resolve_identity"])
        self.assertEqual(res.trace["investigation"]["stop_reason"], STOP_IDENTITY)

    def test_post_cutoff_evidence_never_links_or_cites(self):
        late = tp._bundle("2026-05-25T00:00:00Z", [("search_service", "customer_search_documents", [
            _search("search_9970")])])
        res = self.agent(self.chain_evidence() + [("evidence_k9", "evidence/records/late.json",
                                                   "2026-05-25T00:00:00Z", late)]).investigate_case(self.req["case_k1"])
        self.assertNotIn("search_9970", res.trace["links"])
        self.assertNotIn("evidence_k9", res.steps_json)


class Bounds(Harness):
    def test_budget_stops_a_runaway_agenda(self):
        ag = self.agent(self.chain_evidence(), max_queries=12)
        counter = iter(range(10_000, 99_999))
        with mock.patch.object(ag.tools, "references_from", lambda rid, lo, at: [(f"export_{next(counter)}", "evidence_k1")]), \
                mock.patch.object(ag.tools, "follow_reference", lambda ident, ref, at: "export_service"):
            res = ag.investigate_case(self.req["case_k1"])
        self.assertEqual((res.status, res.steps_json), ("INCOMPLETE", "[]"))
        self.assertEqual(res.trace["investigation"]["stop_reason"], STOP_BUDGET)
        self.assertEqual(len(self.tools_run(res)), 12)
        self.assertTrue(res.trace["investigation_incomplete"])

    def test_repeated_queries_are_suppressed(self):
        ag = self.agent(self.chain_evidence(with_reference=False))
        with mock.patch.object(ag.tools, "references_from", lambda rid, lo, at: [("account_9999", "evidence_k1")] * 3), \
                mock.patch.object(ag.tools, "follow_reference", lambda ident, ref, at: None):
            res = ag.investigate_case(self.req["case_k1"])
        qs = [q for q in res.trace["investigation"]["queries"] if q["tool"] == "follow_reference"]
        self.assertEqual([q["result"] for q in qs], [{"system": None}, "skipped", "skipped"])
        self.assertNotIn("account_9999", res.trace["links"])


class FailClosed(Harness):
    def test_tool_exception_falls_back_to_reference_pipeline(self):
        ag = self.agent(self.chain_evidence())
        expected = ag.a.audit(self.req["case_k1"])
        with mock.patch.object(ag.tools, "resolve_resource_state", side_effect=RuntimeError("store offline")):
            res = ag.investigate_case(self.req["case_k1"])
        self.assertEqual((res.status, res.steps_json), (expected.status, expected.steps_json))
        self.assertEqual(res.trace["fallback"], "reference_pipeline")
        failed = [q for q in res.trace["investigation"]["queries"] if isinstance(q["result"], dict)
                  and "error" in q["result"]]
        self.assertEqual(failed[0]["tool"], "resolve_resource_state")

    def test_failed_fallback_fails_closed(self):
        ag = self.agent(self.chain_evidence())
        with mock.patch.object(ag.tools, "resolve_resource_state", side_effect=RuntimeError("store offline")), \
                mock.patch.object(ag.a, "audit", side_effect=RuntimeError("kernel offline")):
            res = ag.investigate_case(self.req["case_k1"])
        self.assertEqual((res.status, res.steps_json), ("INCOMPLETE", "[]"))
        self.assertEqual(res.trace["fallback"], "failed_closed")

    def _with_compiled(self, steps, status="INCOMPLETE"):
        ag = self.agent(self.chain_evidence())
        with mock.patch.object(ag.tools, "compile_obligations", lambda states, audit_at: (status, steps, [])):
            return ag.investigate_case(self.req["case_k1"]), ag

    def test_unlinked_resource_cannot_reach_output(self):
        res, ag = self._with_compiled([Step("FINDING", "account_service", "account_0001", "REPORT_REMAINING_RECORD",
                                            "evidence_k1")])
        self.assertNotIn("account_0001", res.steps_json)
        self.assertEqual(res.trace["fallback"], "reference_pipeline")
        self.assertIn("not linked", res.trace["fallback_reason"])

    def test_unknown_or_post_cutoff_citation_cannot_reach_output(self):
        for bad in ("evidence_does_not_exist", "activity_unknown"):
            res, _ = self._with_compiled([Step("FINDING", "search_service", "search_9901", "REPORT_REMAINING_RECORD",
                                               bad)])
            self.assertNotIn(bad, res.steps_json)
            self.assertIn("unknown evidence id", res.trace["fallback_reason"])
        late = tp._bundle("2026-05-25T00:00:00Z", [("search_service", "customer_search_documents", [])])
        ag = self.agent(self.chain_evidence() + [("evidence_k9", "evidence/records/late.json",
                                                  "2026-05-25T00:00:00Z", late)])
        bad = [Step("FINDING", "search_service", "search_9901", "REPORT_REMAINING_RECORD", "evidence_k9")]
        with mock.patch.object(ag.tools, "compile_obligations", lambda states, audit_at: ("INCOMPLETE", bad, [])):
            res = ag.investigate_case(self.req["case_k1"])
        self.assertNotIn("evidence_k9", res.steps_json)
        self.assertIn("after audit_at", res.trace["fallback_reason"])

    def test_malformed_tool_output_cannot_bypass_validation(self):
        ag = self.agent(self.chain_evidence())
        expected = ag.a.audit(self.req["case_k1"])
        for garbage in ("COMPLETE", None, ("COMPLETE",), ("BOGUS_STATUS", [], [])):
            with mock.patch.object(ag.tools, "compile_obligations", lambda states, audit_at, g=garbage: g):
                res = ag.investigate_case(self.req["case_k1"])
            self.assertEqual((res.status, res.steps_json), (expected.status, expected.steps_json), garbage)
            self.assertEqual(res.trace["fallback"], "reference_pipeline", garbage)


class ToolContracts(Harness):
    def test_inputs_are_checked_against_the_contract(self):
        spec = TOOLS["follow_reference"]
        check_inputs(spec, {"from": "search_1", "ref": "account_2", "file": "evidence_3"})
        for bad in ({"from": "search_1", "ref": "account_2"},
                    {"from": "search_1", "ref": "account_2", "file": "evidence_3", "extra": "x"},
                    {"from": "search_1", "ref": None, "file": "evidence_3"},
                    {"from": "search_1", "ref": "", "file": "evidence_3"}):
            with self.assertRaises(ToolContractError):
                check_inputs(spec, bad)
        check_inputs(TOOLS["search_direct_evidence"], {"subject": "user_1", "window": [None, "2026-01-01"]})
        with self.assertRaises(ToolContractError):
            check_inputs(TOOLS["search_direct_evidence"], {"subject": "user_1", "window": [1]})

    def test_every_query_is_a_declared_tool_in_its_declared_phase(self):
        res = self.agent(self.chain_evidence()).investigate_case(self.req["case_k1"])
        qs = res.trace["investigation"]["queries"]
        self.assertTrue(qs)
        for q in qs:
            self.assertIn(q["tool"], TOOLS)
            self.assertEqual(q["stage"], TOOLS[q["tool"]].phase)
            self.assertEqual(set(q["inputs"]), {k for k, _ in TOOLS[q["tool"]].inputs})

    def test_changing_a_contract_changes_execution(self):
        """The registry is enforced at run time: a contract the controller does not satisfy
        stops the investigation and the case takes the validated fallback."""
        ag = self.agent(self.chain_evidence())
        stricter = ToolSpec("follow_reference", "DISCOVER", "x", (("from", "str"), ("ref", "str"), ("file", "str"),
                                                                    ("reason", "str")), "x")
        with mock.patch.dict(TOOLS, {"follow_reference": stricter}):
            res = ag.investigate_case(self.req["case_k1"])
        self.assertEqual(res.trace["fallback"], "reference_pipeline")
        self.assertIn("follow_reference", res.trace["fallback_reason"])

    def test_cli_lists_the_registry(self):
        sys.path.insert(0, str(CODE))
        import main as cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--list-tools"]), 0)
        self.assertEqual(json.loads(buf.getvalue()), describe_tools())
        self.assertEqual([t["name"] for t in describe_tools()], list(TOOLS))


class RealDataFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tp.require_dataset()

    def test_forced_fallback_reproduces_the_controller_output(self):
        normal, _, _ = run(tp.DATASET)
        with mock.patch.object(agent_mod.AuditTools, "resolve_resource_state", side_effect=RuntimeError("injected")):
            forced, _, _ = run(tp.DATASET)
        self.assertEqual([(r.case_id, r.status, r.steps_json) for r in normal],
                         [(r.case_id, r.status, r.steps_json) for r in forced])
        self.assertEqual({r.trace.get("fallback") for r in forced if r.trace.get("fallback")}, {"reference_pipeline"})
        self.assertGreater(sum(1 for r in forced if r.trace.get("fallback")), 400)
        self.assertFalse(any(r.trace.get("fallback") for r in normal))

    def test_every_emitted_step_is_linked_and_admissible(self):
        results, ds, _ = run(tp.DATASET)
        req = {r["case_id"]: r for r in ds.requests}
        for r in results:
            violations = [q["result"]["violations"] for q in r.trace["investigation"]["queries"]
                          if q["tool"] == "check_admissibility"]
            if r.status == "NOT_APPLICABLE":
                self.assertEqual(violations, [])
                continue
            self.assertEqual(violations, [[]], r.case_id)
            for s in json.loads(r.steps_json):
                self.assertIn(s["resource_id"], r.trace["links"], r.case_id)
                if s["evidence_id"]:
                    self.assertLessEqual(ds.manifest[s["evidence_id"]]["captured_at"], req[r.case_id]["audit_at"])


if __name__ == "__main__":
    argv = list(sys.argv)
    if "--dataset" in argv:
        i = argv.index("--dataset")
        tp.DATASET = Path(argv[i + 1]).resolve()
        os.environ["GHOST_AUDIT_DATASET"] = str(tp.DATASET)
        del argv[i:i + 2]
    unittest.main(verbosity=2, argv=argv)

"""Behavioural tests for the bounded investigation controller (ghost_audit.agent).

Run:  python3 code/tests/test_agent.py [--dataset /path/to/dataset]

All tests here exercise the controller on the participant dataset and are skipped, with
the missing files named, when it is not available.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
CODE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE))
DATASET = Path(os.environ.get("GHOST_AUDIT_DATASET") or CODE.parent / "dataset")

from ghost_audit.agent import AuditAgent, AuditTools  # noqa: E402
from ghost_audit.audit import Auditor, Config  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.loaders import load_dataset, read_csv  # noqa: E402

REAL_DATA = ("deletion_requests.csv", "sample_deletion_requests.csv", "user_identities.csv", "user_audit_log.csv",
             "system_activity.csv", "deletion_rules.csv", "evidence_files.csv", "evidence/")


def require_dataset(files=REAL_DATA):
    missing = [f for f in files if not ((DATASET / f).is_dir() if f.endswith("/") else (DATASET / f).is_file())]
    if missing:
        raise unittest.SkipTest(f"participant dataset not available at {DATASET} (missing {missing}); "
                                "pass --dataset PATH or set GHOST_AUDIT_DATASET")


STAGES = ["AUTHORIZE", "IDENTIFY", "DISCOVER", "INVESTIGATE", "COMPILE", "VALIDATE"]


def _prepared(requests_file: str):
    ds = load_dataset(DATASET, DATASET / requests_file)
    ev = load_evidence(DATASET, ds.manifest)
    auditor = Auditor(ds, ev, Config())
    universe = {r["case_id"]: r for r in read_csv(DATASET / "deletion_requests.csv")
                + read_csv(DATASET / "sample_deletion_requests.csv")}
    auditor.prepare([universe[k] for k in sorted(universe)])
    return ds, ev, auditor


class AgentBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_dataset()
        cls.ds, cls.ev, cls.auditor = _prepared("deletion_requests.csv")
        cls.agent = AuditAgent(cls.auditor)
        cls.req = {r["case_id"]: r for r in cls.ds.requests}
        cls.results = {cid: cls.agent.investigate_case(r) for cid, r in cls.req.items()}
        cls.sds, _, cls.sauditor = _prepared("sample_deletion_requests.csv")
        cls.sagent = AuditAgent(cls.sauditor)
        cls.sreq = {r["case_id"]: r for r in cls.sds.requests}

    def queries(self, cid, results=None):
        return (results or self.results)[cid].trace["investigation"]["queries"]

    def test_equivalent_to_reference_pipeline(self):
        for cid, r in self.req.items():
            ref = self.auditor.audit(r)
            got = self.results[cid]
            self.assertEqual((got.status, got.steps_json), (ref.status, ref.steps_json), cid)
        for cid, r in self.sreq.items():
            ref = self.sauditor.audit(r)
            got = self.sagent.investigate_case(r)
            self.assertEqual((got.status, got.steps_json), (ref.status, ref.steps_json), cid)

    def test_stages_run_in_order_and_every_query_has_a_reason(self):
        for cid, res in self.results.items():
            qs = self.queries(cid)
            order = [STAGES.index(q["stage"]) for q in qs]
            self.assertEqual(order, sorted(order), cid)
            self.assertTrue(all(q["reason"] for q in qs), cid)
            if res.status != "NOT_APPLICABLE":
                self.assertEqual({q["stage"] for q in qs}, set(STAGES), cid)

    def test_non_deletion_stops_after_authorization(self):
        for cid, res in self.results.items():
            if res.status == "NOT_APPLICABLE":
                self.assertEqual([q["tool"] for q in self.queries(cid)], ["classify_intent"], cid)
                self.assertEqual(res.trace["investigation"]["stop_reason"], "request does not authorize deletion")

    def test_catalog_query_only_when_a_claim_names_the_subject(self):
        tools = self.agent.tools
        called = without = 0
        for cid, res in self.results.items():
            if res.status == "NOT_APPLICABLE":
                continue
            ident = self.auditor.ds.identities.by_user[res.trace["subject"]]
            has_claim = bool(tools.catalog_claims_naming(ident))
            ran = any(q["tool"] == "query_catalog" for q in self.queries(cid))
            self.assertEqual(ran, has_claim, cid)
            called += ran
            without += not ran
        self.assertGreater(called, 0)
        self.assertGreater(without, 0)

    def test_references_followed_only_from_records_that_carry_them(self):
        res = self.sagent.investigate_case(self.sreq["case_06"])
        follows = [q for q in self.queries("case_06", {"case_06": res}) if q["tool"] == "follow_reference"]
        self.assertEqual([(q["inputs"]["from"], q["inputs"]["ref"]) for q in follows], [("search_001", "account_006")])
        self.assertEqual(follows[0]["evidence_refs"], ["evidence_57"])
        no_refs = [cid for cid in self.results
                   if not any(q["tool"] == "follow_reference" for q in self.queries(cid))]
        self.assertGreater(len(no_refs), 500)

    def test_no_repeated_queries(self):
        for cid in self.results:
            keys = [(q["tool"], json.dumps(q["inputs"], sort_keys=True)) for q in self.queries(cid)
                    if q["result"] != "skipped"]
            self.assertEqual(len(keys), len(set(keys)), cid)

    def test_budget_bound_stops_without_fabricating(self):
        bounded = AuditAgent(self.auditor, max_queries=3)
        res = bounded.investigate_case(self.req["case_34"])
        inv = res.trace["investigation"]
        self.assertEqual(inv["stop_reason"], "query budget exhausted")
        self.assertEqual(len([q for q in inv["queries"] if q["result"] != "skipped"]), 3)
        self.assertEqual((res.status, res.steps_json), ("INCOMPLETE", "[]"))
        self.assertTrue(res.trace["investigation_incomplete"])

    def test_evidence_references_are_admissible_and_resolvable(self):
        files = self.ds.manifest
        acts = {a["activity_id"] for a in self.ds.activity}
        for cid, res in self.results.items():
            audit_at = self.req[cid]["audit_at"]
            for q in self.queries(cid):
                for ref in q["evidence_refs"]:
                    self.assertTrue(ref in files or ref in acts, (cid, ref))
                    if q["tool"] == "resolve_resource_state" and ref in files:
                        self.assertLessEqual(files[ref]["captured_at"], audit_at, (cid, ref))

    def test_unresolved_candidates_are_reported_not_assigned(self):
        hit = 0
        for cid, res in self.results.items():
            inv = res.trace.get("investigation", {})
            unresolved = inv.get("unresolved", [])
            if not unresolved:
                continue
            hit += 1
            emitted = {s["resource_id"] for s in json.loads(res.steps_json)}
            self.assertFalse(set(unresolved) & emitted, cid)
            self.assertIn("no admissible distinguishing query", inv["stop_reason"])
        self.assertGreater(hit, 0)

    def test_contradicted_catalog_claim_is_recorded(self):
        inv = self.results["case_63"].trace["investigation"]
        self.assertIn({"resource": "marketing_001",
                       "issue": "catalog claim not corroborated or contradicted by evidence"}, inv["contradictions"])
        self.assertEqual(self.results["case_63"].steps_json, "[]")

    def test_tools_require_shared_context(self):
        bare = Auditor(self.ds, self.ev, Config())
        with self.assertRaises(ValueError):
            AuditTools(bare)


class ImageProvenance(unittest.TestCase):
    def test_all_images_bound_to_hash_and_decodable(self):
        require_dataset(("evidence_files.csv", "user_identities.csv", "user_audit_log.csv",
                         "deletion_requests.csv", "evidence/"))
        ds = load_dataset(DATASET)
        ev = load_evidence(DATASET, ds.manifest)
        images = [m for m in ev.files.values() if m.path.suffix == ".png"]
        self.assertEqual(len(images), 11)
        for m in images:
            self.assertEqual(m.parse_status, "parsed_visual_cache", m.file_id)
            self.assertEqual(len(m.image["sha256"]), 64)
            if m.image.get("decoder") == "Pillow":
                self.assertTrue(m.image.get("readable"), m.file_id)
                self.assertEqual(m.image.get("format"), "PNG")
                self.assertGreater(m.image.get("width", 0), 0)


if __name__ == "__main__":
    argv = list(sys.argv)
    if "--dataset" in argv:
        i = argv.index("--dataset")
        DATASET = Path(argv[i + 1]).resolve()
        os.environ["GHOST_AUDIT_DATASET"] = str(DATASET)
        del argv[i:i + 2]
    unittest.main(verbosity=2, argv=argv)

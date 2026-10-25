"""Property, metamorphic and authored-scenario tests for the deletion-audit pipeline.

Run:  python3 code/tests/test_pipeline.py [--dataset /path/to/dataset]
      python3 -m unittest discover -s code          (dataset via GHOST_AUDIT_DATASET)

Tests that need the participant dataset (real-data properties) or its official
deletion_rules.csv (synthetic scenarios) are skipped, with the missing files named, when
the dataset is not available; unit/contract tests always run.

Two kinds of tests live here and must not be confused:
  * Real-data properties (determinism, shuffle/case invariance, contract) run
    the pipeline on the participant dataset and check invariants, not labels.
  * Authored scenarios build small synthetic datasets in a temp directory and
    check the expected answer derived by hand from the published contract and
    deletion_rules.csv. They are not hidden-label accuracy.
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True
CODE = Path(__file__).resolve().parent.parent
ROOT = CODE.parent
sys.path.insert(0, str(CODE))

from ghost_audit.audit import Auditor, Config, run  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.intent import classify  # noqa: E402
from ghost_audit.loaders import load_dataset  # noqa: E402
from ghost_audit.output import check_row  # noqa: E402

DATASET = Path(os.environ.get("GHOST_AUDIT_DATASET") or ROOT / "dataset")
REAL_DATA = ("deletion_requests.csv", "sample_deletion_requests.csv", "user_identities.csv", "user_audit_log.csv",
             "system_activity.csv", "deletion_rules.csv", "evidence_files.csv", "evidence/")


def require_dataset(files=REAL_DATA):
    """Skip (never fail or pass vacuously) when required participant inputs are absent."""
    missing = [f for f in files if not ((DATASET / f).is_dir() if f.endswith("/") else (DATASET / f).is_file())]
    if missing:
        raise unittest.SkipTest(f"participant dataset not available at {DATASET} (missing {missing}); "
                                "pass --dataset PATH or set GHOST_AUDIT_DATASET")


def _rows(results):
    return {r.case_id: (r.status, r.steps_json) for r in results}


class RealDataProperties(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_dataset()
        cls.ds = load_dataset(DATASET)
        cls.ev = load_evidence(DATASET, cls.ds.manifest)

    def _audit(self, requests, cfg=None):
        auditor = Auditor(self.ds, self.ev, cfg or Config())
        universe = {r["case_id"]: r for r in self.ds.requests}
        for r in requests:
            universe.setdefault(r["case_id"], r)
        auditor.prepare([universe[k] for k in sorted(universe)])
        return [auditor.audit(r) for r in requests]

    def test_contract_all_rows(self):
        results = self._audit(self.ds.requests)
        self.assertEqual(len(results), 518)
        errors = [e for r in results for e in check_row(r.case_id, r.status, r.steps_json)]
        self.assertEqual(errors, [])

    def test_deterministic_repeat(self):
        a = _rows(self._audit(self.ds.requests))
        b = _rows(self._audit(self.ds.requests))
        self.assertEqual(a, b)

    def test_shuffled_requests_same_answers(self):
        base = _rows(self._audit(self.ds.requests))
        shuffled = list(self.ds.requests)
        random.Random(7).shuffle(shuffled)
        self.assertEqual(_rows(self._audit(shuffled)), base)

    def test_email_case_invariance(self):
        sample = self.ds.requests[:60]
        base = _rows(self._audit(sample))
        upper = [dict(r, email=r["email"].upper()) for r in sample]
        self.assertEqual(_rows(self._audit(upper)), base)

    def test_no_citation_after_audit_at(self):
        captured = {fid: m["captured_at"] for fid, m in self.ds.manifest.items()}
        for req, res in zip(self.ds.requests, self._audit(self.ds.requests)):
            for step in json.loads(res.steps_json):
                if step["evidence_id"]:
                    self.assertLessEqual(captured[step["evidence_id"]], req["audit_at"], (req["case_id"], step))

    def test_earlier_audit_at_never_uses_later_evidence(self):
        # Metamorphic: moving audit_at back before a cited file's capture time must
        # remove that file from the answer.
        auditor = Auditor(self.ds, self.ev, Config())
        auditor.prepare(list(self.ds.requests))
        for req in self.ds.requests[:200]:
            res = auditor.audit(req)
            cited = {s["evidence_id"] for s in json.loads(res.steps_json) if s["evidence_id"]}
            if not cited:
                continue
            fid = sorted(cited)[0]
            cap = self.ds.manifest[fid]["captured_at"]
            earlier = dict(req, audit_at=_minus_one_second(cap))
            if earlier["audit_at"] < earlier["requested_at"]:
                continue
            res2 = auditor.audit(earlier)
            self.assertNotIn(fid, {s["evidence_id"] for s in json.loads(res2.steps_json)}, req["case_id"])

    def test_empty_complete_only_without_admissible_evidence(self):
        # Completion requires proof. A COMPLETE row with no steps is allowed only when
        # nothing admissible links any resource to the subject, and it must be flagged.
        auditor = Auditor(self.ds, self.ev, Config())
        auditor.prepare(list(self.ds.requests))
        for req in self.ds.requests:
            res = auditor.audit(req)
            if res.status == "COMPLETE" and res.steps_json == "[]":
                self.assertTrue(res.trace.get("insufficient_evidence"), req["case_id"])
                self.assertEqual(res.trace.get("links"), {}, req["case_id"])

    def test_case_order_modes_are_deterministic(self):
        from ghost_audit.output import write_csv
        rows = [(r.case_id, r.status, r.steps_json) for r in self._audit(self.ds.requests[:80])]
        tmp = Path(tempfile.mkdtemp(prefix="ghost_order_"))
        try:
            for mode in ("natural", "lexicographic"):
                write_csv(tmp / f"{mode}.csv", list(reversed(rows)), mode)
                ids = [r["case_id"] for r in csv.DictReader(open(tmp / f"{mode}.csv", encoding="utf-8"))]
                expect = sorted(ids, key=lambda c: int(c.split("_")[1])) if mode == "natural" else sorted(ids)
                self.assertEqual(ids, expect)
                self.assertEqual(sorted(ids), sorted(r[0] for r in rows))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_output_independent_of_hash_seed(self):
        import hashlib
        import os
        import subprocess
        digests = set()
        out = Path(tempfile.mkdtemp(prefix="ghost_seed_"))
        try:
            for seed in ("0", "7", "4242"):
                target = out / f"s{seed}.csv"
                subprocess.run([sys.executable, str(CODE / "main.py"), "--requests",
                                str(DATASET / "sample_deletion_requests.csv"), "--output", str(target)],
                               check=True, capture_output=True, env=dict(os.environ, PYTHONHASHSEED=seed))
                digests.add(hashlib.sha256(target.read_bytes()).hexdigest())
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertEqual(len(digests), 1)

    def test_non_deletion_requests_are_empty(self):
        for req, res in zip(self.ds.requests, self._audit(self.ds.requests)):
            if not classify(req["request_text"]).authorizes_deletion:
                self.assertEqual((res.status, res.steps_json), ("NOT_APPLICABLE", "[]"))


def _minus_one_second(ts: str) -> str:
    from datetime import datetime, timedelta
    t = datetime.fromisoformat(ts.replace("Z", "+00:00")) - timedelta(seconds=1)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class CheckRowRobustness(unittest.TestCase):
    """check_row must report malformed steps_json, never raise."""

    def _errs(self, payload, status="COMPLETE"):
        return check_row("case_x", status, payload)

    def test_object_scalar_null_string_and_invalid_json(self):
        for payload, kind in (('{"step_no": 1}', "dict"), ("5", "int"), ("null", "NoneType"), ('"x"', "str")):
            errs = self._errs(payload)
            self.assertEqual(len(errs), 1, payload)
            self.assertIn(f"must be a JSON array, got {kind}", errs[0])
        self.assertIn("not valid JSON", self._errs("not json")[0])

    def test_valid_arrays(self):
        self.assertEqual(self._errs("[]", "NOT_APPLICABLE"), [])
        good = json.dumps([{"step_no": 1, "phase": "VERIFICATION", "system_id": "account_service",
                            "resource_id": "account_1", "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT",
                            "evidence_id": "evidence_01"}])
        self.assertEqual(self._errs(good), [])

    def test_malformed_steps_inside_array(self):
        errs = self._errs(json.dumps([7, {"step_no": 1}]))
        self.assertTrue(any("step 1 must be a JSON object, got int" in e for e in errs))
        self.assertTrue(any("step 2 fields" in e for e in errs))
        bad_op = json.dumps([{"step_no": 1, "phase": "VERIFICATION", "system_id": "s", "resource_id": "r",
                              "operation": "DELETE_EVERYTHING", "state": "ABSENT", "evidence_id": "e"}])
        self.assertTrue(any("invalid phase/operation" in e for e in self._errs(bad_op)))


class IntentTests(unittest.TestCase):
    def test_multilingual_and_negated(self):
        yes = [
            "Please erase the account linked to this email address.",
            "Solicito la eliminación permanente de la cuenta asociada a este correo.",
            "Solicito a exclusão permanente da conta associada a este e-mail.",
            "Je demande la suppression définitive du compte associé à cette adresse e-mail.",
            "このメールアドレスに関連するアカウントの完全な削除を依頼します。",
            "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.",
            "Can you verify that my account was deleted? Please check the latest state.",
            "Deseo ejercer mi derecho de supresión y eliminar mi perfil.",        # profile, not account
            "Je demande la suppression de mon profil.",
            "プロフィールを削除してください。",
            "제 프로필을 삭제해 주세요.",
        ]
        no = [
            "Please stop the promotional emails. I am not asking for deletion.",
            "I'd like to download my account information in a portable format. Please keep the account as it is.",
            "My surname is misspelled. Can you correct it without removing any of my records?",
            "No quiero eliminar mi cuenta, solo actualizar mi teléfono.",
            "Could you send me a copy of the information held on my account? Please don't delete anything.",
            "Hello, I have a question about my last order.",
            "No quiero eliminar mi perfil, solo actualizar mi teléfono.",   # negation still wins over profile targets
            "Não quero excluir meu perfil.",
            "Je ne veux pas supprimer mon profil.",
            "プロフィールを削除しないでください。",
            "제 프로필을 삭제하지 마세요.",
            "Actualicen mi perfil, por favor.",
        ]
        for t in yes:
            self.assertTrue(classify(t).authorizes_deletion, t)
        for t in no:
            self.assertFalse(classify(t).authorizes_deletion, t)


# ---------------------------------------------------------------------------
# Authored scenarios on synthetic datasets
# ---------------------------------------------------------------------------

IDENTITIES = [
    ("user_901", "Rhea Sol", "rhea.sol@example.org", "+44 7700 900901", "a" * 64),
    ("user_902", "Rhea Solberg", "rhea.solberg@example.org", "+44 7700 900902", "b" * 64),
    ("user_903", "Ivo Brandt", "ivo.brandt@example.org", "+49 151 0000 9903", "c" * 64),
    ("user_904", "Ivo Brandt", "ivo.b.other@example.net", "+49 151 0000 9904", "e" * 64),   # same name
    ("user_905", "Mara Quint", "mara.quint@example.org", "+31 6 0000 9905", "f" * 64),
]
AUDIT = [
    # user_901 changed email; the old address and old hash remain in older systems
    ("audit_9001", "user_901", "2026-03-01T10:00:00Z", "email", "rhea.old@example.org", "rhea.sol@example.org"),
    ("audit_9002", "user_901", "2026-03-01T10:00:00Z", "email_hash", "d" * 64, "a" * 64),
]


def _write_csv(path: Path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


class Scenario:
    """Build a tiny dataset directory with the real rule table and given evidence."""

    def __init__(self, requests, evidence, activity=()):
        require_dataset(("deletion_rules.csv",))   # scenarios use the official rule table
        self.dir = Path(tempfile.mkdtemp(prefix="ghost_audit_test_"))
        (self.dir / "evidence" / "records").mkdir(parents=True)
        (self.dir / "evidence" / "logs").mkdir(parents=True)
        (self.dir / "evidence" / "databases").mkdir(parents=True)
        shutil.copy(DATASET / "deletion_rules.csv", self.dir / "deletion_rules.csv")
        _write_csv(self.dir / "user_identities.csv", ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
                   [r + ("2026-01-01T00:00:00Z",) for r in IDENTITIES])
        _write_csv(self.dir / "user_audit_log.csv",
                   ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"], AUDIT)
        _write_csv(self.dir / "system_activity.csv",
                   ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id",
                    "reported_result", "details"], list(activity))
        manifest = []
        for fid, rel, captured, content in evidence:
            (self.dir / rel).write_text(content, encoding="utf-8")
            manifest.append((fid, rel, "test_origin", captured))
        _write_csv(self.dir / "evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], manifest)
        _write_csv(self.dir / "deletion_requests.csv",
                   ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"],
                   requests)

    def run(self):
        results, _, _ = run(self.dir, None, Config())
        return {r.case_id: (r.status, json.loads(r.steps_json)) for r in results}

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def _bundle(captured, snapshots):
    return json.dumps({"exported_at": captured, "source": "records_assurance_service", "snapshots": [
        {"captured_at": captured, "data_store": ds, "collection": coll, "records": recs}
        for ds, coll, recs in snapshots]})


def _simple(steps):
    return [(s["phase"], s["system_id"], s["resource_id"], s["operation"], s["evidence_id"]) for s in steps]


DELETE_TEXT = "Please permanently delete the account associated with this email address."


class AuthoredScenarios(unittest.TestCase):
    def test_s1_post_cutoff_evidence_is_ignored(self):
        """Absent in-window; a later file shows the profile again. Answer must reflect audit_at."""
        in_window = _bundle("2026-05-10T12:00:00Z", [
            ("account_service", "customer_profiles", []),
            ("search_service", "customer_search_documents", []),
            ("security_service", "security_audit_events", [
                {"resource_id": "audit_record_9901", "subject_reference": "user_903",
                 "event_name": "account_deletion_reviewed"}]),
        ])
        log = ("2026-05-10T11:00:00Z level=DEBUG service=account_service trace_id=tr-1 "
               "event=profile_lookup_started lookup_key=ivo.brandt@example.org\n"
               "2026-05-10T11:00:01Z level=INFO service=account_service trace_id=tr-1 resource_id=account_9903 "
               "event=profile_lookup_completed rows=0 http_status=404\n")
        later = _bundle("2026-05-20T12:00:00Z", [
            ("account_service", "customer_profiles", [
                {"resource_id": "account_9903", "customer_reference": "user_903", "full_name": "Ivo Brandt",
                 "email": "ivo.brandt@example.org", "phone": "+49 151 0000 9903", "account_status": "ACTIVE"}])])
        sc = Scenario(
            [("case_t1", "ivo.brandt@example.org", "2026-05-01T00:00:00Z", "COMPLETE", "2026-05-02T00:00:00Z",
              "2026-05-12T00:00:00Z", DELETE_TEXT)],
            [("evidence_t1", "evidence/records/bundle_in_window.json", "2026-05-10T12:00:00Z", in_window),
             ("evidence_t2", "evidence/logs/lookup.log", "2026-05-10T11:30:00Z", log),
             ("evidence_t3", "evidence/records/bundle_later.json", "2026-05-20T12:00:00Z", later)])
        try:
            status, steps = sc.run()["case_t1"]
        finally:
            sc.close()
        self.assertEqual(status, "COMPLETE")
        self.assertEqual(_simple(steps), [
            ("VERIFICATION", "account_service", "account_9903", "VERIFY_RECORD_ABSENT", "evidence_t2"),
            ("VERIFICATION", "security_service", "audit_record_9901", "VERIFY_RECORD_PRESENT", "evidence_t1"),
        ])

    def test_s2_historical_identifiers_and_lookalike_decoy(self):
        """Old email (uppercase) + old hash link records; a lookalike and a name-only match must not."""
        snap = _bundle("2026-06-10T09:00:00Z", [
            ("analytics_platform", "customer_analytics_reports", [
                {"resource_id": "analytics_9911", "user_id": None, "email": None, "phone": None,
                 "email_hash": "d" * 64}]),
            ("search_service", "customer_search_documents", [
                {"resource_id": "search_9912", "document": {"display_name": None, "email": "RHEA.OLD@EXAMPLE.ORG",
                                                              "customer_reference": None}},
                {"resource_id": "search_9913", "document": {"display_name": None,
                                                              "email": "rhea.old.jr@example.org",
                                                              "customer_reference": None}},
                {"resource_id": "search_9914", "document": {"display_name": "Rhea Sol", "email": None,
                                                              "customer_reference": None}},
                {"resource_id": "search_9915", "document": {"display_name": "Rhea Solberg",
                                                              "email": "rhea.solberg@example.org",
                                                              "customer_reference": "user_902"}}]),
        ])
        sc = Scenario(
            [("case_t2", "RHEA.OLD@EXAMPLE.ORG", "2026-06-01T00:00:00Z", "COMPLETE", "2026-06-02T00:00:00Z",
              "2026-06-15T00:00:00Z", "Please erase the account linked to this email address.")],
            [("evidence_t4", "evidence/records/bundle.json", "2026-06-10T09:00:00Z", snap)])
        try:
            status, steps = sc.run()["case_t2"]
        finally:
            sc.close()
        self.assertEqual(status, "INCOMPLETE")
        self.assertEqual(_simple(steps), [
            ("FINDING", "search_service", "search_9912", "REPORT_REMAINING_RECORD", "evidence_t4"),
            ("FINDING", "analytics_platform", "analytics_9911", "REPORT_EXPOSED_PII", "evidence_t4"),
            ("REMEDIATION", "search_service", "search_9912", "DELETE_RECORD", ""),
            ("REMEDIATION", "analytics_platform", "analytics_9911", "ANONYMIZE_RECORD", ""),
            ("VERIFICATION", "search_service", "search_9912", "VERIFY_RECORD_ABSENT", ""),
            ("VERIFICATION", "analytics_platform", "analytics_9911", "VERIFY_RECORD_ANONYMIZED", ""),
        ])

    def test_s3_replica_drift_flows_restore_shared_support_tombstone(self):
        """Replica-only profile, live connector, restorable backup, shared support thread, exposed tombstone."""
        sql = """BEGIN TRANSACTION;
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL,
  collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE customer_data_read_replica (resource_id TEXT PRIMARY KEY, origin_system TEXT NOT NULL,
  collection_name TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO customer_data_read_replica VALUES('account_9921','account_service','customer_profiles',
  '{"resource_id":"account_9921","customer_reference":"user_901","full_name":"Rhea Sol","email":"rhea.sol@example.org","phone":"+44 7700 900901","account_status":"ACTIVE"}','2026-07-10T10:00:00Z');
INSERT INTO customer_data_primary VALUES('data_flow_9922','integration_service','connector_configurations',
  '{"resource_id":"data_flow_9922","connector_name":"c","destination":"warehouse","subject_selector":"user_901","enabled":true,"mode":"continuous_sync"}','2026-07-10T10:00:00Z');
INSERT INTO customer_data_primary VALUES('backup_9923','backup_service','backup_catalog',
  '{"resource_id":"backup_9923","archive_path":"s3://b/x.tar","subject_reference":"user_901","restore_allowed":true,"encryption":"AES-256"}','2026-07-10T10:00:00Z');
INSERT INTO customer_data_primary VALUES('support_9924','support_platform','support_conversations',
  '{"resource_id":"support_9924","ticket_id":"CS-1","subject_reference":"user_901","channel":"email","message_text":"Agent note: please send the update to rhea.sol@example.org for order ORD-1."}','2026-07-10T10:00:00Z');
INSERT INTO customer_data_primary VALUES('tombstone_9925','identity_service','deleted_customer_references',
  '{"resource_id":"tombstone_9925","subject_reference":"user_901","name":"Rhea Sol","email":"rhea.sol@example.org","phone":null,"email_hash":null,"account_status":"DELETED"}','2026-07-10T10:00:00Z');
COMMIT;"""
        sc = Scenario(
            [("case_t3", "rhea.sol@example.org", "2026-07-01T00:00:00Z", "COMPLETE", "2026-07-02T00:00:00Z",
              "2026-07-15T00:00:00Z", "I want to close this account and have its associated personal data deleted.")],
            [("evidence_t5", "evidence/databases/replica.sql", "2026-07-10T10:00:00Z", sql)])
        try:
            status, steps = sc.run()["case_t3"]
        finally:
            sc.close()
        self.assertEqual(status, "INCOMPLETE")
        ev = "evidence_t5"
        self.assertEqual(_simple(steps), [
            ("FINDING", "account_service", "account_9921", "REPORT_REMAINING_RECORD", ev),
            ("FINDING", "identity_service", "tombstone_9925", "REPORT_EXPOSED_PII", ev),
            ("FINDING", "support_platform", "support_9924", "REPORT_EXPOSED_PII", ev),
            ("FINDING", "integration_service", "data_flow_9922", "REPORT_ACTIVE_DATA_FLOW", ev),
            ("FINDING", "backup_service", "backup_9923", "REPORT_RESTORE_RISK", ev),
            ("REMEDIATION", "integration_service", "data_flow_9922", "DISABLE_DATA_FLOW", ""),
            ("REMEDIATION", "backup_service", "backup_9923", "BLOCK_RESTORE", ""),
            ("REMEDIATION", "account_service", "account_9921", "DELETE_RECORD", ""),
            ("REMEDIATION", "identity_service", "tombstone_9925", "REDACT_RECORD", ""),
            ("REMEDIATION", "support_platform", "support_9924", "REDACT_RECORD", ""),
            ("VERIFICATION", "account_service", "account_9921", "VERIFY_RECORD_ABSENT", ""),
            ("VERIFICATION", "identity_service", "tombstone_9925", "VERIFY_RECORD_REDACTED", ""),
            ("VERIFICATION", "support_platform", "support_9924", "VERIFY_RECORD_REDACTED", ""),
            ("VERIFICATION", "integration_service", "data_flow_9922", "VERIFY_DATA_FLOW_DISABLED", ""),
            ("VERIFICATION", "backup_service", "backup_9923", "VERIFY_RESTORE_BLOCKED", ""),
        ])

    def test_s4_compliant_tombstone_and_attachment_vs_message(self):
        """Clean tombstone keeps only the stable reference; an attachment is deleted, not redacted."""
        snap = _bundle("2026-08-10T09:00:00Z", [
            ("identity_service", "deleted_customer_references", [
                {"resource_id": "tombstone_9931", "subject_reference": "user_903", "name": None, "email": None,
                 "phone": None, "email_hash": None, "account_status": "DELETED"}]),
            ("support_platform", "ticket_attachments", [
                {"resource_id": "support_9932", "ticket_id": "CS-2", "file_name": "id_scan.pdf",
                 "uploaded_by": "ivo.brandt@example.org", "storage_class": "ticket_attachment"}]),
        ])
        sc = Scenario(
            [("case_t4", "ivo.brandt@example.org", "2026-08-01T00:00:00Z", "COMPLETE", "2026-08-02T00:00:00Z",
              "2026-08-15T00:00:00Z", DELETE_TEXT),
             ("case_t5", "ivo.brandt@example.org", "2026-08-01T00:00:00Z", "COMPLETE", "2026-08-02T00:00:00Z",
              "2026-08-15T00:00:00Z", "Please stop the promotional emails. I am not asking for deletion.")],
            [("evidence_t6", "evidence/records/bundle.json", "2026-08-10T09:00:00Z", snap)])
        try:
            out = sc.run()
        finally:
            sc.close()
        status, steps = out["case_t4"]
        self.assertEqual(status, "INCOMPLETE")
        self.assertEqual(_simple(steps), [
            ("FINDING", "support_platform", "support_9932", "REPORT_REMAINING_RECORD", "evidence_t6"),
            ("REMEDIATION", "support_platform", "support_9932", "DELETE_RECORD", ""),
            ("VERIFICATION", "support_platform", "support_9932", "VERIFY_RECORD_ABSENT", ""),
            ("VERIFICATION", "identity_service", "tombstone_9931", "VERIFY_RECORD_REDACTED", ""),
        ])
        self.assertEqual(out["case_t5"], ("NOT_APPLICABLE", []))

    def test_duplicate_evidence_does_not_duplicate_steps(self):
        snap = _bundle("2026-06-10T09:00:00Z", [
            ("search_service", "customer_search_documents", [
                {"resource_id": "search_9941", "document": {"display_name": "Ivo Brandt",
                                                              "email": "ivo.brandt@example.org",
                                                              "customer_reference": "user_903"}}])])
        sc = Scenario(
            [("case_t6", "ivo.brandt@example.org", "2026-06-01T00:00:00Z", "COMPLETE", "2026-06-02T00:00:00Z",
              "2026-06-15T00:00:00Z", DELETE_TEXT)],
            [("evidence_t7", "evidence/records/a.json", "2026-06-10T09:00:00Z", snap),
             ("evidence_t8", "evidence/records/b.json", "2026-06-10T09:00:00Z", snap)])
        try:
            status, steps = sc.run()["case_t6"]
        finally:
            sc.close()
        self.assertEqual(status, "INCOMPLETE")
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["evidence_id"], "evidence_t8")  # deterministic tie-break: highest file_id


class HiddenLikeFixtures(unittest.TestCase):
    """Sprint 2 adversarial fixtures. Expected answers follow deletion_rules.csv and the contract."""

    REQ = staticmethod(lambda cid, email, text=DELETE_TEXT: (
        cid, email, "2026-04-01T00:00:00Z", "COMPLETE", "2026-04-02T00:00:00Z", "2026-04-20T00:00:00Z", text))

    def _run(self, requests, evidence, activity=()):
        sc = Scenario(requests, evidence, activity)
        try:
            return sc.run()
        finally:
            sc.close()

    def test_legal_hold_and_security_retention_conditions(self):
        """Active hold / in-period audit record -> VERIFY_RECORD_PRESENT; ended -> no retention obligation."""
        snap = _bundle("2026-04-10T09:00:00Z", [
            ("legal_service", "retained_order_records", [
                {"resource_id": "legal_hold_9971", "subject_reference": "user_903", "order_reference": "ORD-1",
                 "retention_reason": "payment_dispute", "retained_until": "2033-12-31T23:59:59Z"},
                {"resource_id": "legal_hold_9972", "subject_reference": "user_905", "order_reference": "ORD-2",
                 "retention_reason": "payment_dispute", "retained_until": "2026-03-31T23:59:59Z"}]),
            ("search_service", "customer_search_documents", [
                {"resource_id": "search_9973", "document": {"email": "mara.quint@example.org"}},
                {"resource_id": "search_9974", "document": {"email": "ivo.brandt@example.org"}}]),
        ])
        log = ("2026-04-10T08:00:00Z level=DEBUG service=security_service trace_id=tr-71 "
               "event=audit_record_query_started subject_ref=user_905\n"
               "2026-04-10T08:00:01Z level=INFO service=security_service trace_id=tr-71 resource_id=audit_record_9975 "
               "event=audit_record_read_completed rows=1 retention_until=2026-01-31T23:59:59Z\n"
               "2026-04-10T08:00:02Z level=DEBUG service=security_service trace_id=tr-72 "
               "event=audit_record_query_started subject_ref=user_903\n"
               "2026-04-10T08:00:03Z level=INFO service=security_service trace_id=tr-72 resource_id=audit_record_9976 "
               "event=audit_record_read_completed rows=1 retention_until=2031-12-31T23:59:59Z\n")
        out = self._run([self.REQ("case_h1", "ivo.brandt@example.org"), self.REQ("case_h2", "mara.quint@example.org")],
                        [("evidence_h1", "evidence/records/b.json", "2026-04-10T09:00:00Z", snap),
                         ("evidence_h2", "evidence/logs/sec.log", "2026-04-10T09:00:00Z", log)])
        h1 = {(s["system_id"], s["resource_id"], s["operation"]) for s in out["case_h1"][1]}
        self.assertIn(("legal_service", "legal_hold_9971", "VERIFY_RECORD_PRESENT"), h1)
        self.assertIn(("security_service", "audit_record_9976", "VERIFY_RECORD_PRESENT"), h1)
        h2 = {s["resource_id"] for s in out["case_h2"][1]}
        self.assertNotIn("legal_hold_9972", h2)       # hold ended before audit_at
        self.assertNotIn("audit_record_9975", h2)     # retention period ended
        self.assertIn("search_9973", h2)
        self.assertFalse(any(s["operation"] == "DELETE_RECORD" and s["resource_id"].startswith(("legal", "audit"))
                             for c in out.values() for s in c[1]))

    def test_marketing_contact_vs_attributable_analytics(self):
        """An opt-in contact record is a marketing profile (DELETE); an analytics row is ANONYMIZE."""
        log = ("2026-04-10T08:00:00Z level=DEBUG service=analytics_platform trace_id=tr-81 resource_id=analytics_9981 "
               "event=contact_lookup_completed rows=1 email_opt_in=true correlation_key=\"ivo.brandt@example.org\"\n")
        snap = _bundle("2026-04-10T09:00:00Z", [("analytics_platform", "customer_analytics_reports", [
            {"resource_id": "analytics_9982", "user_id": "user_903", "email": "ivo.brandt@example.org",
             "phone": None, "email_hash": None}])])
        out = self._run([self.REQ("case_h3", "ivo.brandt@example.org")],
                        [("evidence_h3", "evidence/logs/a.log", "2026-04-10T08:30:00Z", log),
                         ("evidence_h4", "evidence/records/b.json", "2026-04-10T09:00:00Z", snap)])
        ops = {(s["resource_id"], s["operation"]) for s in out["case_h3"][1]}
        self.assertIn(("analytics_9981", "DELETE_RECORD"), ops)
        self.assertIn(("analytics_9982", "ANONYMIZE_RECORD"), ops)

    def test_multiple_support_resources_treated_independently(self):
        snap = _bundle("2026-04-10T09:00:00Z", [
            ("support_platform", "support_conversations", [
                {"resource_id": "support_9991", "ticket_id": "CS-1", "subject_reference": "user_903",
                 "message_text": "Agent: order ORD-7 update sent to ivo.brandt@example.org"},
                {"resource_id": "support_9992", "ticket_id": "CS-2", "subject_reference": "user_903",
                 "message_text": "[REDACTED]"}]),
            ("support_platform", "ticket_attachments", [
                {"resource_id": "support_9993", "ticket_id": "CS-1", "file_name": "passport.pdf",
                 "uploaded_by": "ivo.brandt@example.org", "storage_class": "ticket_attachment"}]),
        ])
        out = self._run([self.REQ("case_h4", "ivo.brandt@example.org")],
                        [("evidence_h5", "evidence/records/b.json", "2026-04-10T09:00:00Z", snap)])
        got = {(s["phase"], s["resource_id"], s["operation"]) for s in out["case_h4"][1]}
        self.assertIn(("REMEDIATION", "support_9991", "REDACT_RECORD"), got)
        self.assertIn(("REMEDIATION", "support_9993", "DELETE_RECORD"), got)
        self.assertIn(("VERIFICATION", "support_9992", "VERIFY_RECORD_REDACTED"), got)
        self.assertNotIn(("REMEDIATION", "support_9992", "REDACT_RECORD"), got)

    def test_duplicate_name_and_contradicted_catalog_claim(self):
        """Same full name as another user never links; a catalog claim contradicted by shown identifiers is rejected."""
        snap = _bundle("2026-04-10T09:00:00Z", [
            ("search_service", "customer_search_documents", [
                {"resource_id": "search_9994", "document": {"display_name": "Ivo Brandt",
                                                              "email": "ivo.b.other@example.net"}},
                {"resource_id": "search_9995", "document": {"display_name": "Ivo Brandt", "email": None}},
                {"resource_id": "search_9996", "document": {"email": "ivo.brandt@example.org"}}]),
            ("marketing_platform", "audience_profiles", [
                {"resource_id": "marketing_9997", "full_name": "Someone Else", "email": "someone@else.example"}]),
        ])
        activity = [("activity_h1", "2026-04-09T00:00:00Z", "LINK_RESOURCE_IDENTITY", "data_catalog",
                     "marketing_platform", "marketing_9997", "SUCCESS",
                     "Data catalog linked marketing_9997 (marketing_9997) to ivo.brandt@example.org and user_903.")]
        out = self._run([self.REQ("case_h5", "ivo.brandt@example.org")],
                        [("evidence_h6", "evidence/records/b.json", "2026-04-10T09:00:00Z", snap)], activity)
        rids = {s["resource_id"] for s in out["case_h5"][1]}
        self.assertEqual(rids, {"search_9996"})

    def test_incomplete_inventory_does_not_prove_absence(self):
        """A replica row stays present when a later, different store's inventory is empty."""
        sql_replica = """BEGIN TRANSACTION;
CREATE TABLE customer_data_primary (resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT,
  record_json TEXT, updated_at TEXT);
CREATE TABLE customer_data_read_replica (resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT,
  record_json TEXT, updated_at TEXT);
INSERT INTO customer_data_read_replica VALUES('account_9998','account_service','customer_profiles',
  '{"resource_id":"account_9998","customer_reference":"user_905","email":"mara.quint@example.org"}','2026-04-05T10:00:00Z');
COMMIT;"""
        sql_inventory = """BEGIN TRANSACTION;
CREATE TABLE snapshot_scope (origin_system TEXT PRIMARY KEY, captured_at TEXT NOT NULL);
CREATE TABLE customer_profiles (resource_id TEXT PRIMARY KEY, customer_reference TEXT, full_name TEXT, email TEXT,
  phone TEXT, email_hash TEXT, account_status TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO snapshot_scope VALUES ('account_service', '2026-04-12T00:00:00Z');
COMMIT;"""
        out = self._run([self.REQ("case_h6", "mara.quint@example.org")],
                        [("evidence_h7", "evidence/databases/replica.sql", "2026-04-05T10:00:00Z", sql_replica),
                         ("evidence_h8", "evidence/databases/inventory.sql", "2026-04-12T00:00:00Z", sql_inventory)])
        status, steps = out["case_h6"]
        self.assertEqual(status, "INCOMPLETE")
        self.assertIn(("FINDING", "account_9998", "REPORT_REMAINING_RECORD", "evidence_h7"),
                      {(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in steps})

    def test_irrelevant_decoys_do_not_change_answer(self):
        base_snap = [("search_service", "customer_search_documents", [
            {"resource_id": "search_9999", "document": {"email": "ivo.brandt@example.org"}}])]
        decoys = [("analytics_platform", "customer_analytics_reports", [
            {"resource_id": "analytics_9990", "user_id": "user_905", "email": "mara.quint@example.org"}]),
            ("support_platform", "support_conversations", [
                {"resource_id": "support_9989", "subject_reference": "user_904",
                 "message_text": "Contact ivo.b.other@example.net about Ivo Brandt's order"}])]
        a = self._run([self.REQ("case_h7", "ivo.brandt@example.org")],
                      [("evidence_h9", "evidence/records/b.json", "2026-04-10T09:00:00Z",
                        _bundle("2026-04-10T09:00:00Z", base_snap))])
        b = self._run([self.REQ("case_h7", "ivo.brandt@example.org")],
                      [("evidence_h9", "evidence/records/b.json", "2026-04-10T09:00:00Z",
                        _bundle("2026-04-10T09:00:00Z", base_snap + decoys))])
        self.assertEqual(a, b)


class CategoryHistoryRule(unittest.TestCase):
    """Sprint 5: a record's category may come from admissible earlier evidence only to fill a gap."""

    @staticmethod
    def _ob(fid, cap, cat, exists=True):
        from ghost_audit.model import Observation
        return Observation(file_id=fid, file_captured_at=cap, observed_at=cap, system="support_platform",
                           resource_id="support_1", store=f"s:{fid}", locator="x", exists=exists, category=cat,
                           link_values=frozenset({"user_903"}))

    def _state(self, obs):
        from ghost_audit.linking import Link
        from ghost_audit.planner import resolve_state
        return resolve_state("support_1", Link("support_1", "support_platform", "direct", []), obs, [],
                             "2026-05-01T00:00:00Z", "2026-05-20T00:00:00Z", set(), set())

    def test_history_fills_unknown_category(self):
        st = self._state([self._ob("f_in", "2026-05-10T00:00:00Z", None),
                          self._ob("f_pre", "2026-04-01T00:00:00Z", "SUPPORT_ATTACHMENT")])
        self.assertEqual((st.category, st.exists, st.evidence_id), ("SUPPORT_ATTACHMENT", True, "f_in"))

    def test_history_never_overrides_in_window_category(self):
        st = self._state([self._ob("f_in", "2026-05-10T00:00:00Z", "SUPPORT_MESSAGE"),
                          self._ob("f_pre", "2026-04-01T00:00:00Z", "SUPPORT_ATTACHMENT")])
        self.assertEqual(st.category, "SUPPORT_MESSAGE")

    def test_post_cutoff_category_never_used(self):
        st = self._state([self._ob("f_in", "2026-05-10T00:00:00Z", None),
                          self._ob("f_post", "2026-05-25T00:00:00Z", "SUPPORT_ATTACHMENT")])
        self.assertIsNone(st.category)


class WorkflowAttributionFixtures(unittest.TestCase):
    """Sprint 4: attribution of resources known only from the deletion workflow, scope proofs, timing."""

    def _run(self, requests, evidence, activity=()):
        sc = Scenario(requests, evidence, activity)
        try:
            return sc.run()
        finally:
            sc.close()

    @staticmethod
    def _req(cid, email, reported):
        return (cid, email, "2026-05-01T00:00:00Z", "COMPLETE", reported, "2026-05-20T00:00:00Z", DELETE_TEXT)

    @staticmethod
    def _act(aid, when, rid, target="account_service", etype="DELETE_RECORD"):
        return (aid, when, etype, "deletion_worker", target, rid, "SUCCESS", f"Deletion worker processed {rid}.")

    def _evidence(self):
        # Each subject has one retained security record (directly linked) and the account
        # store is captured after the workflow ran, enumerating no profiles.
        snap = _bundle("2026-05-10T12:00:00Z", [
            ("account_service", "customer_profiles", []),
            ("security_service", "security_audit_events", [
                {"resource_id": "audit_record_9801", "subject_reference": "user_903"},
                {"resource_id": "audit_record_9802", "subject_reference": "user_905"}]),
        ])
        return [("evidence_w1", "evidence/records/b.json", "2026-05-10T12:00:00Z", snap)]

    def _accounts(self, out, cid):
        return {s["resource_id"] for s in out[cid][1] if s["system_id"] == "account_service"}

    def test_unique_owner_is_assigned_with_scoped_absence_proof(self):
        out = self._run([self._req("case_w1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z"),
                         self._req("case_w2", "mara.quint@example.org", "2026-05-08T12:00:00Z")],
                        self._evidence(), [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9811")])
        self.assertEqual(out["case_w1"][0], "COMPLETE")
        self.assertIn(("VERIFICATION", "account_service", "account_9811", "VERIFY_RECORD_ABSENT", "evidence_w1"),
                      set(_simple(out["case_w1"][1])))
        self.assertEqual(self._accounts(out, "case_w2"), set())

    def test_one_resource_two_plausible_owners_is_not_assigned(self):
        out = self._run([self._req("case_w1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z"),
                         self._req("case_w2", "mara.quint@example.org", "2026-05-05T12:00:00Z")],
                        self._evidence(), [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9811")])
        self.assertEqual(self._accounts(out, "case_w1"), set())
        self.assertEqual(self._accounts(out, "case_w2"), set())

    def test_symmetric_matching_in_shared_slot_is_not_forced(self):
        out = self._run([self._req("case_w1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z"),
                         self._req("case_w2", "mara.quint@example.org", "2026-05-05T12:00:00Z")],
                        self._evidence(), [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9811"),
                                           self._act("activity_w2", "2026-05-05T11:05:00Z", "account_9812")])
        self.assertEqual(self._accounts(out, "case_w1") | self._accounts(out, "case_w2"), set())

    def test_absence_requires_scope_captured_after_the_action(self):
        # The only account snapshot predates the deletion action: absence is not proven,
        # so a COMPLETE plan cannot cite the account.
        snap = _bundle("2026-05-04T12:00:00Z", [("account_service", "customer_profiles", [])])
        sec = _bundle("2026-05-10T12:00:00Z", [("security_service", "security_audit_events", [
            {"resource_id": "audit_record_9801", "subject_reference": "user_903"}])])
        out = self._run([("case_w3", "ivo.brandt@example.org", "2026-05-01T00:00:00Z", "COMPLETE",
                          "2026-05-05T12:00:00Z", "2026-05-20T00:00:00Z", DELETE_TEXT)],
                        [("evidence_w2", "evidence/records/early.json", "2026-05-04T12:00:00Z", snap),
                         ("evidence_w3", "evidence/records/sec.json", "2026-05-10T12:00:00Z", sec)],
                        [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9811")])
        self.assertEqual(out["case_w3"][0], "COMPLETE")
        self.assertEqual(self._accounts(out, "case_w3"), set())

    def test_post_cutoff_file_with_earlier_internal_time_is_ignored(self):
        log = ("2026-05-15T10:00:00Z level=DEBUG service=search_service trace_id=tr-1 event=index_query_started "
               "match_key=ivo.brandt@example.org\n"
               "2026-05-15T10:00:01Z level=INFO service=search_service trace_id=tr-1 resource_id=search_9821 "
               "event=index_query_completed result=hit\n")
        sec = _bundle("2026-05-10T12:00:00Z", [("security_service", "security_audit_events", [
            {"resource_id": "audit_record_9801", "subject_reference": "user_903"}])])
        out = self._run([self._req("case_w4", "ivo.brandt@example.org", "2026-05-05T12:00:00Z")],
                        [("evidence_w4", "evidence/records/sec.json", "2026-05-10T12:00:00Z", sec),
                         ("evidence_w5", "evidence/logs/late_export.log", "2026-05-25T00:00:00Z", log)])
        self.assertNotIn("search_9821", {s["resource_id"] for s in out["case_w4"][1]})
        self.assertEqual(out["case_w4"][0], "COMPLETE")

    def test_explicit_reference_resolves_shared_slot_ambiguity(self):
        """Two requests share a slot; only one subject's search document names the account."""
        sql = """BEGIN TRANSACTION;
CREATE TABLE search_documents_primary (resource_id TEXT PRIMARY KEY, display_name TEXT, email TEXT,
  source_resource_id TEXT, indexed_at TEXT);
INSERT INTO search_documents_primary VALUES('search_9841','Ivo Brandt','ivo.brandt@example.org','account_9811',
  '2026-05-09T10:00:00Z');
COMMIT;"""
        evidence = self._evidence() + [("evidence_w8", "evidence/databases/search.sql", "2026-05-09T10:00:00Z", sql)]
        out = self._run([self._req("case_w1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z"),
                         self._req("case_w2", "mara.quint@example.org", "2026-05-05T12:00:00Z")],
                        evidence, [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9811")])
        self.assertEqual(self._accounts(out, "case_w1"), {"account_9811"})
        self.assertEqual(self._accounts(out, "case_w2"), set())

    def _colocated_export(self):
        # One export holds both subjects' direct records plus an identifier-less 404 lookup.
        return json.dumps({"exported_at": "2026-05-10T12:00:00Z", "region": "global", "service_exports": {
            "account_service": {"profile_lookups": [
                {"observed_at": "2026-05-10T09:00:00Z", "profile": None, "resource_id": "account_9851",
                 "status_code": 404}]},
            "security_service": {"audit_records": [
                {"observed_at": "2026-05-10T09:00:00Z", "resource_id": "audit_record_9852",
                 "subject_reference": "user_903", "event_code": "X", "retention_until": "2031-01-01T00:00:00Z"},
                {"observed_at": "2026-05-10T09:00:00Z", "resource_id": "audit_record_9853",
                 "subject_reference": "user_905", "event_code": "X", "retention_until": "2031-01-01T00:00:00Z"}]}}})

    def test_colocation_alone_does_not_assign_in_shared_slot(self):
        out = self._run([self._req("case_w1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z"),
                         self._req("case_w2", "mara.quint@example.org", "2026-05-05T12:00:00Z")],
                        [("evidence_w9", "evidence/records/export.json", "2026-05-10T12:00:00Z",
                          self._colocated_export())],
                        [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9851")])
        self.assertEqual(self._accounts(out, "case_w1") | self._accounts(out, "case_w2"), set())

    def test_colocated_row_assigned_when_only_one_window_explains_action(self):
        out = self._run([self._req("case_w1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z"),
                         self._req("case_w2", "mara.quint@example.org", "2026-05-08T12:00:00Z")],
                        [("evidence_w9", "evidence/records/export.json", "2026-05-10T12:00:00Z",
                          self._colocated_export())],
                        [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9851")])
        self.assertIn(("VERIFICATION", "account_service", "account_9851", "VERIFY_RECORD_ABSENT", "evidence_w9"),
                      set(_simple(out["case_w1"][1])))
        self.assertEqual(self._accounts(out, "case_w2"), set())

    def test_resource_in_multiple_files_uses_latest_in_window_state(self):
        early = _bundle("2026-05-06T09:00:00Z", [("search_service", "customer_search_documents", [
            {"resource_id": "search_9831", "document": {"email": "ivo.brandt@example.org"}}])])
        log = ("2026-05-12T10:00:00Z level=DEBUG service=search_service trace_id=tr-2 event=index_query_started "
               "match_key=ivo.brandt@example.org\n"
               "2026-05-12T10:00:01Z level=INFO service=search_service trace_id=tr-2 resource_id=search_9831 "
               "event=index_query_completed result=miss\n")
        out = self._run([self._req("case_w5", "ivo.brandt@example.org", "2026-05-05T12:00:00Z")],
                        [("evidence_w6", "evidence/records/early.json", "2026-05-06T09:00:00Z", early),
                         ("evidence_w7", "evidence/logs/later.log", "2026-05-12T11:00:00Z", log)])
        self.assertEqual(out["case_w5"], ("COMPLETE", [
            {"step_no": 1, "phase": "VERIFICATION", "system_id": "search_service", "resource_id": "search_9831",
             "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": "evidence_w7"}]))


class Sprint10Regressions(unittest.TestCase):
    """Reproduced defects D1-D4/D6 with their closest plausible false positives."""

    _run = WorkflowAttributionFixtures._run
    _req = staticmethod(WorkflowAttributionFixtures._req)
    _act = staticmethod(WorkflowAttributionFixtures._act)
    _evidence = WorkflowAttributionFixtures._evidence
    _accounts = WorkflowAttributionFixtures._accounts

    # D4 -----------------------------------------------------------------------
    def test_two_exclusive_candidates_of_one_category_are_not_picked_arbitrarily(self):
        out = self._run([self._req("case_w1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z")],
                        self._evidence(), [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9811"),
                                           self._act("activity_w2", "2026-05-05T11:10:00Z", "account_9812")])
        self.assertEqual(self._accounts(out, "case_w1"), set())

    def test_single_exclusive_candidate_is_still_assigned(self):
        out = self._run([self._req("case_w1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z")],
                        self._evidence(), [self._act("activity_w1", "2026-05-05T11:00:00Z", "account_9811")])
        self.assertEqual(self._accounts(out, "case_w1"), {"account_9811"})

    # D2 -----------------------------------------------------------------------
    def _catalog_case(self, email, user):
        snap = _bundle("2026-05-10T12:00:00Z", [
            ("search_service", "customer_search_documents", [
                {"resource_id": "search_9961", "document": {"display_name": None, "email": None}}]),
            ("security_service", "security_audit_events", [
                {"resource_id": "audit_record_9801", "subject_reference": "user_903"}])])
        claim = ("activity_c1", "2026-05-09T00:00:00Z", "LINK_RESOURCE_IDENTITY", "data_catalog", "search_service",
                 "search_9961", "SUCCESS", f"Data catalog linked search_9961 (search_9961) to {email} and {user}.")
        out = self._run([self._req("case_c1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z")],
                        [("evidence_c1", "evidence/records/b.json", "2026-05-10T12:00:00Z", snap)], [claim])
        return {s["resource_id"] for s in out["case_c1"][1]}

    def test_catalog_claim_with_contradictory_identifiers_is_rejected(self):
        self.assertNotIn("search_9961", self._catalog_case("ivo.brandt@example.org", "user_905"))
        self.assertNotIn("search_9961", self._catalog_case("mara.quint@example.org", "user_903"))

    def test_partial_catalog_claim_is_still_accepted(self):
        self.assertIn("search_9961", self._catalog_case("unknown.person@example.net", "user_903"))
        self.assertIn("search_9961", self._catalog_case("ivo.brandt@example.org", "user_903"))

    # D1b ----------------------------------------------------------------------
    @staticmethod
    def _sql(table, rows):
        body = "".join(f"INSERT INTO {table} VALUES('{rid}','account_service','customer_profiles',"
                       f"'{{\"resource_id\":\"{rid}\",\"customer_reference\":\"{ref}\"}}','{t}');\n"
                       for rid, ref, t in rows)
        return ("BEGIN TRANSACTION;\n"
                f"CREATE TABLE {table} (resource_id TEXT PRIMARY KEY, origin_system TEXT, collection_name TEXT, "
                "record_json TEXT, updated_at TEXT);\n" + body + "COMMIT;")

    def _replica_case(self, later_table):
        a = self._sql("customer_data_read_replica", [("account_9971", "user_903", "2026-05-06T10:00:00Z"),
                                                       ("account_9972", "user_905", "2026-05-06T10:00:00Z")])
        b = self._sql(later_table, [("account_9972", "user_905", "2026-05-12T10:00:00Z")])
        out = self._run([self._req("case_r1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z")],
                        [("evidence_r1", "evidence/databases/a.sql", "2026-05-06T10:00:00Z", a),
                         ("evidence_r2", "evidence/databases/b.sql", "2026-05-12T10:00:00Z", b)])
        return {(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in out["case_r1"][1]}

    def test_later_snapshot_of_same_table_and_collection_proves_absence(self):
        self.assertIn(("VERIFICATION", "account_9971", "VERIFY_RECORD_ABSENT", "evidence_r2"),
                      self._replica_case("customer_data_read_replica"))

    def test_later_snapshot_of_a_different_table_does_not_prove_absence(self):
        self.assertIn(("FINDING", "account_9971", "REPORT_REMAINING_RECORD", "evidence_r1"),
                      self._replica_case("customer_data_primary"))

    # D3 -----------------------------------------------------------------------
    def _simultaneous_case(self, lookup_time):
        snap = _bundle("2026-05-10T12:00:00Z", [("account_service", "customer_profiles", [
            {"resource_id": "account_9981", "customer_reference": "user_903", "email": "ivo.brandt@example.org"}])])
        log = (f"{lookup_time} level=DEBUG service=account_service trace_id=tr-9 event=profile_lookup_started "
               "lookup_key=ivo.brandt@example.org\n"
               f"{lookup_time} level=INFO service=account_service trace_id=tr-9 resource_id=account_9981 "
               "event=profile_lookup_completed rows=0 http_status=404\n")
        out = self._run([self._req("case_s1", "ivo.brandt@example.org", "2026-05-05T12:00:00Z")],
                        [("evidence_s1", "evidence/records/snap.json", "2026-05-10T12:00:00Z", snap),
                         ("evidence_s2", "evidence/logs/lookup.log", "2026-05-10T12:00:00Z", log)])
        return {(s["phase"], s["resource_id"], s["operation"], s["evidence_id"]) for s in out["case_s1"][1]}

    def test_simultaneous_observations_from_two_files_are_aggregated(self):
        # Replica copy (snapshot) and primary lookup observed at the same instant: still present.
        self.assertIn(("FINDING", "account_9981", "REPORT_REMAINING_RECORD", "evidence_s1"),
                      self._simultaneous_case("2026-05-10T12:00:00Z"))

    def test_strictly_later_lookup_still_decides(self):
        self.assertIn(("VERIFICATION", "account_9981", "VERIFY_RECORD_ABSENT", "evidence_s2"),
                      self._simultaneous_case("2026-05-11T09:00:00Z"))

    # D6 -----------------------------------------------------------------------
    def test_phone_recognizer_positives_and_adversarial_negatives(self):
        from ghost_audit.evidence import PHONE_TEXT_RE
        from ghost_audit.loaders import norm_token
        for txt in ("call +1 (202) 555-0103 today", "+81 90 6156 0005", "+44 7700 900016", "+55 11 96954-2024"):
            self.assertTrue(PHONE_TEXT_RE.search(txt), txt)
        for txt in ("2026-09-15T11:51:46Z", "case_33", "account_1234", "rb-075713-004", "1,284 events",
                    "4b065493f4403c03ee3cd593b75b03cb6085134a7acb5c5c63ed71d3da346c77", "cursor=884019",
                    "ORD-20264827", "order 20261260"):
            self.assertIsNone(PHONE_TEXT_RE.search(txt), txt)
            self.assertFalse(norm_token(txt).startswith("tel:"), txt)
        self.assertEqual(norm_token("+81 90 6156 0005"), norm_token("+819061560005"))


class CapacityOneScope(unittest.TestCase):
    """Only USER_PROFILE and ANALYTICS_PROFILE are one-per-subject; support and search records are not.

    Corpus basis: no request's content-linked resources include two profiles of one kind, while five
    include two or more support resources. Elimination by "this request is already full" is therefore
    valid only for the two profile categories."""

    T0 = datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc)

    def cand(self, rid, cat, minutes=0, observed=True):
        from ghost_audit.linking import Link
        return {"link": Link(rid, "svc", "workflow", []), "times": [self.T0 + timedelta(minutes=minutes)],
                "category": cat, "observed": observed}

    @staticmethod
    def solve(cands, held=None, anchors=None):
        from ghost_audit.linking import resolve_workflow
        out = resolve_workflow(cands, held or {}, anchors or {}, 5.0)
        return {c: sorted(r) for c, r in sorted(out.items()) if r}

    def test_two_anonymous_support_records_of_one_request_are_both_assigned(self):
        cs = {"c": {"support_1": self.cand("support_1", "SUPPORT_ATTACHMENT"),
                    "support_2": self.cand("support_2", "SUPPORT_ATTACHMENT", 5)}}
        self.assertEqual(self.solve(cs), {"c": ["support_1", "support_2"]})

    def test_two_anonymous_search_entries_of_one_request_are_both_assigned(self):
        cs = {"c": {"search_1": self.cand("search_1", "SEARCH_INDEX_ENTRY"),
                    "search_2": self.cand("search_2", "SEARCH_INDEX_ENTRY", 3)}}
        self.assertEqual(self.solve(cs), {"c": ["search_1", "search_2"]})

    def test_direct_support_record_does_not_block_an_anonymous_one(self):
        cs = {"c": {"support_3": self.cand("support_3", "SUPPORT_ATTACHMENT")}}
        self.assertEqual(self.solve(cs, {"c": {"SUPPORT_ATTACHMENT"}}), {"c": ["support_3"]})

    def test_holding_a_support_record_does_not_eliminate_a_competing_request(self):
        cs = {k: {"support_x": self.cand("support_x", "SUPPORT_ATTACHMENT")} for k in ("c", "d")}
        self.assertEqual(self.solve(cs, {"d": {"SUPPORT_ATTACHMENT"}}), {})

    def test_profiles_keep_capacity_one(self):
        two = {"c": {"analytics_1": self.cand("analytics_1", "ANALYTICS_PROFILE"),
                     "analytics_2": self.cand("analytics_2", "ANALYTICS_PROFILE", 5)}}
        self.assertEqual(self.solve(two), {})
        contested = {k: {"account_x": self.cand("account_x", "USER_PROFILE")} for k in ("c", "d")}
        self.assertEqual(self.solve(contested, {"d": {"USER_PROFILE"}}), {"c": ["account_x"]})
        self.assertEqual(self.solve({"c": {"account_9": self.cand("account_9", "USER_PROFILE")}},
                                    {"c": {"USER_PROFILE"}}), {})

    def test_activity_only_candidates_stay_one_per_request(self):
        # Challenger counterexample: three search entries known only from worker activity. With
        # --activity-only extended to search, these were other people's records (two failed deletions).
        cs = {"c": {f"search_{i}": self.cand(f"search_{i}", "SEARCH_INDEX_ENTRY", 5 * i, observed=False)
                    for i in range(1, 4)}}
        self.assertEqual(self.solve(cs), {})
        held = {"c": {"support_8": self.cand("support_8", "SUPPORT_ATTACHMENT", observed=False)}}
        self.assertEqual(self.solve(held, {"c": {"SUPPORT_ATTACHMENT"}}), {})

    def test_unknown_category_stays_one_per_request(self):
        cs = {"c": {"object_1": self.cand("object_1", None), "object_2": self.cand("object_2", None, 4)}}
        self.assertEqual(self.solve(cs), {})

    def named(self, cands, mapping, **kw):
        """Solve under a bijective renaming with reversed input order, mapped back."""
        renamed = {c: {mapping[r]: inf for r, inf in reversed(list(rs.items()))} for c, rs in reversed(list(cands.items()))}
        back = {v: k for k, v in mapping.items()}
        return {c: sorted(back[r] for r in rs) for c, rs in self.solve(renamed, **kw).items()}

    def test_observed_records_assigned_and_activity_only_one_excluded_whatever_the_names(self):
        # Challenger counterexample against the first amendment: the outcome depended on which observed
        # entry sorted first. Observed co-located entries are assigned; the activity-only entry is not,
        # because the request then already holds a search record.
        base = {"c": {"search_1": self.cand("search_1", "SEARCH_INDEX_ENTRY"),
                      "search_2": self.cand("search_2", "SEARCH_INDEX_ENTRY"),
                      "search_3": self.cand("search_3", "SEARCH_INDEX_ENTRY", observed=False)}}
        self.assertEqual(self.solve(base), {"c": ["search_1", "search_2"]})
        for order in (("search_3", "search_1", "search_2"), ("search_2", "search_3", "search_1")):
            self.assertEqual(self.named(base, dict(zip(sorted(base["c"]), order))), {"c": ["search_1", "search_2"]})

    def test_mixed_component_is_independent_of_names(self):
        # Fuzz-found shape: observed and activity-only support candidates shared by two requests.
        T = "SUPPORT_ATTACHMENT"
        cs = {"a": {"r0": self.cand("r0", T, 1), "r2": self.cand("r2", T, 45, False), "r4": self.cand("r4", T, 20, False)},
              "b": {"r1": self.cand("r1", T, 1), "r2": self.cand("r2", T, 45, False), "r3": self.cand("r3", T, 45),
                    "r4": self.cand("r4", T, 20, False)}}
        base = self.solve(cs)
        rids = ["r0", "r1", "r2", "r3", "r4"]
        for k in range(1, 5):
            self.assertEqual(self.named(cs, dict(zip(rids, rids[k:] + rids[:k]))), base, k)

    def test_resource_with_mixed_limited_status_is_independent_of_names(self):
        # Reviewer's third counterexample: r1 and r4 are observed for c2 but activity-only for c1/c0,
        # and r2 has an unknown category. Such resources stay one-per-request as a whole.
        S = "SEARCH_INDEX_ENTRY"
        spec = {"r0": {"c2": (S, False)}, "r1": {"c1": (S, False), "c2": (S, True)}, "r2": {"c0": (None, True)},
                "r3": {"c0": (S, False)}, "r4": {"c0": (S, False), "c1": (S, False), "c2": (S, True)}}
        held = {"c1": {S}, "c2": {S}}
        outcomes = set()
        for shift in range(5):
            rn = {f"r{i}": f"q{(i + shift) % 5}" for i in range(5)}
            cn = {f"c{i}": f"k{(i + shift) % 3}" for i in range(3)}
            cands = {}
            for r, per in spec.items():
                for cid, (cat, obs) in per.items():
                    cands.setdefault(cn[cid], {})[rn[r]] = self.cand(rn[r], cat, observed=obs)
            out = self.solve(cands, {cn[k]: v for k, v in held.items()})
            rb, cb = {v: k for k, v in rn.items()}, {v: k for k, v in cn.items()}
            outcomes.add(repr(sorted((cb[k], sorted(rb[x] for x in v)) for k, v in out.items())))
        self.assertEqual(outcomes, {repr([("c0", ["r2"])])})

    def test_multiple_support_assignment_is_independent_of_names_and_input_order(self):
        base = {"c": {"support_1": self.cand("support_1", "SUPPORT_ATTACHMENT"),
                      "support_2": self.cand("support_2", "SUPPORT_ATTACHMENT", 5)},
                "d": {"account_7": self.cand("account_7", "USER_PROFILE")}}
        mapping = {"support_1": "support_9", "support_2": "support_0", "account_7": "account_7"}
        renamed = {c: {mapping[r]: inf for r, inf in reversed(list(rs.items()))} for c, rs in reversed(list(base.items()))}
        back = {v: k for k, v in mapping.items()}
        self.assertEqual({c: sorted(back[r] for r in rs) for c, rs in self.solve(renamed).items()}, self.solve(base))

    def test_end_to_end_identifier_less_support_tickets_are_attributed(self):
        """Real export shape: the subject's ticket plus two tickets with every identifier null that the
        deletion worker acted on in the subject's window, all in one file."""
        def ticket(rid, email=None):
            return {"customer_reference": None, "message_excerpt": "Customer supplied contact details.",
                    "observed_at": "2026-05-10T09:00:00Z", "requester_email": email, "requester_name": None,
                    "requester_phone": None, "resource_id": rid}
        export = json.dumps({"exported_at": "2026-05-10T12:00:00Z", "region": "global", "service_exports": {
            "support_platform": {"tickets": [ticket("support_9990", "ivo.brandt@example.org"), ticket("support_9991"),
                                             ticket("support_9992")]}}})
        req = ("case_k1", "ivo.brandt@example.org", "2026-05-01T00:00:00Z", "COMPLETE", "2026-05-05T12:00:00Z",
               "2026-05-20T00:00:00Z", DELETE_TEXT)
        act = [(f"activity_k{i}", f"2026-05-05T11:0{i}:00Z", "DELETE_RECORD", "deletion_worker", "support_platform",
                rid, "SUCCESS", f"Deletion worker processed {rid}.") for i, rid in ((1, "support_9991"), (2, "support_9992"))]
        sc = Scenario([req], [("evidence_k1", "evidence/records/export.json", "2026-05-10T12:00:00Z", export)], act)
        try:
            status, steps = sc.run()["case_k1"]
        finally:
            sc.close()
        self.assertEqual({s["resource_id"] for s in steps}, {"support_9990", "support_9991", "support_9992"})
        self.assertEqual({s["evidence_id"] for s in steps if s["phase"] == "FINDING"}, {"evidence_k1"})


def _consume_dataset_arg(argv):
    """Support `--dataset PATH` before handing argv to unittest."""
    global DATASET
    if "--dataset" in argv:
        i = argv.index("--dataset")
        DATASET = Path(argv[i + 1]).resolve()
        os.environ["GHOST_AUDIT_DATASET"] = str(DATASET)   # subprocess checks inherit it
        del argv[i:i + 2]
    return argv


if __name__ == "__main__":
    unittest.main(verbosity=2, argv=_consume_dataset_arg(list(sys.argv)))

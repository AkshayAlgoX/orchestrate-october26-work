"""All-or-nothing publication of output.csv.

Run:  python3 code/tests/test_publication.py [--dataset /path/to/dataset]

Reproduced defects: an existing valid output was overwritten by a degraded run (exit 0, later
exit 5), by a run with contract-invalid rows (exit 1), and was corrupted by an I/O error part-way
through writing. Every failed or degraded run must now leave the destination exactly as it was,
create nothing when there was no output, and leave no temporary files behind.
Synthetic scenarios need the official deletion_rules.csv; the real-data test needs the dataset.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
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
from ghost_audit import output as output_mod  # noqa: E402
from ghost_audit.audit import Auditor  # noqa: E402
from ghost_audit.output import PublicationError, publish_csv, serialize_csv, validate_csv_file  # noqa: E402

IVO = "ivo.brandt@example.org"
REQ = ("case_p1", IVO, "2026-05-01T00:00:00Z", "COMPLETE", "2026-05-05T12:00:00Z", "2026-05-20T00:00:00Z",
       tp.DELETE_TEXT)
CAPTURED = "2026-05-10T12:00:00Z"
BUNDLE = tp._bundle(CAPTURED, [
    ("search_service", "customer_search_documents", [{"resource_id": "search_9901", "document": {"email": IVO}}]),
    ("security_service", "security_audit_events", [{"resource_id": "audit_record_9801",
                                                    "subject_reference": "user_903"}]),
])
EVIDENCE = [("evidence_p1", "evidence/records/p.json", CAPTURED, BUNDLE)]
PREVIOUS = "case_id,deletion_status,steps_json\ncase_p1,NOT_APPLICABLE,[]\n"     # an earlier valid answer


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublicationHarness(unittest.TestCase):
    def setUp(self):
        sc = tp.Scenario([REQ], EVIDENCE)
        self.addCleanup(sc.close)
        self.dataset = sc.dir
        self.out_dir = Path(tempfile.mkdtemp(prefix="ghost_publish_"))
        self.addCleanup(shutil.rmtree, self.out_dir, ignore_errors=True)
        self.dest = self.out_dir / "output.csv"

    def existing(self):
        self.dest.write_text(PREVIOUS, encoding="utf-8")
        return digest(self.dest)

    def invoke(self, *flags, dataset=None):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = cli.main(["--dataset", str(dataset or self.dataset), "--output", str(self.dest), *flags])
        return rc, err.getvalue()

    def leftovers(self):
        return sorted(p.name for p in self.out_dir.iterdir() if p.name != "output.csv")

    def assert_untouched(self, before, rc, expected_rc):
        self.assertEqual(rc, expected_rc)
        self.assertEqual(digest(self.dest), before)
        self.assertEqual(self.leftovers(), [])

    def assert_nothing_created(self, rc, expected_rc):
        self.assertEqual(rc, expected_rc)
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.leftovers(), [])


def kernel_fault(resource):
    original = Auditor.resource_state

    def faulty(self, req, rid, *a, **k):
        if rid == resource:
            raise ValueError("malformed observation")
        return original(self, req, rid, *a, **k)
    return mock.patch.object(Auditor, "resource_state", faulty)


class SuccessPath(PublicationHarness):
    def test_complete_run_replaces_an_existing_output_with_valid_incomplete_predictions(self):
        before = self.existing()
        rc, err = self.invoke()
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(digest(self.dest), before)
        text = self.dest.read_text(encoding="utf-8")
        self.assertIn("case_p1,INCOMPLETE,", text)             # an ordinary challenge status, published
        self.assertEqual(validate_csv_file(self.dest, ["case_p1"]), [])
        self.assertEqual(self.leftovers(), [])

    def test_existing_file_permissions_are_kept(self):
        self.existing()
        os.chmod(self.dest, 0o640)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o640)


class FailedRunsNeverPublish(PublicationHarness):
    def test_injected_resource_failure(self):
        before = self.existing()
        with kernel_fault("audit_record_9801"):
            rc, err = self.invoke()
        self.assert_untouched(before, rc, 5)
        self.assertIn("no output was published", err)

    def test_injected_resource_failure_without_existing_output(self):
        with kernel_fault("audit_record_9801"):
            rc, _ = self.invoke()
        self.assert_nothing_created(rc, 5)

    def test_budget_exhaustion(self):
        before = self.existing()
        rc, err = self.invoke("--max-queries", "2")
        self.assert_untouched(before, rc, 5)
        self.assertIn("query budget exhausted", err)

    def test_contract_invalid_row(self):
        before = self.existing()
        original = agent_mod.AuditAgent.investigate_case

        def invalid(agent, req):
            r = original(agent, req)
            r.status = "COMPLETE"      # COMPLETE with FINDING steps violates the contract
            return r
        with mock.patch.object(agent_mod.AuditAgent, "investigate_case", invalid):
            rc, err = self.invoke()
        self.assert_untouched(before, rc, 1)
        self.assertIn("contract self-check error", err)

    def test_malformed_configuration(self):
        before = self.existing()
        bad = self.out_dir / "bad_config.json"
        bad.write_text("{not json", encoding="utf-8")
        rc, err = self.invoke("--config", str(bad))
        bad.unlink()
        self.assert_untouched(before, rc, 2)

    def test_malformed_input_file(self):
        before = self.existing()
        (self.dataset / "system_activity.csv").write_text("activity_id,event_time\n", encoding="utf-8")
        rc, err = self.invoke()
        self.assert_untouched(before, rc, 2)
        self.assertIn("missing required column", err)

    def test_unreadable_evidence(self):
        before = self.existing()
        (self.dataset / "evidence/records/p.json").write_text('{"snapshots": [', encoding="utf-8")
        rc, _ = self.invoke()
        self.assert_untouched(before, rc, 3)


class PublicationFailures(PublicationHarness):
    def test_serialization_failure_part_way_through_writing(self):
        before = self.existing()
        with mock.patch.object(output_mod.os, "fsync", side_effect=OSError("disk full")):
            rc, err = self.invoke()
        self.assert_untouched(before, rc, 6)
        self.assertIn("could not be published", err)

    def test_rename_failure(self):
        before = self.existing()
        with mock.patch.object(output_mod.os, "replace", side_effect=OSError("read-only file system")):
            rc, _ = self.invoke()
        self.assert_untouched(before, rc, 6)

    def test_validation_failure_immediately_before_publication(self):
        before = self.existing()
        real = output_mod.serialize_csv

        def drops_a_row(rows, case_order="natural"):
            return real(rows[:-1], case_order)            # the written bytes no longer match the requests
        with mock.patch.object(output_mod, "serialize_csv", drops_a_row):
            rc, err = self.invoke()
        self.assert_untouched(before, rc, 6)
        self.assertIn("failed verification", err)

    def test_validation_failure_without_existing_output(self):
        with mock.patch.object(output_mod, "validate_csv_file", return_value=["corrupt"]):
            rc, _ = self.invoke()
        self.assert_nothing_created(rc, 6)

    def test_missing_destination_directory_is_rejected_before_running(self):
        self.dest = self.out_dir / "no_such_dir" / "output.csv"
        rc, err = self.invoke()
        self.assertEqual(rc, 2)
        self.assertFalse(self.dest.exists())
        self.assertIn("does not exist", err)

    def test_non_oserror_during_publication(self):
        before = self.existing()
        with mock.patch.object(output_mod, "serialize_csv", side_effect=UnicodeEncodeError("utf-8", "x", 0, 1, "bad")):
            rc, err = self.invoke()
        self.assert_untouched(before, rc, 6)


class ReviewerCounterexamples(PublicationHarness):
    """Independent review findings against the first version of this change."""

    def test_trace_and_output_on_the_same_path_are_refused(self):
        before = self.existing()
        rc, err = self.invoke("--max-queries", "2", "--trace", str(self.dest))
        self.assert_untouched(before, rc, 2)
        self.assertIn("must be different files", err)

    def test_same_path_through_a_symlink_is_refused(self):
        before = self.existing()
        alias = self.out_dir / "alias.csv"
        alias.symlink_to(self.dest)
        rc, _ = self.invoke("--trace", str(alias))
        self.assertEqual(rc, 2)
        self.assertEqual(digest(self.dest), before)

    def test_missing_trace_directory_is_rejected_before_running(self):
        before = self.existing()
        rc, err = self.invoke("--trace", str(self.out_dir / "nodir" / "t.jsonl"))
        self.assert_untouched(before, rc, 2)

    def test_trace_write_failure_publishes_nothing(self):
        before = self.existing()
        with mock.patch.object(cli, "publish_text", side_effect=OSError("quota exceeded")):
            rc, err = self.invoke("--trace", str(self.out_dir / "t.jsonl"))
        self.assert_untouched(before, rc, 6)
        self.assertIn("trace", err)

    def test_symlinked_destination_keeps_the_link_and_updates_the_target(self):
        target = self.out_dir / "real_output.csv"
        target.write_text(PREVIOUS, encoding="utf-8")
        self.dest.symlink_to(target)
        rc, _ = self.invoke()
        self.assertEqual(rc, 0)
        self.assertTrue(self.dest.is_symlink())
        self.assertIn("case_p1,INCOMPLETE,", target.read_text(encoding="utf-8"))

    def test_symlinked_destination_untouched_on_failure(self):
        target = self.out_dir / "real_output.csv"
        target.write_text(PREVIOUS, encoding="utf-8")
        before = digest(target)
        self.dest.symlink_to(target)
        rc, _ = self.invoke("--max-queries", "2")
        self.assertEqual(rc, 5)
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(digest(target), before)
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()), ["output.csv", "real_output.csv"])

    def test_non_regular_destination_is_still_supported(self):
        self.dest = Path(os.devnull)
        rc, err = self.invoke()
        self.assertEqual(rc, 0, err)

    def test_failed_write_to_a_non_regular_destination_is_not_called_unchanged(self):
        if not Path("/dev/full").exists():
            self.skipTest("/dev/full not available on this platform")
        self.dest = Path("/dev/full")
        rc, err = self.invoke()
        self.assertEqual(rc, 6)
        self.assertIn("may hold a partial write", err)
        self.assertNotIn("is unchanged", err)

    def test_publish_csv_rejects_contract_invalid_rows_directly(self):
        self.existing()
        before = digest(self.dest)
        with self.assertRaises(PublicationError):
            publish_csv(self.dest, [("case_p1", "COMPLETE", '[{"bad": 1}]')])
        self.assertEqual(digest(self.dest), before)
        self.assertEqual(self.leftovers(), [])


class ExplicitOverrides(PublicationHarness):
    def test_incomplete_output_published_only_on_request_and_never_as_success(self):
        before = self.existing()
        with kernel_fault("audit_record_9801"):
            rc, err = self.invoke("--allow-incomplete-output")
        self.assertEqual(rc, 5)
        self.assertNotEqual(digest(self.dest), before)
        self.assertIn("published on request", err)

    def test_degraded_evidence_run_still_publishes_with_status_4(self):
        (self.dataset / "evidence/records/extra.json").write_text("{", encoding="utf-8")
        with open(self.dataset / "evidence_files.csv", "a", encoding="utf-8") as fh:
            fh.write(f"evidence_p2,evidence/records/extra.json,test_origin,{CAPTURED}\n")
        rc, _ = self.invoke("--allow-unparsed-evidence")
        self.assertEqual(rc, 4)
        self.assertTrue(self.dest.is_file())


class RealDataPublication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tp.require_dataset()

    def test_published_bytes_equal_the_serialized_rows(self):
        from ghost_audit.audit import run
        results, ds, _ = run(tp.DATASET)
        rows = [(r.case_id, r.status, r.steps_json) for r in results]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "output.csv"
            publish_csv(dest, rows, expected_ids=[r["case_id"] for r in ds.requests])
            self.assertEqual(dest.read_text(encoding="utf-8"), serialize_csv(rows))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["output.csv"])


if __name__ == "__main__":
    argv = list(sys.argv)
    if "--dataset" in argv:
        i = argv.index("--dataset")
        tp.DATASET = Path(argv[i + 1]).resolve()
        os.environ["GHOST_AUDIT_DATASET"] = str(tp.DATASET)
        del argv[i:i + 2]
    unittest.main(verbosity=2, argv=argv)

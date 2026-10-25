"""Fail-closed handling of evidence that cannot be read, and of malformed inputs.

Run:  python3 code/tests/test_evidence_gate.py [--dataset /path/to/dataset]

An unread evidence file can hide remaining personal data and turn an INCOMPLETE case into a
false COMPLETE (reproduced: without pdftotext, case_33 became COMPLETE with [] and the run
exited 0). The CLI must therefore never write output or report success in that state.
Synthetic scenarios need the official deletion_rules.csv; the real-data test needs the dataset.
"""

from __future__ import annotations

import contextlib
import csv
import io
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
from ghost_audit import evidence  # noqa: E402
from ghost_audit.evidence import load_evidence  # noqa: E402
from ghost_audit.loaders import load_dataset  # noqa: E402

SENTINEL = "previous output\n"
GOOD = tp._bundle("2026-05-10T12:00:00Z", [("security_service", "security_audit_events", [
    {"resource_id": "audit_record_9801", "subject_reference": "user_903"}])])
REQ = ("case_g1", "ivo.brandt@example.org", "2026-05-01T00:00:00Z", "COMPLETE", "2026-05-05T12:00:00Z",
       "2026-05-20T00:00:00Z", tp.DELETE_TEXT)


def rows(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def invoke(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class SyntheticGate(unittest.TestCase):
    def scenario(self, extra=()):
        sc = tp.Scenario([REQ], [("evidence_g1", "evidence/records/good.json", "2026-05-10T12:00:00Z", GOOD)]
                         + list(extra))
        self.addCleanup(sc.close)
        self.out = sc.dir / "out.csv"
        self.out.write_text(SENTINEL, encoding="utf-8")
        return sc.dir

    def run_cli(self, ds, *flags):
        return invoke(["--dataset", str(ds), "--output", str(self.out), *flags])

    def assert_blocked(self, ds, file_id, status):
        rc, _, err = self.run_cli(ds)
        self.assertEqual(rc, 3, err)
        self.assertEqual(self.out.read_text(encoding="utf-8"), SENTINEL)   # previous output untouched
        self.assertIn(f"evidence {file_id}", err)
        self.assertIn(status, err)
        self.assertIn("no output was written", err)

    def test_clean_run_succeeds(self):
        ds = self.scenario()
        rc, _, err = self.run_cli(ds)
        self.assertEqual(rc, 0, err)
        self.assertNotEqual(self.out.read_text(encoding="utf-8"), SENTINEL)
        self.assertEqual(load_evidence(ds, load_dataset(ds).manifest).gaps(), [])

    def test_malformed_json_blocks_output(self):
        self.assert_blocked(self.scenario([("evidence_g2", "evidence/records/bad.json", "2026-05-10T12:00:00Z",
                                            '{"snapshots": [')]), "evidence_g2", "error:")

    def test_missing_evidence_file_blocks_output(self):
        ds = self.scenario([("evidence_g2", "evidence/logs/gone.log", "2026-05-10T12:00:00Z", "x\n")])
        (ds / "evidence/logs/gone.log").unlink()
        self.assert_blocked(ds, "evidence_g2", "missing")

    def test_corrupt_pdf_blocks_output(self):
        if shutil.which("pdftotext") is None:
            self.skipTest("pdftotext not installed; covered by test_missing_pdftotext_blocks_output")
        self.assert_blocked(self.scenario([("evidence_g2", "evidence/records/scan.pdf", "2026-05-10T12:00:00Z",
                                            "not a pdf")]), "evidence_g2", "unreadable")

    def test_missing_pdftotext_blocks_output(self):
        ds = self.scenario([("evidence_g2", "evidence/records/scan.pdf", "2026-05-10T12:00:00Z", "%PDF-1.4")])
        with mock.patch.object(evidence.shutil, "which", return_value=None), \
                mock.patch.object(evidence, "pdf_text", return_value=None):
            self.assert_blocked(ds, "evidence_g2", "pdftotext is not installed")

    def test_unverified_image_blocks_output(self):
        self.assert_blocked(self.scenario([("evidence_g2", "evidence/records/screen.png", "2026-05-10T12:00:00Z",
                                            "not the transcribed image")]), "evidence_g2", "image")

    def test_unsupported_evidence_format_blocks_output(self):
        self.assert_blocked(self.scenario([("evidence_g2", "evidence/records/sheet.xlsx", "2026-05-10T12:00:00Z",
                                            "x")]), "evidence_g2", "unsupported")

    def test_explicit_degraded_run_writes_output_but_never_reports_success(self):
        ds = self.scenario([("evidence_g2", "evidence/records/bad.json", "2026-05-10T12:00:00Z", "{")])
        rc, _, err = self.run_cli(ds, "--allow-unparsed-evidence")
        self.assertEqual(rc, 4)
        self.assertNotEqual(self.out.read_text(encoding="utf-8"), SENTINEL)
        self.assertIn("DEGRADED RUN", err)

    def test_missing_required_column_is_rejected_before_running(self):
        ds = self.scenario()
        path = ds / "deletion_requests.csv"
        with open(path, encoding="utf-8", newline="") as fh:
            rows = [r[:5] + r[6:] for r in csv.reader(fh)]       # drop audit_at
        with open(path, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
        rc, _, err = self.run_cli(ds)
        self.assertEqual(rc, 2)
        self.assertIn("audit_at", err)
        self.assertEqual(self.out.read_text(encoding="utf-8"), SENTINEL)

    def test_missing_requests_file_is_rejected(self):
        ds = self.scenario()
        rc, _, err = self.run_cli(ds, "--requests", str(ds / "nope.csv"))
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", err)


class RealDataGate(unittest.TestCase):
    """The reproduced defect: without pdftotext the 4 PDFs were silently skipped and case_33 changed."""

    @classmethod
    def setUpClass(cls):
        tp.require_dataset()
        cls.tmp = Path(tempfile.mkdtemp(prefix="ghost_gate_"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_real_dataset_has_no_evidence_gaps(self):
        ds = load_dataset(tp.DATASET)
        self.assertEqual([m.file_id for m in load_evidence(tp.DATASET, ds.manifest).gaps()], [])

    def test_missing_pdftotext_fails_closed_on_real_data(self):
        out = self.tmp / "out.csv"
        out.write_text(SENTINEL, encoding="utf-8")
        with mock.patch.object(evidence.shutil, "which", return_value=None), \
                mock.patch.object(evidence, "pdf_text", return_value=None):
            rc, _, err = invoke(["--dataset", str(tp.DATASET), "--output", str(out)])
            self.assertEqual(rc, 3)
            self.assertEqual(out.read_text(encoding="utf-8"), SENTINEL)
            self.assertEqual(err.count("pdftotext is not installed"), 4)
            rc, _, err = invoke(["--dataset", str(tp.DATASET), "--output", str(out), "--allow-unparsed-evidence"])
        self.assertEqual(rc, 4)
        degraded = rows(out)
        normal = self.tmp / "normal.csv"
        self.assertEqual(invoke(["--dataset", str(tp.DATASET), "--output", str(normal)])[0], 0)
        full = rows(normal)
        self.assertEqual([c for c in full if full[c] != degraded[c]], ["case_33"])   # the hidden change


if __name__ == "__main__":
    argv = list(sys.argv)
    if "--dataset" in argv:
        i = argv.index("--dataset")
        tp.DATASET = Path(argv[i + 1]).resolve()
        os.environ["GHOST_AUDIT_DATASET"] = str(tp.DATASET)
        del argv[i:i + 2]
    unittest.main(verbosity=2, argv=argv)

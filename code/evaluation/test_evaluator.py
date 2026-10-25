"""Regression tests for the public-sample evaluator's metric formulas."""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from .evaluator import SampleEvaluator
except ImportError:
    from evaluator import SampleEvaluator

def _gold() -> Path:
    """Public sample answers, resolved at test time (GHOST_AUDIT_DATASET or repo dataset/)."""
    base = Path(os.environ.get("GHOST_AUDIT_DATASET") or Path(__file__).resolve().parent.parent.parent / "dataset")
    return base / "sample_output.csv"


def _csv(rows):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["case_id", "deletion_status", "steps_json"])
    for cid, status, steps in rows:
        w.writerow([cid, status, json.dumps(steps)])
    return out.getvalue()


class TestEvaluateCommandFailsClosed(unittest.TestCase):
    """Without the public answers the evaluate command must not print 100% metrics and exit 0."""

    def run_evaluate(self, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            pred = Path(tmp) / "pred.csv"
            pred.write_text(_csv([("case_01", "COMPLETE", [])]), encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "GHOST_AUDIT_DATASET"}
            env["GHOST_AUDIT_DATASET"] = str(Path(tmp) / "no_dataset")
            res = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve().parent / "main.py"), "evaluate",
                                  str(pred), *extra], cwd=tmp, env=env, capture_output=True, text=True)
        return res

    def test_missing_golden_path_is_an_error(self):
        res = self.run_evaluate("--golden", "/nonexistent/sample_output.csv")
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertIn("not found", res.stderr)
        self.assertNotIn("Step Precision", res.stdout)

    def test_unresolvable_default_golden_is_an_error(self):
        if (Path(__file__).resolve().parent.parent.parent / "dataset" / "sample_output.csv").is_file():
            self.skipTest("a repository dataset/ is present, so the default resolution succeeds")
        res = self.run_evaluate()
        self.assertEqual(res.returncode, 2, res.stdout)
        self.assertNotIn("Step Precision", res.stdout)


class TestEvaluatorFormulas(unittest.TestCase):
    def setUp(self):
        gold = _gold()
        if not gold.is_file():
            self.skipTest(f"public sample answers not available at {gold}; "
                          "pass --dataset PATH or set GHOST_AUDIT_DATASET")
        with open(gold, encoding="utf-8") as fh:
            self.rows = [(r["case_id"], r["deletion_status"], json.loads(r["steps_json"]))
                         for r in csv.DictReader(fh)]
        self.ev = SampleEvaluator(golden_path=gold)

    def test_gold_against_itself_is_perfect(self):
        rep = self.ev.evaluate_content(_csv(self.rows))
        self.assertEqual(rep.status_accuracy, 1.0)
        self.assertEqual(rep.exact_array_accuracy, 1.0)
        self.assertEqual((rep.step_precision, rep.step_recall), (1.0, 1.0))
        self.assertEqual((rep.resource_precision, rep.resource_recall), (1.0, 1.0))
        self.assertEqual(rep.citation_agreement, 1.0)
        self.assertEqual(rep.cases_with_order_errors, 0)

    def test_inversion_detected_in_partially_matching_case(self):
        rows = []
        for cid, status, steps in self.rows:
            if cid == "case_11":
                steps = steps[:-1]
                steps[0], steps[1] = steps[1], steps[0]
                for i, s in enumerate(steps, 1):
                    s["step_no"] = i
            rows.append((cid, status, steps))
        rep = self.ev.evaluate_content(_csv(rows))
        self.assertEqual(rep.cases_with_order_errors, 1)
        self.assertEqual(rep.total_order_inversions, 1)
        self.assertEqual(rep.fn_steps, 1)


if __name__ == "__main__":
    unittest.main()

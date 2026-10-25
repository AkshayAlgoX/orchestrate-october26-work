"""CLI entry point for output contract validation and public-sample evaluation.

Usage examples:
  # Validate root output.csv
  python3 code/evaluation/main.py validate output.csv

  # Validate dataset/sample_output.csv against sample requests
  python3 code/evaluation/main.py validate dataset/sample_output.csv --requests dataset/sample_deletion_requests.csv

  # Evaluate predictions against public samples
  python3 code/evaluation/main.py evaluate my_predictions.csv

  # Run full test suite (validator tests + edge-case contract tests)
  python3 code/evaluation/main.py test
"""

import argparse
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the source tree / code.zip

# Add current directory to path
CURR_DIR = Path(__file__).resolve().parent
if str(CURR_DIR) not in sys.path:
    sys.path.insert(0, str(CURR_DIR))

try:
    from .validator import OutputValidator
    from .evaluator import SampleEvaluator
    from .test_validator import TestOutputValidator
    from .test_edge_cases import TestContractEdgeCases
    from .test_evaluator import TestEvaluateCommandFailsClosed, TestEvaluatorFormulas
except ImportError:
    from validator import OutputValidator
    from evaluator import SampleEvaluator
    from test_validator import TestOutputValidator
    from test_edge_cases import TestContractEdgeCases
    from test_evaluator import TestEvaluateCommandFailsClosed, TestEvaluatorFormulas


def cmd_validate(args: argparse.Namespace) -> int:
    """Execute output contract validation."""
    target_path = Path(args.file)
    if not target_path.exists():
        print(f"[ERROR] Target file does not exist: {target_path}", file=sys.stderr)
        return 1

    expected_cases = None
    if args.requests:
        req_path = Path(args.requests)
        if req_path.is_file():
            import csv
            with open(req_path, "r", encoding="utf-8") as f:
                expected_cases = [r["case_id"] for r in csv.DictReader(f) if "case_id" in r]

    validator = OutputValidator(
        dataset_dir=args.dataset_dir,
        expected_case_ids=expected_cases,
        enforce_requested_at_bound=args.enforce_requested_at_bound,
    )

    result = validator.validate_file(target_path)
    print(result.summary())
    return 0 if result.is_valid else 1


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Execute public-sample evaluation."""
    target_path = Path(args.predictions)
    if not target_path.exists():
        print(f"[ERROR] Prediction file does not exist: {target_path}", file=sys.stderr)
        return 1

    evaluator = SampleEvaluator(golden_path=args.golden)
    if not evaluator.golden_cases:
        where = args.golden or "sample_output.csv ($GHOST_AUDIT_DATASET, ./dataset or the repository's dataset/)"
        print(f"[ERROR] Public sample answers not found or empty: {where}. No metrics are reported without them; "
              f"pass --golden PATH or set GHOST_AUDIT_DATASET.", file=sys.stderr)
        return 2
    report = evaluator.evaluate_file(target_path)

    if args.markdown:
        print(report.to_markdown())
    else:
        print("=" * 70)
        print("PUBLIC SAMPLE EVALUATION (32 CASES)")
        print("[Note: Public-sample performance does NOT guarantee hidden test scores]")
        print("=" * 70)
        print(f"Status Accuracy:       {report.status_accuracy:.2%} ({report.status_matches}/{report.total_cases})")
        print(f"Exact-Array Accuracy:  {report.exact_array_accuracy:.2%} ({report.exact_array_matches}/{report.total_cases})")
        print(f"Step Precision:        {report.step_precision:.2%} (TP={report.tp_steps}, FP={report.fp_steps})")
        print(f"Step Recall:           {report.step_recall:.2%} (TP={report.tp_steps}, FN={report.fn_steps})")
        print(f"Step F1 Score:         {report.step_f1:.2%}")
        print(f"Citation Agreement:    {report.citation_agreement:.2%} ({report.citation_matches}/{report.total_cited_steps})")
        print(f"Sequence Order Errors: {report.cases_with_order_errors} cases ({report.total_order_inversions} inversions)")
        print(f"Resource Precision:    {report.resource_precision:.2%} (TP={report.resource_tp}, FP={report.resource_fp})")
        print(f"Resource Recall:       {report.resource_recall:.2%} (FN={report.resource_fn})")
        la = report.loose_aligned or 1
        print(f"Loose-aligned op/ev:   {report.loose_operation_matches / la:.2%} / {report.loose_evidence_matches / la:.2%} "
              f"({report.loose_aligned} steps aligned on phase+system+resource)")
        print("-" * 70)
        print("Field-level Accuracy (aligned steps):")
        fstats = report.field_stats
        print(f"  phase:        {fstats.accuracy(fstats.phase_matches):.2%}")
        print(f"  operation:    {fstats.accuracy(fstats.operation_matches):.2%}")
        print(f"  state:        {fstats.accuracy(fstats.state_matches):.2%}")
        print(f"  system_id:    {fstats.accuracy(fstats.system_id_matches):.2%}")
        print(f"  resource_id:  {fstats.accuracy(fstats.resource_id_matches):.2%}")
        print(f"  evidence_id:  {fstats.accuracy(fstats.evidence_id_matches):.2%}")
        print("=" * 70)
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Run all validation and contract edge-case unit tests."""
    if getattr(args, "dataset", None):
        import os
        os.environ["GHOST_AUDIT_DATASET"] = str(Path(args.dataset).resolve())
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestOutputValidator))
    suite.addTests(loader.loadTestsFromTestCase(TestContractEdgeCases))
    suite.addTests(loader.loadTestsFromTestCase(TestEvaluatorFormulas))
    suite.addTests(loader.loadTestsFromTestCase(TestEvaluateCommandFailsClosed))

    runner = unittest.TextTestRunner(verbosity=2)
    res = runner.run(suite)
    return 0 if res.wasSuccessful() else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ghost Data Still Exists: Contract Validator and Public Sample Evaluator"
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # Validate subcommand
    p_val = subparsers.add_parser("validate", help="Validate an output CSV against the contract")
    p_val.add_argument("file", help="Path to output CSV file to validate")
    p_val.add_argument("--requests", default=None, help="Path to request CSV (e.g. dataset/deletion_requests.csv)")
    p_val.add_argument("--dataset-dir", default=None, help="Path to dataset directory")
    p_val.add_argument(
        "--enforce-requested-at-bound",
        action="store_true",
        default=False,
        help="Disputed check: enforce that cited evidence must be captured at or after requested_at",
    )

    # Evaluate subcommand
    p_eval = subparsers.add_parser("evaluate", help="Evaluate predictions against sample_output.csv")
    p_eval.add_argument("predictions", help="Path to predictions CSV to evaluate")
    p_eval.add_argument("--golden", default=None, help="Path to golden sample_output.csv")
    p_eval.add_argument("--markdown", action="store_true", help="Output report in Markdown format")

    # Test subcommand
    p_test = subparsers.add_parser("test", help="Run validator, edge-case and evaluator-formula tests")
    p_test.add_argument("--dataset", default=None,
                        help="participant dataset directory (else $GHOST_AUDIT_DATASET or ./dataset); "
                             "dataset-dependent tests are skipped, not failed, when it is unavailable")

    return parser


def main() -> None:
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    # Allow direct validation invocation: python3 main.py <file.csv>
    if len(sys.argv) == 2 and not sys.argv[1].startswith("-") and sys.argv[1] not in ("validate", "evaluate", "test"):
        args = argparse.Namespace(
            subcommand="validate",
            file=sys.argv[1],
            requests=None,
            dataset_dir=None,
            enforce_requested_at_bound=False,
        )
        sys.exit(cmd_validate(args))

    args = parser.parse_args()
    if args.subcommand == "validate":
        sys.exit(cmd_validate(args))
    elif args.subcommand == "evaluate":
        sys.exit(cmd_evaluate(args))
    elif args.subcommand == "test":
        sys.exit(cmd_test(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

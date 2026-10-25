"""Ghost Data Still Exists - deletion-audit agent (terminal entry point).

Usage:
  python3 code/main.py                                  # 518 requests -> ./output.csv
  python3 code/main.py --requests dataset/sample_deletion_requests.csv --output predictions/sample_predictions.csv
  python3 code/main.py --trace traces.jsonl            # also write per-case reasoning traces
  python3 code/main.py --inventory                     # print evidence parse inventory
  python3 code/main.py --list-tools                    # print the investigation tool contracts (JSON)

The run is deterministic and makes no model/API calls.

Exit status: 0 success; 1 contract self-check errors; 2 missing input file or column;
3 evidence that could not be read (nothing is written); 4 degraded run written because
--allow-unparsed-evidence was given (never reported as success); 5 at least one case was
not fully investigated (query budget, failed tool, unresolved state); 6 the output could not be
written and verified. Output is published all-or-nothing: on statuses 1, 3, 5 and 6 an existing
output file is left unchanged and no new one is created (5 publishes only with
--allow-incomplete-output; 4 publishes because --allow-unparsed-evidence was given).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit.audit import Config, run  # noqa: E402
from ghost_audit.loaders import REQUEST_COLUMNS, REQUIRED_COLUMNS, missing_columns  # noqa: E402
from ghost_audit.output import PublicationError, check_row, publish_csv, publish_text  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=os.environ.get("GHOST_AUDIT_DATASET") or str(ROOT / "dataset"),
                    help="participant dataset directory (default: $GHOST_AUDIT_DATASET or ./dataset)")
    ap.add_argument("--requests", default=None, help="requests CSV (default: dataset/deletion_requests.csv)")
    ap.add_argument("--output", default=str(ROOT / "output.csv"))
    ap.add_argument("--trace", default=None, help="optional JSONL file with per-case traces")
    ap.add_argument("--config", default=None,
                    help="path to JSON configuration file (default: code/config/audit_config.json if present)")
    ap.add_argument("--window", choices=["requested_at", "none"], default="requested_at")
    ap.add_argument("--no-workflow-links", action="store_true")
    ap.add_argument("--proximity-margin", type=float, default=Config.proximity_margin_min,
                    help="minutes for the nearest-activity tie-break (negative disables)")
    ap.add_argument("--identity-lookback", action="store_true")
    ap.add_argument("--ambiguous", choices=["drop", "all"], default="drop")
    ap.add_argument("--post-cutoff-retention", action="store_true",
                    help="EXPERIMENT: see Config.post_cutoff_retention")
    ap.add_argument("--activity-only", default=",".join(Config.activity_only_categories),
                    help="comma-separated categories allowed from workflow activity alone, e.g. USER_PROFILE")
    ap.add_argument("--strict-colocation", action="store_true",
                    help="require identifier-less resources to share a file with the subject's direct links")
    ap.add_argument("--no-reference-links", action="store_true",
                    help="do not follow explicit record-to-record references (e.g. source_resource_id)")
    ap.add_argument("--case-order", choices=["natural", "lexicographic"], default="natural",
                    help="row order interpretation of 'sorted by case_id' (default: natural, as in the input file)")
    ap.add_argument("--engine", choices=["agent", "pipeline"], default="agent",
                    help="agent: bounded tool-based investigation per case (default); pipeline: reference single pass")
    ap.add_argument("--max-queries", type=int, default=256, help="per-case investigation query budget")
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("--allow-unparsed-evidence", action="store_true",
                    help="write output even if some evidence files could not be read (exit status 4)")
    ap.add_argument("--allow-incomplete-output", action="store_true",
                    help="publish output even if some cases were not fully investigated (exit status 5)")
    ap.add_argument("--list-tools", action="store_true", help="print the investigation tool contracts and exit")
    args = ap.parse_args(argv)
    if args.list_tools:
        from ghost_audit.agent import describe_tools
        print(json.dumps(describe_tools(), indent=2))
        return 0

    dataset_dir = Path(args.dataset)
    required = ["evidence_files.csv", "user_identities.csv", "user_audit_log.csv", "system_activity.csv",
                "deletion_rules.csv"] + ([] if args.requests else ["deletion_requests.csv"])
    missing = [f for f in required if not (dataset_dir / f).is_file()]
    if missing:
        print(f"ERROR: dataset directory {dataset_dir} is missing {missing}. Pass --dataset /path/to/dataset "
              f"or set GHOST_AUDIT_DATASET.", file=sys.stderr)
        return 2
    if args.requests and not Path(args.requests).is_file():
        print(f"ERROR: requests file {args.requests} does not exist.", file=sys.stderr)
        return 2
    tables = {dataset_dir / name: cols for name, cols in REQUIRED_COLUMNS.items()}
    for path in (Path(args.requests) if args.requests else None, dataset_dir / "deletion_requests.csv",
                 dataset_dir / "sample_deletion_requests.csv"):
        if path is not None and path.is_file():
            tables[path] = REQUEST_COLUMNS
    bad = [(p, missing_columns(p, cols)) for p, cols in tables.items()]
    bad = [(p, cols) for p, cols in bad if cols]
    for p, cols in bad:
        print(f"ERROR: {p} is missing required column(s) {cols}.", file=sys.stderr)
    if bad:
        return 2
    output_path = Path(os.path.realpath(args.output))
    problems = []
    if output_path.is_dir():
        problems.append(f"--output {args.output} is a directory")
    elif not output_path.parent.is_dir():
        problems.append(f"directory of --output {args.output} does not exist")
    if args.trace:
        trace_path = Path(os.path.realpath(args.trace))
        if trace_path == output_path:
            problems.append("--trace and --output must be different files")
        elif trace_path.is_dir() or not trace_path.parent.is_dir():
            problems.append(f"--trace {args.trace} is a directory or its directory does not exist")
    for p in problems:
        print(f"ERROR: {p}; nothing was run or written.", file=sys.stderr)
    if problems:
        return 2
    raw_argv = sys.argv[1:] if argv is None else argv
    cli_flags = {a.split("=")[0] for a in raw_argv if a.startswith("--")}

    config_path = Path(args.config) if args.config else (ROOT / "code" / "config" / "audit_config.json")
    if config_path.is_file():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
        except Exception as e:
            print(f"ERROR: could not read configuration file {config_path}: {e}", file=sys.stderr)
            return 2
    else:
        cfg_data = {}

    window = args.window if "--window" in cli_flags else cfg_data.get("window", "requested_at")
    workflow_links = (not args.no_workflow_links) if "--no-workflow-links" in cli_flags else cfg_data.get("workflow_links", True)
    if "--proximity-margin" in cli_flags:
        proximity_margin = args.proximity_margin if args.proximity_margin >= 0 else None
    else:
        proximity_margin = cfg_data.get("proximity_margin_min", Config.proximity_margin_min)
    identity_lookback = args.identity_lookback if "--identity-lookback" in cli_flags else cfg_data.get("identity_lookback", False)
    ambiguous = args.ambiguous if "--ambiguous" in cli_flags else cfg_data.get("ambiguous", "drop")
    post_cutoff_retention = args.post_cutoff_retention if "--post-cutoff-retention" in cli_flags else cfg_data.get("post_cutoff_retention", False)
    if "--activity-only" in cli_flags:
        activity_only = tuple(c for c in args.activity_only.split(",") if c)
    elif "activity_only_categories" in cfg_data:
        activity_only = tuple(cfg_data["activity_only_categories"])
    else:
        activity_only = Config.activity_only_categories
    relaxed_colocation = (not args.strict_colocation) if "--strict-colocation" in cli_flags else cfg_data.get("relaxed_colocation", True)
    reference_links = (not args.no_reference_links) if "--no-reference-links" in cli_flags else cfg_data.get("reference_links", True)

    engine = args.engine if "--engine" in cli_flags else cfg_data.get("engine", "agent")
    max_queries = args.max_queries if "--max-queries" in cli_flags else cfg_data.get("max_queries", 256)
    case_order = args.case_order if "--case-order" in cli_flags else cfg_data.get("case_order", "natural")

    cfg = Config(window=window, workflow_links=workflow_links,
                 proximity_margin_min=proximity_margin, identity_lookback=identity_lookback,
                 ambiguous=ambiguous, post_cutoff_retention=post_cutoff_retention,
                 activity_only_categories=activity_only,
                 relaxed_colocation=relaxed_colocation,
                 reference_links=reference_links)
    results, ds, ev = run(Path(args.dataset), Path(args.requests) if args.requests else None, cfg,
                          engine=engine, max_queries=max_queries)
    if args.inventory:
        for meta in ev.files.values():
            print(f"{meta.file_id}\t{meta.parse_status}\t{meta.observations}\t{meta.path.name}\t{meta.note}")
    gaps = ev.gaps()
    for meta in gaps:
        print(f"ERROR: evidence {meta.file_id} ({meta.path.name}) was not read: {meta.parse_status}"
              + (f" ({meta.note})" if meta.note else ""), file=sys.stderr)
    if gaps and not args.allow_unparsed_evidence:
        print(f"ERROR: {len(gaps)} evidence file(s) could not be read; no output was written. Unread evidence can "
              f"hide remaining personal data and turn an INCOMPLETE case into a false COMPLETE. Fix the input or "
              f"environment (e.g. install poppler-utils for pdftotext), or pass --allow-unparsed-evidence for a "
              f"degraded run (exit status 4).", file=sys.stderr)
        return 3
    rows, errors = [], []
    for r in results:
        errors += check_row(r.case_id, r.status, r.steps_json)
        rows.append((r.case_id, r.status, r.steps_json))
    if len({r[0] for r in rows}) != len(ds.requests):
        errors.append("row count mismatch")
    if args.trace:   # diagnostics are written whether or not the output is published, and before it
        try:
            publish_text(args.trace, "".join(json.dumps({"case_id": r.case_id, "status": r.status, **r.trace},
                                                        default=str) + "\n" for r in results))
        except Exception as exc:
            print(f"ERROR: trace {args.trace} could not be written: {exc}; no output was published "
                  f"({args.output} is unchanged).", file=sys.stderr)
            return 6
    budget = [r.case_id for r in results if r.trace.get("investigation", {}).get("stop_reason") == "query budget exhausted"]
    closed = [r.case_id for r in results if r.trace.get("fallback") == "failed_closed"]
    degraded = {r.case_id: sorted(r.trace["degraded"]["unresolved_states"]) for r in results
                if r.trace.get("degraded") and r.trace.get("fallback") != "failed_closed"}
    fell_back = [r.case_id for r in results if r.trace.get("fallback") == "reference_pipeline"]
    if budget:
        print(f"WARNING: query budget exhausted for {len(budget)} case(s) (INCOMPLETE, no steps): {budget[:10]}",
              file=sys.stderr)
    if closed:
        print(f"WARNING: {len(closed)} case(s) failed closed after a tool failure (INCOMPLETE, no steps): {closed[:10]}",
              file=sys.stderr)
    if degraded:
        print(f"WARNING: {len(degraded)} case(s) have resource states that could not be resolved; their rows omit "
              f"those resources and never claim COMPLETE: {dict(list(degraded.items())[:10])}", file=sys.stderr)
    if fell_back:
        print(f"WARNING: the investigation controller failed for {len(fell_back)} case(s); their rows come from the "
              f"re-validated reference pipeline: {fell_back[:10]}", file=sys.stderr)
    not_established = sorted(set(budget) | set(closed) | set(degraded))
    flagged = [r.case_id for r in results if r.trace.get("insufficient_evidence")]
    if flagged:
        print(f"WARNING: {len(flagged)} deletion case(s) have no admissible evidence (COMPLETE with []): {flagged}")
    cov = {}
    for r in results:
        for k, v in r.trace.get("coverage", {}).items():
            v = v.split("(")[0]
            cov[(k, v)] = cov.get((k, v), 0) + 1
    if cov:
        print("coverage (deletion cases): " + ", ".join(f"{k}={v}:{n}" for (k, v), n in sorted(cov.items())))
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    if errors:
        print(f"ERROR: {len(errors)} contract self-check error(s); no output was published "
              f"({args.output} is unchanged).", file=sys.stderr)
        for e in errors[:20]:
            print("  ", e, file=sys.stderr)
        return 1
    if not_established and not args.allow_incomplete_output:
        print(f"ERROR: INCOMPLETE RUN - {len(not_established)} case(s) were not fully investigated; no output was "
              f"published ({args.output} is unchanged). Pass --allow-incomplete-output to publish it anyway "
              f"(exit status 5).", file=sys.stderr)
        return 5
    try:
        publish_csv(args.output, rows, case_order, expected_ids=[r["case_id"] for r in ds.requests])
    except Exception as exc:   # PublicationError, OSError or anything else: never a partial output
        state = ("is unchanged" if not output_path.exists() or output_path.is_file()
                 else "is not a regular file and may hold a partial write")
        print(f"ERROR: output could not be published ({args.output} {state}): {exc}", file=sys.stderr)
        return 6
    print(f"wrote {len(rows)} rows to {args.output}; status counts {dict(sorted(counts.items()))}; "
          f"self-check errors {len(errors)}")
    if gaps:
        print(f"WARNING: DEGRADED RUN - {len(gaps)} evidence file(s) were not read; answers that depend on them "
              f"are not established.", file=sys.stderr)
    if not_established:
        print(f"WARNING: INCOMPLETE RUN published on request - {len(not_established)} case(s) were not fully "
              f"investigated.", file=sys.stderr)
    return 4 if gaps else (5 if not_established else 0)


if __name__ == "__main__":
    sys.exit(main())

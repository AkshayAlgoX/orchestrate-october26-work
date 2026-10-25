# Evaluation & Output Contract Validation Harness

Starter evaluation package for **HackerRank Orchestrate: Ghost Data Still Exists**.

This package provides an independent, deterministic output-contract validator, a public-sample evaluator, and comprehensive unit and contract test suites under `code/evaluation/`.

---

## 1. CLI Usage

All tools are standard-library-first and require zero external third-party dependencies.

### Running Contract Validation
```bash
# Validate production output.csv at repository root (checks 518 cases from dataset/deletion_requests.csv)
python3 code/evaluation/main.py validate output.csv

# Validate dataset/sample_output.csv against public sample requests (32 cases)
python3 code/evaluation/main.py validate dataset/sample_output.csv --requests dataset/sample_deletion_requests.csv

# Run with disputed lower-bound check strictly enforced
python3 code/evaluation/main.py validate output.csv --enforce-requested-at-bound
```

### Running Public-Sample Evaluation
```bash
# Compare a prediction CSV against dataset/sample_output.csv
python3 code/evaluation/main.py evaluate my_predictions.csv

# Output report in Markdown format
python3 code/evaluation/main.py evaluate my_predictions.csv --markdown
```

### Running Test Suite
```bash
# Run all validator unit tests and authored contract edge-case tests
python3 code/evaluation/main.py test
```

---

## 2. Enforced Contract Specification Checks

The validator strictly validates all requirements from `problem_statement.md` and `AGENTS.md`:

1. **Exact Header**: Exactly `case_id,deletion_status,steps_json` with no extra or missing columns.
2. **Case ID Matching**: Exactly the expected case IDs (518 for production requests `case_33`..`case_550`; 32 for sample requests `case_01`..`case_32`), with no duplicates, no missing cases, and no extraneous case IDs.
3. **Valid Deletion Status**: Must be one of `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`.
4. **Valid JSON Array**: `steps_json` must deserialize as a valid JSON array (`list`).
5. **Non-Deletion Contract (`NOT_APPLICABLE`)**: When `deletion_status == "NOT_APPLICABLE"`, `steps_json` must be strictly `[]`.
6. **Step Fields**: Every step object must contain exactly the 7 canonical keys:
   - `step_no` (integer)
   - `phase` (string)
   - `system_id` (string)
   - `resource_id` (string)
   - `operation` (string)
   - `state` (string)
   - `evidence_id` (string)
7. **Consecutive Step Numbering**: `step_no` starts at 1 and increases consecutively (`step_no == i + 1`).
8. **Allowed (Phase, Operation, State) Triples**: Strictly enforces the specification table:
   - `FINDING`:
     - `REPORT_REMAINING_RECORD` -> `PRESENT`
     - `REPORT_EXPOSED_PII` -> `PRESENT`
     - `REPORT_ACTIVE_DATA_FLOW` -> `ACTIVE`
     - `REPORT_RESTORE_RISK` -> `ACTIVE`
   - `REMEDIATION`:
     - `DISABLE_DATA_FLOW` -> `DISABLED`
     - `BLOCK_RESTORE` -> `BLOCKED`
     - `DELETE_RECORD` -> `ABSENT`
     - `REDACT_RECORD` -> `REDACTED`
     - `ANONYMIZE_RECORD` -> `ANONYMIZED`
   - `VERIFICATION`:
     - `VERIFY_RECORD_ABSENT` -> `ABSENT`
     - `VERIFY_RECORD_PRESENT` -> `PRESENT`
     - `VERIFY_RECORD_REDACTED` -> `REDACTED`
     - `VERIFY_RECORD_ANONYMIZED` -> `ANONYMIZED`
     - `VERIFY_DATA_FLOW_DISABLED` -> `DISABLED`
     - `VERIFY_RESTORE_BLOCKED` -> `BLOCKED`
9. **Status Phase Constraints**:
   - `COMPLETE` cases must contain ONLY `VERIFICATION` steps (no `FINDING` or `REMEDIATION`).
   - `INCOMPLETE` cases must contain `FINDING`, `REMEDIATION`, and `VERIFICATION`.
10. **Canonical Ordering**:
    - Major: `phase` (`FINDING` < `REMEDIATION` < `VERIFICATION`).
    - Secondary: operation canonical table order.
    - Tertiary: `system_id` ascending.
    - Quaternary: `resource_id` ascending.
11. **Evidence Citations**:
    - `COMPLETE` verification steps must cite a non-empty `evidence_id`.
    - `INCOMPLETE` finding steps must cite a non-empty `evidence_id`.
    - `INCOMPLETE` remediation and post-remediation verification steps represent future proposed work and must use an empty string `evidence_id: ""`.
12. **Permitted Citation Namespaces**: Non-empty `evidence_id` values must exist in:
    - `dataset/evidence_files.csv` (`file_id`)
    - `dataset/system_activity.csv` (`activity_id`)
    - `dataset/user_audit_log.csv` (`audit_id`)
13. **Temporal Bounds**: Evidence timestamp must be `<= audit_at` for the audited case.

---

## 3. Disputed Assumptions (Explicitly Separated)

The validator separates ambiguous or debated assumptions from hard specification contract requirements:

### Disputed Assumption 1: Lower Citation Bound (`timestamp >= requested_at`)
- **Analysis**: AGENTS.md §6.2 and §6.6 state that evidence captured *after* `audit_at` is invalid. However, pre-existing evidence created *prior* to `requested_at` (such as account creation snapshots, older audit log entries, or existing replica pipelines) often proves whether data existed or was exposed.
- **Handling**: By default, evidence captured prior to `requested_at` is tracked as an informational **Disputed Check** / warning rather than a fatal error. If strict lower-bound enforcement is required, pass `--enforce-requested-at-bound`.

### Disputed Assumption 2: Case ID Row Order (Natural Numerical vs Lexicographical)
- **Analysis**: `deletion_requests.csv` is ordered natural-wise (`case_33`, `case_34`, ... `case_100` ... `case_550`). In standard string sorting, `case_100` precedes `case_33`.
- **Handling**: The validator checks against the exact sequence of the input request file. If a file is sorted lexicographically by string, the validator logs a distinct `LEXICOGRAPHICAL_SORT_DETECTED` warning rather than an opaque failure.

---

## 4. Public-Sample Metrics & Grounding

The public-sample evaluator calculates six orthogonal metrics:
1. **Case-Level Status Accuracy**: Correctness of `COMPLETE`, `INCOMPLETE`, `NOT_APPLICABLE`.
2. **Exact-Array Accuracy**: Exact match of `steps_json` content and sequence.
3. **Step Precision, Recall, and F1**: Semantic alignment of `(phase, system_id, resource_id, operation, state)`.
4. **Field-Level Accuracy**: Accuracy across each of the 6 step fields on aligned steps.
5. **Sequence & Order Inversion Errors**: Detection of pairwise step inversions.
6. **Citation Agreement**: Proportion of cited steps matching the golden evidence reference.

> [!IMPORTANT]
> All metrics reported by `evaluator.py` are labeled as **PUBLIC SAMPLE METRICS ONLY**. They reflect performance exclusively against the 32 demonstration cases in `dataset/sample_output.csv` and do not estimate hidden test performance.

---

## 5. Report-Path Inconsistency Note

A structural path inconsistency exists between the specification documents and repository layouts:
- In `problem_statement.md` §Evaluation and AGENTS.md §6.8:
  The evaluation reports are specified as:
  ```text
  evaluation/evaluation_report.md
  evaluation/usage_report.md
  ```
- In the repository working tree:
  Evaluation code is located at `code/evaluation/`.
- **Resolution Note**:
  During local development, evaluation logic resides in `code/evaluation/`. When packaging `code.zip` for submission, participants must ensure that the archive contains the required reports at `evaluation/evaluation_report.md` and `evaluation/usage_report.md` (or that both `code/evaluation/` and `evaluation/` paths are accommodated as expected by the grading harness).

---

## 6. Authored Contract Edge-Case Tests

`code/evaluation/test_edge_cases.py` contains authored contract/unit tests for key specification edge cases:
1. **Edge Case 1: Non-Deletion Intent**:
   Tests that requests for access, export, or opt-out are classified as `NOT_APPLICABLE` with `steps_json=[]`, and that emitting steps or other statuses fails validation.
2. **Edge Case 2: Historical Identifiers & Identity Drift**:
   Tests handling of users whose identifiers changed in `user_audit_log.csv`. Verifies that multi-system findings and remediation (e.g. redacting legacy support archives) comply with phase ordering and tombstone rules.
3. **Edge Case 3: Replica Synchronization and Restore Risk**:
   Tests that active sync flows (`REPORT_ACTIVE_DATA_FLOW`) and restore risks (`REPORT_RESTORE_RISK`) are disabled/blocked prior to dependent record modifications, and verifies that `VERIFY_DATA_FLOW_DISABLED` / `VERIFY_RESTORE_BLOCKED` states are verified post-remediation.

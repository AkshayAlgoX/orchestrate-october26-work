# Evaluation Report — Ghost Data Still Exists

All numbers below come from commands run on 2026-10-24 against the repository state that produced
`output.csv` (current SHA-256 `d6795660b65f65090a2f06d928303e91d80b8603974f5208ee00bc1bfc781e4e`, after sprint 10;
earlier sections cite the then-current `3d628b9b…f2a9` / `ee3fd9fa…8c34`). Sample predictions:
`code/evaluation/sample_predictions.csv` (SHA-256 `b156d290…187b`, unchanged since sprint 1).

> **Public-sample metrics only.** The labelled set is the 32 public cases in
> `dataset/sample_output.csv`. These numbers measure agreement with those 32 answers; they are not an
> estimate of the hidden score on the 518 evaluation requests. Several defaults were chosen by
> ablation on the same 32 cases (see §5), so the sample numbers are optimistic.

Reproduce:

```bash
python3 code/main.py --requests dataset/sample_deletion_requests.csv --output code/evaluation/sample_predictions.csv
python3 code/evaluation/main.py evaluate code/evaluation/sample_predictions.csv
python3 code/main.py                                   # writes ./output.csv (518 rows)
python3 code/evaluation/main.py validate output.csv --requests dataset/deletion_requests.csv
python3 code/evaluation/main.py test                   # validator, edge-case and evaluator-formula tests (26)
python3 code/tests/test_pipeline.py                    # pipeline property + authored scenario tests (58)
python3 code/tests/test_agent.py                       # controller behaviour on the real dataset (12)
python3 code/tests/test_controller.py                  # routing, budget, fail-closed, tool contracts (21)
python3 -m unittest discover -s code                  # full test suite (210 tests, honours GHOST_AUDIT_DATASET)
```

## 1. Public-sample results (final defaults)

| Metric | Value | Detail |
|---|---|---|
| Case-level status accuracy | **100.00%** | 32/32 (10 COMPLETE, 22 INCOMPLETE; no NOT_APPLICABLE in the samples) |
| Exact-array accuracy (sequence accuracy, strict) | **46.88%** | 15/32 cases identical in content and order |
| Step precision | **100.00%** | TP=165, FP=0 |
| Step recall | **85.49%** | FN=28 |
| Step F1 | **92.18%** | |
| Ordering errors among matched steps | **0** | 0 pairwise inversions (checked over all matched steps since sprint 1) |
| Citation agreement (gold-cited steps) | **100.00%** | 63/63 |
| Resource precision (distinct system_id+resource_id) | **100.00%** | TP=83, FP=0 |
| Resource recall | **74.77%** | FN=28 resources |
| Loose-aligned operation accuracy | **100.00%** | 165 steps aligned on (phase, system_id, resource_id) |
| Loose-aligned evidence_id accuracy | **100.00%** | 165/165 |

Field-level accuracy, as computed by `evaluator.py` on steps aligned by the full step key:
phase 100%, operation 100%, state 100%, system_id 100%, resource_id 100%, evidence_id 100% (165 steps).
**Measurement note:** this aligns on (phase, system_id, resource_id, operation, state), so those five fields
match by construction. I added the resource-level and loose-aligned metrics to `evaluator.py` to give a
non-tautological view; the existing metrics and the 22 existing tests are unchanged.

### Where the 28 missed steps come from (all are VERIFICATION steps; per-step matrix in §7)

| Missed step type | Count | Cause | Status |
|---|---|---|---|
| `account_service … VERIFY_RECORD_ABSENT` | 16 | The account ID appears only in `system_activity.csv` (deletion-worker rows), or only in a 404 lookup with no identifier. Several requests share the same `reported_at` slot, so attribution is ambiguous. | Not resolved: the pipeline drops ambiguous attributions on purpose |
| `security_service … VERIFY_RECORD_PRESENT` | 7 | 5 records are visible only in files captured **after** `audit_at` (evidence_02, evidence_06). 2 (`audit_record_6645`, `audit_record_5569`) appear in no participant file. | 2 are unrecoverable; 5 are recoverable only by an experiment that is off by default (§5) |
| `analytics_platform … VERIFY_RECORD_ANONYMIZED` | 5 | The anonymized rows carry no identifier, and two or more requests explain each one equally well. | Not resolved |

None of the 32 cases has a wrong finding, a wrong remediation, a wrong treatment
(delete/redact/anonymize/retain/block/disable), a wrong citation or a wrong order.

## 2. Full 518-case output

`python3 code/evaluation/main.py validate output.csv --requests dataset/deletion_requests.csv` →
**PASSED, 518 rows, 0 errors, 1 warning**. The warning: `case_63` is COMPLETE with `[]`; see §6.

| | Count |
|---|---|
| COMPLETE | 154 |
| INCOMPLETE | 354 |
| NOT_APPLICABLE | 10 (`case_533`–`case_542`) |
| Steps | 2,783 (721 FINDING, 721 REMEDIATION, 1,341 VERIFICATION) after sprint 10 (2,799 after sprint 5; 2,796 before) |
| Resource links by basis | 1,244 direct identifier, 109 workflow, 3 catalog (one link can support several steps) |
| Runtime | 1.74 s wall (mean of 6 interleaved runs), 48.9 MB RSS, Python 3.12.3, standard library + `pdftotext` |

Row order is natural numeric `case_id` order (`case_33` … `case_550`), which matches
`dataset/deletion_requests.csv`. The contract says "sorted by case_id" without saying string or numeric
order; that ambiguity is documented and is not resolved by any official source.

## 3. Test suites

| Suite | Result |
|---|---|
| `code/evaluation/main.py test` (contract validator, 3 contract edge cases, evaluator formulas, evaluate fails closed without gold) | 26/26 OK |
| `code/tests/test_agent.py` (investigation-controller behaviour, image provenance) | 12/12 OK |
| `code/tests/test_pipeline.py` | 58/58 OK (sprint-2…10 fixtures, hash-seed, case-order, category-history, co-location, check_row, D1–D6 regression and capacity-scope tests; intent test includes profile-only requests, §21) |
| `code/tests/test_controller.py` (routing, budget, fail-closed fallback, tool contracts; §16) | 21/21 OK |
| `code/tests/test_evidence_gate.py` (unreadable evidence and malformed inputs; §18) | 12/12 OK |
| `code/tests/test_invariance.py` (row order, JSON key order, account renaming, ±45 s jitter; naming limitation; §17) | 6/6 OK (1 expected failure: the documented limitation) |
| `code/tests/test_config_prompts.py` (configuration loading; prompt documents checked against code and data; §20, §21) | 8/8 OK |
| `code/tests/test_agent_preconditions.py` (semantic precondition guards, red-team regression & contract tests; §20) | 14/14 OK |
| `code/tests/test_recovery.py` (fault isolation, resource-scoped degradation, fail-closed handling; §20) | 12/12 OK |
| `code/tests/test_publication.py` (all-or-nothing publication, destination preservation, atomic rename, cleanup; §20) | 27/27 OK |
| `code/tests/test_evidence_provenance.py` (evidence-step substantiation, direct observations, scoped absence; §20) | 7/7 OK |
| `code/tests/test_provenance_bypass_hunt.py` (adversarial citation verification, mutation detection, falsification resistance; §20) | 7/7 OK |
| `python3 -m unittest discover -s code` | 210 run, OK (1 expected failure; 1 skipped inside the repository, where `test_unresolvable_default_golden_is_an_error` cannot run because `dataset/` resolves) |

### 3.1 Clean-room test suite execution (without dataset)

When executed in a clean environment where the participant dataset is not mounted (`GHOST_AUDIT_DATASET` unset and `dataset/` directory absent), all dataset-dependent tests cleanly self-skip via `unittest.skipIf`, while all standalone contract, property, mock, and safety tests execute and pass:

**Clean-Room Execution Summary:**
- **210 total discovered methods** across the repository (184 in `code/tests`, 26 in `code/evaluation`).
- **151 methods actually counted in unittest's `testsRun`** (125 in `code/tests`, 26 in `code/evaluation`).
- **33 passes, 1 expected failure, and 127 recorded skip events** logged by the `unittest` test runner:
  - `code/tests`: 28 passed + 1 expected failure + 106 recorded skip events (96 method skips + 10 class skips); 125 `testsRun`.
  - `code/evaluation`: 5 passed + 0 expected failures + 21 recorded skip events = 26 `testsRun`.
  - Combined: 33 passed + 1 expected failure + 127 recorded skip events (117 method skips + 10 class skips).
- **176 individual methods skipped in total**, including all methods belonging to skipped test classes:
  - `code/tests`: 96 standalone method skips + 59 methods inside 10 test classes skipped via `setUpClass` = 155 skipped methods.
  - `code/evaluation`: 21 standalone method skips = 21 skipped methods.
  - Combined: 155 + 21 = 176 individual test methods skipped.

| Suite | Total Discovered Methods | Evaluated by Runner (`testsRun`) | Passed Methods | Expected Failures | Recorded Skip Events | Individual Skipped Methods | Exit Status |
|---|---|---|---|---|---|---|---|
| `python3 -m unittest discover -s code/tests` | 184 | 125 | 28 | 1 (`NamingLimitation`) | 106 (96 method + 10 class) | 155 (96 method + 59 in skipped classes) | OK (exit code 0) |
| `python3 -m unittest discover -s code/evaluation` | 26 | 26 | 5 | 0 | 21 (all method skips) | 21 (all method skips) | OK (exit code 0) |
| `python3 code/evaluation/main.py test` | 26 | 26 | 5 | 0 | 21 (all method skips) | 21 (all method skips) | OK (exit code 0) |
| `python3 -m unittest discover -s code` (combined) | 210 | 151 | 33 | 1 (`NamingLimitation`) | 127 (117 method + 10 class) | 176 (117 method + 59 in skipped classes) | OK (exit code 0) |

*Reconciliation Note:* Standard Python `unittest` reports `Ran 125 tests` for `code/tests` and `Ran 151 tests` for the combined discovery. This occurs because when an entire test class is skipped via `setUpClass` (10 classes containing 59 methods across `code/tests`), `unittest` registers 1 skip event without incrementing its internal `testsRun` counter for the individual methods of that skipped class. The exact method accounting is:
- `code/tests`: 28 passed + 1 expected failure + 96 method skips + 59 class-skipped methods = 184 test methods total.
- `code/evaluation`: 5 passed + 0 expected failure + 21 method skips = 26 test methods total.
- **Combined:** 33 passed + 1 expected failure + 176 individual skipped test methods (127 recorded skip events) = 210 test methods discovered across the entire repository.

New real-data property and metamorphic tests check invariants on the participant data; they do not
check labels. They cover:
- every row passes the contract self-check;
- two runs produce identical output;
- shuffling the requests gives the same answers;
- upper-casing request emails gives the same answers;
- no cited file was captured after the case's `audit_at`;
- moving `audit_at` to one second before a cited file's capture time removes that citation (200 cases);
- non-deletion requests always give `NOT_APPLICABLE` / `[]`.

## 4. Authored evaluation cases (new; contract-derived expected answers, not hidden labels)

Each scenario builds a small synthetic dataset in a temporary directory, using the real
`deletion_rules.csv`, and runs the full pipeline end to end (`code/tests/test_pipeline.py::AuthoredScenarios`).

1. **S1 — post-cutoff evidence.** A 404 lookup and an in-window snapshot show the profile gone. A file
   captured after `audit_at` shows it ACTIVE again. Expected: COMPLETE, citing only the in-window files,
   with the security record verified PRESENT. *Justification:* AGENTS §6.2 and §6.6 (state at `audit_at`;
   later files must not change the answer or be cited).
2. **S2 — historical identifiers and decoys.** The request uses the subject's *old* email in upper case.
   An analytics row is keyed only by the *old* email hash. The decoys are a `.jr` lookalike, a
   **name-only** match, and a real different user with a similar name. Expected: the search document and
   the analytics row are found (delete and anonymize); all three decoys are ignored.
   *Justification:* §6.5 (current + historical identifiers) and names never establish identity.
3. **S3 — replica drift, flows, restore, shared support, tombstone.**
   - Setup: a profile exists only in the read replica, a connector is enabled, a backup has
     `restore_allowed=true`, a support thread contains an agent note plus the subject's email, and a
     tombstone still holds the name and email.
   - Expected: all five findings; remediation in published operation order
     (DISABLE → BLOCK → DELETE → REDACT → REDACT); five matching verifications.
   - *Justification:* rules 05, 14 and 15, and the completion definition (no active sync or restore
     path remains).
4. **S4 — clean tombstone, attachment vs message, non-deletion.**
   - Setup: a tombstone holding only `subject_reference` is compliant; a ticket attachment is
     DELETE (rule 06), not REDACT; a second request "stop promotional emails… not asking for
     deletion" for the same person.
   - Expected: INCOMPLETE with the attachment remediated and the compliant tombstone verified
     REDACTED; the second request gives `NOT_APPLICABLE` / `[]`.
5. **Duplicate evidence.** The same snapshot appears in two files. Expected: no duplicate steps, and
   the citation is chosen deterministically.

## 5. Failures found and improvements made (measured on the 32 samples)

| Version | Change | Status | Exact | Precision | Recall | F1 |
|---|---|---|---|---|---|---|
| v0 | Typed parsers + direct identifier links + naive workflow links + cross-file "absent from snapshot" | 31/32 | 3/32 | 89.14% | 80.83% | 84.78% |
| v1 | **Defect:** an empty account snapshot from another inventory (evidence_90) "proved" `account_005` absent, and a different bundle "proved" security records absent. **Fix:** a scope proves absence only for the same physical store/table it enumerates. | 32/32 | 9/32 | 89.89% | 87.56% | 88.71% |
| v2 | **Defect:** per-case workflow links attached other requests' anonymized and 404 rows (19 false positives). **Fix:** all participant requests compete; constraint propagation assigns a resource only when exactly one request remains; ambiguous resources are dropped. | 32/32 | 9/32 | 100.00% | 81.87% | 90.03% |
| v3 | Nearest-worker-activity tie-break (≥ 5 min margin), only for candidates that are observed in a shared file | 32/32 | 10/32 | 100.00% | 82.38% | 90.34% |
| v4 | Accounts known only from deletion-worker activity become candidates (USER_PROFILE only). Absence is proven only by a later in-window snapshot of the same system; otherwise the record is a future check in INCOMPLETE plans. The tie-break is not applied to these. | 32/32 | 15/32 | 100.00% | 84.97% | 91.88% |

| v5 (sprint 1, final) | A request with **no** directly linked evidence file may receive an identifier-less observed resource from any in-window file. Global uniqueness is still required. | 32/32 | 15/32 | 100.00% | 85.49% | 92.18% |

Rejected variants (measured):
- **Ambiguous resources assigned to every candidate request:** precision 92.35%, recall 87.56%, F1 89.89%.
  Lower F1, and it is speculative.
- **Proximity tie-break applied to activity-only accounts:** 5 false positives (cases 09, 18, 20, 31, 32),
  precision 97.04%.
- **No lower time bound (`--window none`, evidence before `requested_at` allowed):** 5 false positives
  (older records of a person who has an earlier request), precision 97.04%.
- **Identity look-back (older evidence may link a resource):** no change on the samples and no change in
  the 518 rows.

Experiment, kept **off** by default:
- **What it does** (`--post-cutoff-retention`): adds an uncited future `VERIFY_RECORD_PRESENT` for a
  retained security record that is visible only in a file captured after `audit_at`, where the record's
  own time is ≤ `audit_at`.
- **Measured effect** (`--engine pipeline`, re-measured 2026-10-25): samples precision 100%, recall
  88.08%, F1 93.66% (TP 170, FP 0), exact 16/32; changes 116 of the 518 rows (+118 uncited steps).
- **Engine limitation:** These C3 metrics come from the pipeline engine only. In
  the current default agent engine, adding a post-cutoff-only retained resource leaves it outside the
  subject-linked resource set; the admissibility check rejects the extra step, including on fallback.
  The C3 metrics therefore do not describe a successful default-agent run.
- **Why it is off:** C3 passes *citation-time validation* (no step cites evidence captured after
  `audit_at`) but fails *information provenance* (its steps are derived from source records captured
  after `audit_at`). The provenance failure violates AGENTS §6.2 ("Evidence created later must not change
  the answer"). C3 is a rejected diagnostic, not a submission option; keep the flag disabled.

Other defects found and fixed during development:
- The evidence loader assumed numeric file-ID suffixes (crash on authored scenario IDs); it now uses a
  natural sort.
- The audit cache was keyed by `case_id` only, so a re-audit with a different `audit_at` reused stale
  links. It now uses the full request key, and workflow assignments apply only to the exact request they
  were solved for.
- The intent classifier initially rejected 9 natural-language sample requests ("I'm deleting my account",
  "remove that search result"). The target vocabulary was widened. Negative scope (explicit "do not
  delete", "only marketing", "keep the account") still overrides.

## 6. Known gaps and risks (disclosed, not hidden)

- **Attribution ambiguity.** 289 of the 508 deletion cases have no attributable account resource. In the
  samples, every deletion case has one in the gold answer, so hidden recall on account-absence checks is
  likely well below sample recall.
- **COMPLETE with `[]`.** `case_63` has no evidence-backed resource and is emitted as COMPLETE with
  an empty array. (Sample `case_04` was in the same state until sprint 1's X1; see §7.) For `case_63`:
  - the only in-window evidence is a screenshot of `marketing_001` showing a different person;
  - catalog row `activity_4008` claims the record belongs to the requester;
  - the claim is rejected as contradicted.
- **Images.** No OCR engine is installed. The 11 PNGs were transcribed visually once during development
  by the coding model; the cache in `code/ghost_audit/visual.py` is bound to each image's SHA-256. This
  input is model-derived and was not independently re-verified by a second method.
- **Categories inferred from structure.**
  - Analytics rows with `email_opt_in` are classed MARKETING_PROFILE (delete). This explains sample
    `case_12` but is a HYPOTHESIS.
  - The legacy contact index is classed SECONDARY_IDENTIFIER (delete).
- **No sort-tie test.** Sort ties on `resource_id` within the same phase, operation and system are
  string-ordered. The samples never contain such a tie, so this is untested.
- **Security retention.** Retention is assumed active unless an evidence `retention_until` precedes
  `audit_at`. No expired retention exists in the data.

## 7. Sprint 1 — evaluator self-test, missing-step forensics, experiments

### 7.1 Evaluator self-test and fix

- Scoring `dataset/sample_output.csv` against itself gives 100% on every metric with 0 ordering errors.
- **Defect found:** inversions were only counted when *every* step matched. A probe that dropped one
  step of `case_11` and swapped two findings reported "0 inversions".
- **Fix:** relative order is now checked among all matched steps, so the probe reports
  1 case / 1 inversion. The regression tests are in `code/evaluation/test_evaluator.py` and run from
  `main.py test`.
- Earlier metrics were unaffected: 0 inversions before and after.

### 7.2 Per-missing-step matrix (baseline before sprint: 29 missed steps)

Searched: all 94 evidence files (text, SQL, JSON, logs and PDF text; PNGs via the cached transcription),
`system_activity.csv`, and both request files.

Window labels:
- **IN:** captured between `requested_at` and `audit_at`.
- **POST:** captured after `audit_at`.

Category codes are those from the sprint brief:
- **C:** workflow/activity claim only.
- **D:** scoped observation without a subject identifier.
- **E:** visible only after `audit_at`.
- **F:** absent from participant data.
- **G:** ambiguous between several requests.

| # | Case | Expected step (gold evidence) | Participant-visible support | Code | Admissible / recoverable without guessing? |
|---|---|---|---|---|---|
| 1 | case_04 | account_004 VERIFY_ABSENT (evidence_54) | No file and no activity row mentions `account_004` | F | No: an unsupported reference ID |
| 2 | case_04 | analytics_901 VERIFY_ANONYMIZED (evidence_56) | evidence_56 (IN) row with all identifiers null; `activity_3463` ANONYMIZE at reported_at−0.1 h; no other request can claim it | C+D | **Yes. Recovered by X1** |
| 3 | case_09 | account_3960 VERIFY_ABSENT (–) | Named only in evidence_06 (POST); `activity_3703` DELETE FAILED; candidates case_09, case_64 | C+E+G | No |
| 4 | case_09 | audit_record_4902 VERIFY_PRESENT (–) | Only evidence_06 (POST), line time before audit_at | E | No (post-cutoff) |
| 5 | case_10 | account_010 VERIFY_ABSENT (–) | `activity_3595` only; 6 candidate requests | C+G | No |
| 6 | case_18 | account_5521 VERIFY_ABSENT (–) | `activity_3732` only; candidates 18, 20, 51 | C+G | No |
| 7 | case_18 | audit_record_6645 VERIFY_PRESENT (–) | Nowhere in participant data | F | No |
| 8 | case_20 | account_5037 VERIFY_ABSENT (–) | `activity_3734` only; 6 candidates | C+G | No |
| 9 | case_20 | audit_record_5569 VERIFY_PRESENT (–) | Nowhere | F | No |
| 10 | case_21 | account_6440 VERIFY_ABSENT (evidence_92) | 404 lookup in evidence_92 (IN, no identifier); candidates 21, 23 (same reported_at, same file) | C+D+G | No |
| 11 | case_21 | analytics_5335 VERIFY_ANONYMIZED (evidence_92) | Anonymized row in evidence_92; candidates 21, 23 | C+D+G | No |
| 12 | case_22 | account_4990 VERIFY_ABSENT (–) | `activity_3573` only; 6 candidates | C+G | No |
| 13 | case_23 | account_410 VERIFY_ABSENT (–) | `activity_3662` only; 5 candidates | C+G | No |
| 14 | case_23 | audit_record_4128 VERIFY_PRESENT (–) | evidence_02 (POST) sub-snapshot dated 09-15T12:00 | E | No (post-cutoff) |
| 15 | case_24 | audit_record_7095 VERIFY_PRESENT (–) | evidence_02 (POST) | E | No |
| 16 | case_25 | account_3136 VERIFY_ABSENT (evidence_92) | 404 in evidence_92; candidates 25, 26 after capacity pruning | C+D+G | No |
| 17 | case_25 | analytics_1895 VERIFY_ANONYMIZED (evidence_92) | Anonymized in evidence_92; candidates 24, 25, 26 | C+D+G | No |
| 18 | case_26 | account_5684 VERIFY_ABSENT (evidence_92) | 404 in evidence_92; the subject link exists only in evidence_02 (POST); candidates 25, 26 | C+D+E+G | No |
| 19 | case_26 | analytics_4566 VERIFY_ANONYMIZED (evidence_92) | Same pattern as #17 | C+D+G | No |
| 20 | case_27 | account_3866 VERIFY_ABSENT (evidence_92) | 404 in evidence_92; subject link only in evidence_02 (POST); candidates 27, 28, 29 | C+D+E+G | No |
| 21 | case_27 | analytics_4574 VERIFY_ANONYMIZED (evidence_92) | Anonymized; candidates 27, 28, 29 | C+D+G | No |
| 22 | case_28 | account_5482 VERIFY_ABSENT (evidence_92) | 404; candidates 27, 28, 29 | C+D+G | No |
| 23 | case_28 | analytics_6077 VERIFY_ANONYMIZED (evidence_92) | Anonymized; candidates 27, 28, 29 | C+D+G | No |
| 24 | case_29 | account_7012 VERIFY_ABSENT (–) | `activity_3532` only; candidates 27, 28, 29 | C+G | No |
| 25 | case_29 | audit_record_3954 VERIFY_PRESENT (–) | evidence_02 (POST) | E | No |
| 26 | case_30 | account_4163 VERIFY_ABSENT (–) | `activity_3465` only; candidates 04, 30 | C+G | No |
| 27 | case_30 | audit_record_1707 VERIFY_PRESENT (–) | evidence_02 (POST) | E | No |
| 28 | case_31 | account_4857 VERIFY_ABSENT (evidence_51) | `activity_3682` only (evidence_51's empty account collection would prove absence); candidates 31, 32 after capacity pruning | C+D+G | No |
| 29 | case_32 | account_8858 VERIFY_ABSENT (–) | `activity_3685` only; candidates 31, 32 | C+G | No |

Observed facts versus open questions:
- `account_004`, `audit_record_6645` and `audit_record_5569` appear in no participant file. The gold
  answer cannot be derived from participant data for those three (category F).
- Six rows (#3, 4, 14, 15, 25, 27) are recoverable only through files captured after `audit_at`.
  The sprint brief rules this out, so they stay missed.
- The other ambiguous (G) rows involve two or more requests:
  - with the same `reported_at` slot;
  - whose own content links appear in the same evidence file;
  - and the same per-request capacity (one account and one analytics profile each).
- Signals tested and **rejected** because they carry no attribution information:
  - **Readback `observed_at` times in evidence_92:** scattered 08:48–11:44 within each request.
  - **Worker action order within a slot:** inconsistent between the 09-02 and 08-30 slots.
- Whether the reference answers came from information outside the participant files is UNKNOWN.

### 7.3 Experiments (one mechanism each; same evaluator and validator; baseline = v4)

| Exp | Hypothesis | Sample result | Main-set rows changed | Decision |
|---|---|---|---|---|
| X1 | An identifier-less observed resource that the worker acted on, which only one request can explain, belongs to that request even if the request has no directly linked file | +1 TP (case_04 analytics_901, evidence_56), 0 FP; F1 91.88 → 92.18 | 0 | **Promoted** |
| X2 | Evidence captured before `requested_at` links more resources (identity look-back) | 0 change | 0 | Rejected (no effect) |
| X3 | COMPLETE cases drop uniquely assigned accounts for lack of a scoped absence proof | 0 such cases | 0 | Not needed |
| X4 | Correlation keys, catalog rows or readbacks attribute anonymized analytics | No subject-bearing field exists for the remaining rows | — | No change; stays unresolved |
| X5 | Coverage diagnostic for required obligations | Diagnostic only | 0 | Kept (trace + run summary) |
| Probe | `resource_id` tie order (string vs natural) changes the output | 10 tie groups, 0 order differences | — | Risk not present in this output |

### 7.4 Main-set coverage (diagnostic, `python3 code/main.py` summary)

| Obligation | Established | Ambiguous | None |
|---|---|---|---|
| Account | 219 | 279 (2–6 candidates each) | 10 |
| Retained security record | 300 | — | 208 (mostly INCOMPLETE cases) |

None of these gaps are filled automatically.

**case_63** is the only deletion case with no admissible evidence:
- no in-window file names the subject;
- there is no deletion-worker activity in its window;
- the only claim (`activity_4008`, `marketing_001`) is contradicted by evidence_60, which shows
  another person.

It remains COMPLETE with `[]`, because INCOMPLETE would require a finding there is no evidence for. It is
flagged by the run summary and by `test_empty_complete_only_without_admissible_evidence`. This is a
disclosed contract/data ambiguity.

## 8. Sprint 2 — main-dataset precision audit

### 8.1 Baseline reproduced before any edit

| Item | Result |
|---|---|
| `output.csv` SHA-256 | `3d628b9b…f2a9` (unchanged) |
| Sample predictions SHA-256 | `b156d290…187b` (unchanged) |
| Status counts | 154 COMPLETE / 354 INCOMPLETE / 10 NOT_APPLICABLE |
| Sample metrics | status 32/32; exact 15/32; P / R / F1 = 100 / 85.49 / 92.18%; citations 63/63; inversions 0 |
| Validator, full output | PASSED, 518 rows, 0 errors, 1 warning (`case_63`) |
| Evaluator self-test (gold vs gold) | 100% on every metric; 0 inversions; evaluator SHA-256 `87c38bab…2854` |

### 8.2 Audit matrix (all 518 decisions; evidence from in-memory replay of the pipeline over the actual files)

Classes: **PD** proven defect, **SC** supported concern, **AMB** evidence/spec ambiguity, **OK** correct.

| Area | Scope checked | Finding | Class |
|---|---|---|---|
| Security records | 305 `VERIFY_RECORD_PRESENT` steps in 300 cases | Every record is linked to the request's own `user_id` (by `subject_reference` or a trace `subject_ref`); no observation carries another identity. Retention values: 99 active, 243 not supplied, 0 expired. | OK |
| Security records, case_45 | 6 records (`audit_record_001`–`006`, evidence_37 screenshot, subject `user_03`) | Retention is supplied only for `audit_record_003` (evidence_55: retain until 2027-08-25). Records without a supplied period are treated as retained; that matches the samples (e.g. `audit_record_6909`, evidence_51, no retention field, gold PRESENT). Samples never show more than one record per case. | SC (possible 5 extra steps) |
| Retention condition | rule_12 / rule_13 conditions | **Latent defect:** an expired `retention_until` / `retained_until` was still verified PRESENT. No such record exists in this data. | PD (latent) → fixed (C1) |
| Legal holds | 10 hold IDs in evidence/activity | 6 linked and active (until 2033, or FOUND with RETAIN activity) → PRESENT, 5 of them inside INCOMPLETE plans. `legal_hold_4961` (case_220) and `legal_hold_438` (case_432) are visible only after those cases' `audit_at` and are correctly excluded. `legal_hold_1745` and `legal_hold_668` appear in activity only. evidence_27 register names no resource IDs and lies outside every relevant window. | OK |
| Marketing / secondary identifier | All MARKETING_PROFILE and SECONDARY_IDENTIFIER observations | MARKETING: `analytics_9914` (opt-in contact lookup, sample) and `marketing_001` (screenshot of another person, not linked). SECONDARY: the legacy contact index in evidence_61 (`support_002` sample; `support_4104` and `support_5914` belong to no request). The one-sample category rule affects no main-set row. | OK / SC (single sample) |
| Support resources | 182 support decisions; 5 cases with 2 resources (95, 131, 263, 407, 527) | Each resource is classified and planned independently. **8 resources are labelled `ticket_attachments` only in files captured after `audit_at`**: `support_1370` (case_71), `support_9309` (case_119), `support_2315` (case_131), `support_2283` (case_179), `support_5830` (case_227), `support_3684` (case_275), `support_3615` (case_431), `support_3896` (case_527). Their in-window evidence shows ticket records exposing the requester's email, so they are REDACT. Under the strict cutoff the category cannot change; if the attachment label applies, these 8 would become DELETE. | AMB |
| Screenshot / image cases | 9 image-only cases plus 34 others with image observations | All 11 cached transcriptions still match their image SHA-256. Fields were re-checked against the screenshots viewed in this session; no OCR and no new model call. Catalog joins: `export_1842` (PDF statement NST-2026-0718-1842), `tombstone_17` (evidence_42 shows only `user_17`), `object_19` (evidence_46 shows no owner). All three are uncontradicted claims. | OK / SC (`export_1842` is a customer statement that the catalog calls an export) |
| `marketing_001` vs `activity_4008` | case_63 | The screenshot shows Soo-jin Park, `sj.park@naver.com`; the claim names `kwame-mensah`/`user_09`. Rejected as contradicted; case_63 has no admissible evidence (COMPLETE with `[]`, flagged). | AMB |
| Backup restore unknown | case_55 `backup_9287` | The screenshot shows the archive but not its restore status. Without proof of blocking it is reported as a restore risk. | SC |
| Absence from scoped snapshots | 43 activity-only accounts | Each is proven absent by an in-window snapshot of `account_service` captured after the deletion activity. This is the same evidence pattern the gold uses (case_01/02/04 → evidence_54, case_31 → evidence_51). Attribution relies on uniqueness among all 550 requests; all 1,466 deletion-worker activities fall inside some known request window. | SC |
| Store-shift absence | Records absent at the latest file while an earlier in-window store held them | 0 | OK |
| Same-time conflicting observations | State-bearing observations sharing file, store and time | 0 | OK |
| Data flows and backups | Conflicting enabled/restore values within a window | 0 flows; 1 backup unknown (case_55) | OK / SC |
| Tombstones | 9 (case_46, case_543–case_550) | 4 still hold direct identifiers → REDACT; 5 compliant → VERIFY_REDACTED (evidence_52 / evidence_42) | OK |
| Analytics exposure basis | 73 exposed | Always based on a direct identifier or a reported identifier; never on a stable reference alone | OK |
| Structure | Findings = remediation ⊆ verification (INCOMPLETE); verification only (COMPLETE); IDs unique per system | 0 violations | OK |
| Coverage | 508 deletion cases | Account: 219 established / 279 ambiguous / 10 none. Retained security: 300 / 208 none. | AMB (not filled) |

### 8.3 Change promoted in sprint 2

**C1 — retention conditions enforced** (`planner.plan_case`).
- **Hypothesis:** rule_12 ("within the supplied retention period") and rule_13 ("a supplied legal hold is
  active") must gate `VERIFY_RECORD_PRESENT`. With a supplied end date before `audit_at`, no retention
  obligation exists. No rule authorizes deletion in that case either, so nothing is planned and the gap
  is noted in the trace.
- **Measured:** `output.csv` and the sample predictions are byte-identical (no expired retention in the
  data). The new fixture `test_legal_hold_and_security_retention_conditions` fails without C1 and passes
  with it.

Six sprint-2 fixtures were added in `HiddenLikeFixtures`:
- legal-hold and security-retention conditions;
- an opt-in marketing contact vs an attributable analytics row;
- multiple support resources (message + attachment + already-redacted message);
- a duplicate-name decoy plus a catalog claim contradicted by the shown identifiers;
- an empty inventory from another store that must not prove a replica row absent;
- irrelevant decoys that leave the answer unchanged.

They complement S1–S4 (post-cutoff evidence, lookalike/historical identifiers, replica + flow + backup +
tombstone, attachment vs message).

No other candidate was promoted. The audit found no evidence-backed defect in the current output beyond
the latent C1 issue, and the rules forbid using post-cutoff evidence to resolve the AMB rows.

## 9. Sprint 3 — sensitivity analysis of open interpretations

Champion before and after: `output.csv` `3d628b9b…f2a9`, sample predictions `b156d290…187b`, evaluator
`87c38bab…2854` (gold-vs-gold 100%). Every scenario changes one assumption, in an isolated harness
outside the package, with the same evaluator and validator.

All metrics below are **PUBLIC-SAMPLE METRICS** (32 cases), not hidden-score estimates.

| Scenario | Assumption changed | Status | Exact | P / R / F1 | Res. P / R | Citations | Validator (518) | Future citations | Unsupported findings | Rows changed (518) | Steps +/− |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Champion | — | 32/32 | 15/32 | 100 / 85.49 / 92.18 | 100 / 74.77 | 63/63 | PASS (1 warning) | 0 | 0 | 0 | 0 / 0 |
| A2 | 8 support records → SUPPORT_ATTACHMENT / DELETE using post-cutoff labels | 32/32 | 15/32 | 100 / 85.49 / 92.18 | 100 / 74.77 | 63/63 | PASS (1 warning) | 0 | 0 | 8 | 24 / 24 |
| B2 | case_45 keeps only `audit_record_003` | 32/32 | 15/32 | 100 / 85.49 / 92.18 | 100 / 74.77 | 63/63 | PASS (1 warning) | 0 | 0 | 1 | 0 / 5 |
| C2 | Observation eligibility by record time; file capture time ignored | 32/32 | 15/32 | 100 / 89.64 / 94.54 | 100 / 81.98 | 38/65 | **FAIL: 408 FUTURE_CITATION** | 408 | 239 | 183 | 602 / 414 |
| C3 | Only uncited future `VERIFY_RECORD_PRESENT` for retained records seen after the cutoff (`--post-cutoff-retention`) | 32/32 | 16/32 | 100 / 88.08 / 93.66 | 100 / 79.28 | 63/63 | PASS (1 warning) | 0 | 0 | 116 | 118 / 0 |
| D-alt | case_63 COMPLETE `[]` → INCOMPLETE `[]` | 32/32 | 15/32 | 100 / 85.49 / 92.18 | 100 / 74.77 | 63/63 | PASS (warning now on INCOMPLETE) | 0 | 0 | 1 | 0 / 0 |
| E | Lexicographic row order | 32/32 | 15/32 | 100 / 85.49 / 92.18 | 100 / 74.77 | 63/63 | PASS (+ LEXICOGRAPHICAL_SORT_DETECTED warning) | 0 | 0 | 0 (518 rows reordered) | 0 / 0 |

"Unsupported findings" counts FINDING steps whose cited file is not an eligible observation of that
resource at the case's cutoff.

### Row-level change manifests

- **A2** (each row: REPORT_EXPOSED_PII / REDACT / VERIFY_REDACTED → REPORT_REMAINING_RECORD / DELETE / VERIFY_ABSENT; citations unchanged):
  - case_71 `support_1370` (evidence_93)
  - case_119 `support_9309` (evidence_88)
  - case_131 `support_2315` (evidence_87)
  - case_179 `support_2283` (evidence_84)
  - case_227 `support_5830` (evidence_80)
  - case_275 `support_3684` (evidence_77)
  - case_431 `support_3615` (evidence_68)
  - case_527 `support_3896` (evidence_62)

  The attachment label for each exists only in evidence_06, 11, 12, 17, 22, 26, 40 and 49 respectively.
  All of those were captured after the corresponding `audit_at`.
- **B2:** case_45 loses `VERIFY_RECORD_PRESENT` for `audit_record_001`, 002, 004, 005 and 006 (all evidence_37).
- **C2:** 183 rows change, including 1 COMPLETE→INCOMPLETE; 602 steps added and 414 removed. Most of the
  churn is citations moving to post-cutoff files (e.g. case_64 citing evidence_06, captured 09-18, for
  an `audit_at` of 09-16).
- **C3:** 116 INCOMPLETE rows each gain uncited `VERIFY_RECORD_PRESENT` security checks (118 steps),
  e.g. case_65 `audit_record_1848`.
- **D-alt:** case_63's status only.
- **E:** order only. String order starts `case_100, case_101, …`; natural order starts `case_33, case_34, …`.

### Admissibility and disposition

| Scenario | Authority | Classification | Disposition |
|---|---|---|---|
| A1 (champion) | AGENTS §6.2 "Use only information available at or before audit_at… Evidence created later must not change the answer"; §6.6 | Permitted | **Keep** |
| A2 | The label comes from files captured after `audit_at` and changes the finding and remediation | Disallowed by current rules | Reject (diagnostic only; 0 sample effect) |
| A3 | In-window content is ticket text ("Customer asked support to correct delivery details", "…supplied contact details…") and gives no attachment signal | Not supported | Same as A1 |
| B1 (champion) | rule_12; each of the six rows shows subject `user_03` and integrity Valid in evidence_37 (in window: 09-05 vs `audit_at` 09-06). Only 003 has a supplied period (evidence_55, until 2027-08-25). Gold verifies security records with no supplied period as PRESENT (case_31 `audit_record_6909`, evidence_51). | Supported by participant evidence | **Keep** |
| B2 | Its precondition (the other five fail identity, cutoff or policy) is not met | Not supported | Reject |
| C1 (champion) | AGENTS §6.2 and §6.6; validator FUTURE_CITATION | Explicitly required | **Keep** |
| C2 | 408 future citations, 239 findings not supported by eligible evidence | Disallowed (hard failure) | Reject |
| C3 | Passes citation-time validation (no citation) but fails information provenance: post-cutoff source records change the answer, contrary to §6.2 | Disallowed by current rules | Reject; keep as an off-by-default diagnostic; request clarification |
| D1 (champion) | Neither status is fully satisfiable: COMPLETE requires proof, INCOMPLETE requires findings | Unresolved | **Keep**, with a run warning, trace flag and test |
| D2 | No admissible finding exists (no in-window evidence of the subject, no worker activity; `activity_4008` is contradicted by evidence_60) | Cannot be constructed | Reject |
| D-alt | Schema-valid but asserts non-compliance with no finding; no evidence-based gain | Not supported | Reject |
| E | problem_statement "sorted by `case_id`"; README same; AGENTS "Sort rows by `case_id`". The input file and validator use natural order; string order raises a validator warning. | Unresolved (both deterministic) | **Keep natural**; `--case-order lexicographic` added for a fast switch if clarified |
| F1–F3 | problem_statement and AGENTS §6.8: `evaluation/…` in code.zip; README: `code/evaluation/…`, and the README archive command bundles `code/` and allows adding a top-level `evaluation/` | F3 satisfies all three documents | Package with **both** paths, byte-identical copies made at packaging time from `code/evaluation/`, with a checksum check (not yet packaged) |

### Change made in sprint 3

- `code/ghost_audit/output.py` and `code/main.py`: added `--case-order {natural,lexicographic}`. Default
  is natural, so output is byte-identical.
- `code/tests/test_pipeline.py`: added `test_case_order_modes_are_deterministic`.
- No prediction change was promoted. No alternative is both permissible and demonstrably better.

### Unresolved contradictions that materially affect score

1. **Cutoff semantics** (file capture time vs internal record time). C3 alone touches 116 rows and C2
   touches 183.
2. **Support category known only after the cutoff:** 8 rows.
3. **Account attribution ties:** 279 rows, plus 3 sample references with no participant support
   (`account_004`, `audit_record_6645`, `audit_record_5569`).
4. **`case_63` completion semantics** when no evidence exists.
5. **`case_id` sort semantics** (row order only).

## 10. Sprint 4 — account-coverage audit, global assignment test, robustness

Champion reproduced first: `output.csv` `3d628b9b…f2a9`, sample predictions `b156d290…187b`;
24/24 evaluator and 21/21 pipeline tests; gold-vs-gold 100%. **Both prediction files are byte-identical
at the end of the sprint.** No change in this sprint moved a public-sample or main-set metric. All
changes below are robustness or provenance fixes.

### 10.1 Account coverage matrix (508 deletion cases)

| Account obligation | Cases | Proof / reason |
|---|---|---|
| Present → finding + delete | 54 | Direct identifier link; in-window record exists |
| Proven absent, direct observation | 120 | 404 lookup / index miss / NOT_FOUND (112 direct, 8 workflow-attributed) |
| Proven absent, scoped snapshot | 43 | Workflow-attributed; an in-window `account_service` snapshot captured after the deletion action does not list it |
| Unproven, future check only | 2 | Workflow-attributed; no snapshot after the action |
| **Link unresolved** | **279** | Every candidate account (2–6 per case) is equally attributable to another open request |
| **Not found** | **10** | No account candidate in the request's workflow window and no content link |

A per-case audit record is kept outside the package for all 289 unresolved/not-found cases. It holds:
- the subject;
- the candidate IDs;
- the activity IDs and times;
- the evidence files;
- the competing requests;
- the classification.

### 10.2 Global constraint-assignment experiment (resolver variants)

| Variant | Constraint added to the existing "≤ 1 resource per category per request" | Components where the constraint is contradicted | Entailed new assignments | Sample-case assignments |
|---|---|---|---|---|
| 1. Current resolver | Unit propagation plus observed-only proximity tie-break | — | — | — |
| 2a. Every candidate resource must belong to a candidate request | Hall-style saturation of resources | 23 of 138 | 0 | 0 |
| 2b. Every request must receive an account | Saturation of requests | 39 of 138 | 2 | 0 |
| 3. Conservative (= current) | None | — | — | — |

- Both global constraints are **falsified by the data**: many components have more resources than
  requests can absorb, or the reverse.
- Where they are feasible, they entail almost nothing (2 assignments, not verifiable on samples).
- Variant 2 is **not promoted**. The 279 unresolved links are genuinely symmetric.
- Two other signals were already tested and rejected earlier: readback `observed_at` times and
  worker-action order.

### 10.3 Verification-obligation check

The required final-state checks were derived independently from resolved resource states (category,
policy action, existence, flow and restore state, retention) and compared with the emitted
VERIFICATION steps over all 518 rows: **0 missing, 0 extra.**

### 10.4 Ambiguity boundaries

For an unresolved account link, every interpretation produces a *different* resource ID, so no single
output is correct across interpretations. The smallest evidence that would resolve one is an in-window
`account_service` lookup keyed by the subject's own identifier, like those in the readback traces. For
the 279 cases no such record exists in the corpus.

States are kept distinct in the trace (`coverage` / `notes`):
- **not found:** no candidate at all;
- **link unresolved:** candidates are shared with other requests;
- **proven absent within scope:** a scoped snapshot after the action;
- **not captured:** unproven, future check only.

### 10.5 Changes made (each verified byte-identical on real outputs)

1. **Hash-seed-independent category tie-breaks** (`planner._category`, `linking.workflow_candidates`,
   `audit.prepare`): `max(set(...))` → `max(sorted(set(...)))`. Outputs are identical across
   `PYTHONHASHSEED` 0, 1, 2, 3, 5, 42, 77, 1234 and 99999. Covered by `test_output_independent_of_hash_seed`.
2. **Explicit record-to-record references** (`Observation.references`; `Auditor._reference_links`).
   - A resource named by a subject-linked record, e.g. `source_resource_id` in evidence_57, is linked.
   - Its system comes from its own observations or from deletion-worker activity on it, never from its
     ID prefix.
   - On the real data it re-derives `case_06` → `account_006` (already assigned by workflow uniqueness)
     from explicit evidence; 0 rows change.
   - Flag: `--no-reference-links`.
3. **Defect found by a new fixture and fixed:** a resource content-linked to one request could still be
   given to another request as its "unique" workflow candidate (double attribution). Workflow
   candidates now exclude resources claimed by another request's content evidence captured on or before
   the candidate request's `audit_at`. 0 rows change on the real data.
4. **Input-order robustness:** with every input CSV row-shuffled (activity, manifest, identities, audit
   log, rules, both request files), both outputs are byte-identical.

New sprint-4 fixtures in `WorkflowAttributionFixtures`:
- a unique owner assigned with a scoped absence proof;
- one resource with two plausible owners (not assigned);
- a symmetric shared-slot matching (not forced);
- an absence claim from a snapshot taken before the action (rejected);
- a post-cutoff file with earlier internal times (ignored);
- one resource in several files (latest in-window state and citation);
- an explicit reference resolving shared-slot ambiguity without double attribution.

### 10.6 Highest-impact remaining gap and next falsifiable experiment

- **Gap:** 279 symmetric account attributions, plus 208 cases with no admissible retained-security
  record. Most of the 208 records are visible only after the cutoff (C3, 116 rows).
- **Next experiment (falsifiable):** obtain organizer clarification of the cutoff semantics, and of
  whether verification steps may name resources that are known only from the workflow and shared
  between requests. C3 is a diagnostic measured only with `--engine pipeline` (+118 uncited steps that
  pass citation-time validation but fail information provenance, §5); it is disallowed under the current
  cutoff rule. Even if the organizer explicitly changed that rule, C3 could not run under the default
  agent engine without extending its subject-link admissibility check (§5). Without clarification, no
  further evidence-backed recall is available from this corpus.

## 11. Sprint 5 — residual reconciliation and admissible recall recovery

Champion reproduced from fresh commands before editing: `output.csv` `3d628b9b…f2a9`, sample
`b156d290…187b`; 24/24 evaluator and 29/29 pipeline tests; gold-vs-gold 100%.

**PUBLIC-SAMPLE METRICS:** status 32/32, exact 15/32, P / R / F1 = 100 / 85.49 / 92.18%, citations
63/63, 0 inversions.

### 11.1 Residuals in the 17 non-exact sample cases (current evaluator)

The 17 non-exact cases differ from the reference by **missing VERIFICATION steps only** (28 steps).
Status, ordering, citation and extra-step differences are all 0.

| Missing step | Cases | Admissible support at audit_at | Recoverable without guessing? |
|---|---|---|---|
| `account_service … VERIFY_RECORD_ABSENT` | 04, 09, 10, 18, 20, 21, 22, 23, 25, 26, 27, 28, 29, 30, 31, 32 (16 steps) | `account_004`: none at all. Others: a deletion-worker activity, sometimes a 404 row (evidence_92) or a scoped empty snapshot (evidence_51), shared with 2+ requests in the same slot and file | No: symmetric attribution (and `account_004` unsupported) |
| `security_service … VERIFY_RECORD_PRESENT` | 09, 18, 20, 23, 24, 29, 30 (7 steps) | 4902 → evidence_06; 4128, 7095, 3954, 1707 → evidence_02 (all captured after the case's `audit_at`). 6645 and 5569 appear nowhere. No activity rows. | No: post-cutoff only, or unsupported |
| `analytics_platform … VERIFY_RECORD_ANONYMIZED` | 21, 25, 26, 27, 28 (5 steps) | Anonymized rows in evidence_92 plus an ANONYMIZE activity, both shared with 2–3 requests | No: symmetric attribution |

### 11.2 C2/C3 pre-cutoff recovery matrix

- All 5 sample security records recovered by C3 appear **only** in evidence_02 or evidence_06, both
  captured after the case's cutoff. None appears in any admissible file or activity row.
- Across the main set, every one of the 116 C3-recovered security records is "post-cutoff only"
  (0 have admissible pre-request support).
- C2's other gains depend on post-cutoff citations or on the same symmetric attributions.
- Relaxing only the admissible lower bound (`--window none`: all evidence captured by `audit_at`) adds
  5 sample false positives and 0 true positives. These are records from a person's *earlier* deletion
  request: case_07 `object_19` (Feb), case_17 `tombstone_17` and `account_6523` (Mar). The gold
  excludes them, so the `requested_at` lower bound stays.
- **Result: no part of the C2/C3 sample gain is admissible.** The F1 ≥ 93.5% and ≥ 17/32 exact targets
  are **not reachable** without post-cutoff information.

### 11.3 Change promoted: K1 — screenshot-referenced resource + admissible category history

- **Evidence.** case_34 (Haruka Sato, `user_11`, window 09-24 → 09-27):
  - The live admin console (evidence_04, captured 09-26, in window) lists "Support CS-8538 ·
    support_9538" on the subject's account, and its activity timeline shows "28 Aug support_9538
    created". This was transcribed as a reference-only fact: existence and owner are shown; type and
    content are not.
  - evidence_01 (captured 09-15, ≤ `audit_at`) has an `attachment_pii_scan` of `support_9538`
    (`delivery_confirmation_32.pdf`) matching `+81 90 6156 0005`.
  - `user_audit_log.csv` audit_279 shows that number is `user_11`'s *previous* phone.
- **Rule** (`planner.resolve_state`). A record's category is static. When in-window evidence leaves it
  unknown, it may come from any admissible observation (file captured and record dated ≤ `audit_at`).
  It never overrides an in-window category, and post-cutoff files are never used; three unit tests in
  `CategoryHistoryRule` pin this. Applied: SUPPORT_ATTACHMENT, belonging only to the subject (rule_06),
  so DELETE.
- **Rejected intermediate:** an earlier variant let the platform default classify the screen reference
  as SUPPORT_MESSAGE and judged it "redacted" with no visible content. That is an unsupported
  compliance claim and was replaced by the reference-only fact.
- **Row-level change (518 rows): 1 row.** case_34 INCOMPLETE → INCOMPLETE gains:
  - `FINDING support_platform support_9538 REPORT_REMAINING_RECORD evidence_04`
  - `REMEDIATION … DELETE_RECORD`
  - `VERIFICATION … VERIFY_RECORD_ABSENT`
- **Sample predictions:** byte-identical; metrics unchanged.
- **Gates:** validator PASS, 0 future citations in `output.csv`, identical across hash seeds, 32/32
  pipeline and 24/24 evaluator tests. New `output.csv` SHA-256 `ee3fd9fa…8c34`.

### 11.4 Other experiments (no promotion)

- **Identity look-back**, re-measured with the category-history rule: 0 rows changed.
- **Global assignment**, re-run on current code:
  - Resource saturation: contradicted in 23 of 138 groups, 0 forced.
  - Request saturation: contradicted in 39 of 138 groups, 2 forced (case_154, case_454).
  - The hybrid that takes only forced assignments rests on a contradicted invariant and is rejected.
- **Screenshot ticket references** (CS-3245, CS-3378, ORD/FIN refs on evidence_42): no admissible text
  evidence maps them to resources. CS-5034 appears only in a post-cutoff bundle.
- **Unsupported sample IDs.**
  - `account_004`: "004" occurs only as trace-ID suffixes.
  - `audit_record_6645`: "6645" occurs nowhere.
  - `audit_record_5569`: "5569" occurs only as ticket CS-5569 of `support_6569` (`user_405`), which is
    unrelated.
  - SQL schemas have no integer keys (only 0/1 flags).
  - No file contains a security record for `user_04`, `user_48` or `user_50`.
  - These are reported as possible sample/data inconsistencies.

### 11.5 case_63 disposition

The request authorizes deletion ("Please permanently delete the account associated with this email
address…"), so NOT_APPLICABLE is not allowed. The subject is `user_09`; there is no identity history.

The window 09-14T04:20 → 09-17T09:20 contains evidence_59, 01, 92, 51, 60 and 93. None of them carries
any `user_09` identifier. There is no deletion-worker activity near `reported_at` (only a SEND_EMAIL).
The only claim is `activity_4008` (`marketing_001` → `kwame-mensah`/`user_09`), and evidence_60 shows
that profile belongs to Soo-jin Park, `sj.park@naver.com`.

No status/step combination is fully valid under the written contract:
- COMPLETE needs proving verifications;
- INCOMPLETE needs findings;
- NOT_APPLICABLE needs non-deletion intent.

COMPLETE with `[]` is kept as the only output that asserts nothing unsupported. It stays flagged, pinned
by `test_empty_complete_only_without_admissible_evidence`, and the validator warning is not eliminated.

### 11.6 Remaining gap and next experiment

- **Gap:** 279 symmetric account attributions, plus retained-security records that are visible only
  after the cutoff. Neither can be recovered from admissible evidence.
- **Next falsifiable experiment:** organizer clarification of the cutoff semantics. C3 (+118 uncited
  obligations, measured with `--engine pipeline`) is a diagnostic experiment: its steps pass citation-time
  validation but are derived from post-cutoff source records, so they fail information provenance and are
  inadmissible under the current cutoff rule. C3 is not a valid submission option unless the organizer
  explicitly changes that rule, and enabling the flag alone would not make it compliant (§5, §10.6).
  Without such a rule change, freeze and package.

## 12. Sprint 6 — co-location linking experiment (not promoted)

**Baseline (fresh):**
- `output.csv` `ee3fd9fa…8c34`; sample predictions `b156d290…187b`.
- Tests: 24/24 evaluator, 32/32 pipeline; gold-vs-gold 100%.
- **PUBLIC-SAMPLE METRICS:** 32/32 status, 15/32 exact, P / R / F1 = 100 / 85.49 / 92.18%, 63/63
  citations, 0 inversions.
- Validator PASS (1 warning, case_63).

### 12.1 Evidence chain for the co-location hypothesis

Chain checked for each candidate: request → subject → deletion-worker action → resource → admissible
observation → policy → step.

| Case | Resource | Worker action | Admissible observation (captured ≤ audit_at) | Requests the same chain fits equally |
|---|---|---|---|---|
| 21 | `account_6440`, `analytics_5335` | `activity_3661` DELETE 09-05 11:01; `activity_3665` ANONYMIZE 11:31 | evidence_92 (09-15 11:44): 404 row / anonymized row, no identifier | 21, 23 (same reported_at 09-05 12:00, both directly linked in evidence_92) |
| 25 | `account_3136`, `analytics_1895` | `activity_3597` 09-02 10:48; `activity_3602` 11:31 | evidence_92 | 24, 25, 26 (24 is pruned for accounts because it already has `account_5334`) |
| 26 | `account_5684`, `analytics_4566` | `activity_3600` 11:08; `activity_3603` 11:31 | evidence_92 (subject identifier only in evidence_02, captured after the cutoff) | 24, 25, 26 |
| 27 | `account_3866`, `analytics_4574` | `activity_3530` 08-30 10:57; `activity_3533` 11:31 | evidence_92 (identifier only in evidence_02, after the cutoff) | 27, 28, 29 |
| 28 | `account_5482`, `analytics_6077` | `activity_3529` 10:46; `activity_3534` 11:31 | evidence_92 | 27, 28, 29 |
| 31 | `account_4857` | `activity_3682` 09-06 10:45 | none (evidence_51 is an empty account snapshot that could prove absence, but names no subject) | 14, 31, 32, 38, 40 (14/38/40 hold direct accounts only via evidence_03, captured after case_31's cutoff) |

- Every competing request was submitted before the worker action. An isolated
  "request-before-action" constraint (X6a) therefore changed **0 rows** (samples and 518), and the
  option was reverted.
- Membership in the same export does not separate the requests: each is directly linked in
  evidence_92.

### 12.2 Measured candidates

| Candidate | P / R / F1 (sample) | Exact | Res. P / R | Main rows changed | Decision |
|---|---|---|---|---|---|
| Champion | 100 / 85.49 / 92.18 | 15/32 | 100 / 74.77 | — | Keep |
| X6a: request-before-action filter | 100 / 85.49 / 92.18 | 15/32 | 100 / 74.77 | 0 | Reverted (no effect) |
| Co-location → every co-located request (`--ambiguous all`) | 78.06 / 95.85 / 86.05 | 15/32 | 66.45 / 92.79 | 289 | Rejected: +20 TP, +52 FP |

- Under the co-location counterfactual, each correct addition comes with 1–5 wrong siblings: e.g.
  case_21 gains `account_6440` (TP) plus `account_294`, 410, 7015, 8832 and 9155 (FP).
- The sample-label pattern is reproduced only by over-assignment, not by distinguishing evidence.

### 12.3 Next failure class examined: 10 cases with no account candidate

These are case_33, 35, 39, 45, 50, 52, 53, 55, 61 and 63: the image/catalog-based cases plus 45 and 63.
None has any deletion-worker activity near its `reported_at`. All account deletions inside their
request windows belong to other reporting slots. **Unrecoverable from the corpus.**

### 12.4 Tests added

In `WorkflowAttributionFixtures`:
- `test_colocation_alone_does_not_assign_in_shared_slot`
- `test_colocated_row_assigned_when_only_one_window_explains_action`

Pipeline suite: 34/34 OK. The production code is byte-identical to the sprint-5 Champion and both
outputs are unchanged.

**Packaging note:** three `__pycache__` directories appeared from an external test runner during the
sprint and were removed. The README `zip` command already excludes `*/__pycache__/*`. The packaging step
must keep that exclusion (or delete caches first), because `sys.dont_write_bytecode` only protects our
own entry points.

### 12.5 Next best experiment

Every sample residual is now classified as symmetric attribution, post-cutoff-only or unsupported. No
admissible failure class remains.

1. Freeze the Champion and build the release package. Include the report under both `evaluation/` and
   `code/evaluation/`, `log.txt` and `agent-trace.zip`, with checksum and secret checks.
2. In parallel, seek the organizer clarification on cutoff semantics. C3 remains a diagnostic measured
   with `--engine pipeline`: it passes citation-time validation but fails information provenance, so it is
   disallowed under the current cutoff rule and is not a submission option unless the organizer explicitly
   changes that rule (§5).

## 13. Sprint 7 — investigation controller and code.zip compliance

Champion reproduced first: `output.csv` `ee3fd9fa…8c34`, sample predictions `b156d290…187b`.
**Both files are byte-identical after every change in this sprint.** Three full runs, one under
`PYTHONHASHSEED=9`, all hash `ee3fd9fa…`.

### 13.1 Bounded investigation controller (`code/ghost_audit/agent.py`)

> Superseded by §16: the controller is now an agenda-driven state machine with enforced tool contracts, an
> admissibility gate and fail-closed fallback. Current stop-reason counts: 432 unresolved candidates, 76 agenda
> empty, 10 not authorized.

- `AuditAgent.investigate_case(req)` runs six stages over typed tools in `AuditTools`:
  1. **AUTHORIZE:** `classify_intent`.
  2. **IDENTIFY:** `resolve_identity`.
  3. **DISCOVER:**
     - `search_direct_evidence` (token index over observations);
     - `query_catalog`, called only when a catalog claim names one of the subject's identifiers;
     - `follow_reference`, called only from records that carry an explicit reference.
  4. **INVESTIGATE:** `query_workflow_context` (the shared, globally solved request context), then
     `resolve_resource_state` per linked resource.
  5. **COMPILE:** `compile_obligations` (the deterministic policy kernel).
  6. **VALIDATE:** `validate_contract`.
- **Query discipline.** Each query records stage, tool, reason, inputs, evidence references and a
  result summary. Queries are de-duplicated by (tool, inputs) and count against `--max-queries`
  (default 256).
- **Stop reasons:**
  - "request does not authorize deletion";
  - "identity unresolved";
  - "obligations compiled; agenda empty";
  - "obligations compiled; unresolved workflow candidates have no admissible distinguishing query";
  - "query budget exhausted". This emits INCOMPLETE `[]`, flagged; COMPLETE is never claimed without
    compiled obligations.
- **Recorded issues.** Unresolved candidates and contradictions are kept in the trace, e.g. case_63:
  the `marketing_001` catalog claim is contradicted.
- **Measured on the 518 requests:**
  - 1–13 queries per case (mean 8.5);
  - stop reasons: 430 with unresolved workflow candidates, 78 agenda empty, 10 not authorized.
- `--engine pipeline` keeps the original single pass; both engines give byte-identical outputs.
- **Shared preparation stays explicit.** `AuditTools` refuses an `Auditor` that has not run
  `prepare()`, because one case cannot settle globally competing workflow resources alone.

Example trace, case_34 (9 queries):
1. `classify_intent` → authorized.
2. `resolve_identity` → `user_11`, 7 identifier tokens.
3. `search_direct_evidence` → `account_5762`, `support_9538` [evidence_04].
4. `query_catalog` (1 claim names the subject) → accepted `account_5762` [activity_4000, evidence_04].
5. `query_workflow_context` → none.
6. `resolve_resource_state` `account_5762` → USER_PROFILE, exists, exposed [evidence_04].
7. `resolve_resource_state` `support_9538` → SUPPORT_ATTACHMENT, exists [evidence_04].
8. `compile_obligations` → INCOMPLETE, 6 steps.
9. `validate_contract` → no errors.

Stop: "obligations compiled; agenda empty".

`code/tests/test_agent.py` (12 tests) checks:
- equivalence with the reference pipeline on all 518 + 32 requests;
- stage order, and a reason on every query;
- non-deletion requests stop after one query;
- the catalog tool runs exactly when a claim names the subject;
- references are followed only from records that carry them (sample case_06: `search_001` →
  `account_006`, evidence_57);
- no repeated queries;
- the budget bound stops without fabrication;
- every evidence reference is a real file or activity, and every state reference was captured by the
  cutoff;
- unresolved candidates are reported and never emitted;
- the case_63 contradiction is recorded;
- shared context is required;
- image provenance.

### 13.2 Visual provenance

- `visual.image_provenance` records each image's SHA-256 and byte size. When Pillow is installed it
  also decodes the file (`verify()`) and records format and size: all 11 are PNG, 1800×1100–1200 or
  1500×920.
- Cached facts are used only when the hash matches and the file decodes. An undecodable image becomes
  `unreadable_image`; an unknown hash becomes `uncovered_image`.
- This is **not OCR**. The facts remain a manual visual transcription made during development (stated in
  each file's note).

### 13.3 Packaging (`code/package.py`)

- **Allowlist:**
  - `README.md`;
  - `code/**.py`;
  - `code/evaluation/*.md`;
  - `code/evaluation/sample_predictions.csv`;
  - byte-identical copies at `evaluation/evaluation_report.md` and `evaluation/usage_report.md`.
- **Reproducibility:** sorted entries, fixed 1980-01-01 timestamps, fixed mode and compression. Two
  builds are byte-identical.
- **Verification:**
  - required entries are present;
  - both report copies are identical;
  - no `dataset/`, `output.csv`, `log.txt`, gold file, cache, virtualenv, nested archive or `.env`;
  - a secret and personal-path scan passes.
- **Defects found and fixed while building the package:**
  1. Hard-coded `/home/<user>/…` dataset paths in `validator.py`, `evaluator.py`, `test_validator.py`
     and `test_edge_cases.py`. These would leak a personal path and break on other machines; they are
     now repo-relative, with a `GHOST_AUDIT_DATASET` override.
  2. Clean-extraction failures: `test_evaluator.py` could not find the gold file (2 errors), and
     `main.py` crashed with a traceback when no dataset was reachable.
  3. Fixes: the tests and CLI honour `GHOST_AUDIT_DATASET` / `--dataset`, and the CLI now exits 2 with a
     clear message.
- **Clean extraction** (archive unzipped to an empty directory, dataset supplied separately):
  - CLI output is byte-identical to the Champion;
  - tests pass: evaluator 24/24, pipeline 34/34, controller 12/12;
  - samples score F1 92.18%;
  - no `__pycache__` is created.

### 13.4 Remaining code.zip risks

| Risk | Class |
|---|---|
| Graders run the code without supplying `dataset/` next to `code/` | Hypothesis. Mitigated by `--dataset` / `GHOST_AUDIT_DATASET` and a clear exit-2 message. |
| Which report path the code judge reads | Unknown; both are present and identical. |
| `pdftotext` (poppler) absent on the grader machine | Hypothesis. PDFs are then marked `unavailable` in the inventory; 4 files and the cases depending on them would lose evidence. |
| Pillow absent | Hypothesis, low impact. Hash binding still applies; only the decode check is skipped and reported. |

## 14. Priority sprint — baseline reconciliation, validator fix, calibration gate

### 14.1 Status counts: verified from the artifacts themselves

Counts computed with Python's `csv` module over each file:

| Artifact | SHA-256 | Rows | COMPLETE / INCOMPLETE / NOT_APPLICABLE |
|---|---|---|---|
| Root `output.csv` | `ee3fd9fa5794dc48153654ef39c819d104a711668de6502e66b6ed6974c98c34` | 518 | 154 / 354 / 10 |
| Fresh default run (`--engine agent`) | identical | 518 | 154 / 354 / 10 |
| Clean extraction of `code.zip` (dataset supplied via `GHOST_AUDIT_DATASET`) | identical | 518 | 154 / 354 / 10 |

- All three files are byte-identical. **154 / 354 / 10 is correct.**
- The conflicting "378 / 120 / 20" package report is not present in the repository, `log.txt`, or any
  report.
- No counting method over these artifacts reproduces it: field-2 parsing, `,STATUS,` token counts and
  whole-file substring counts (`COMPLETE` = 508, because it also matches INCOMPLETE) were all tried.
- It does not come from the current code or outputs. Its source must be checked before it is cited
  anywhere.

### 14.2 Confirmed defect fixed: `output.check_row` on non-array JSON

- **Defect:** `check_row` raised `TypeError` for object, scalar, null and string JSON, and
  `JSONDecodeError` for invalid JSON. It now returns a clear error ("steps_json must be a JSON array,
  got dict" / "…not valid JSON").
- **Also handled:** non-object steps inside an array, steps with missing or extra fields, and unknown
  phase/operation pairs are reported instead of raising.
- **Tests:** `CheckRowRobustness` (3 tests) covers object, scalar, null, string, invalid JSON, valid
  arrays and malformed steps.
- **Prediction behaviour unchanged:** `output.csv` is byte-identical (`ee3fd9fa…`) and the sample
  predictions too (`b156d290…`). Tests: pipeline 37/37, controller 12/12, evaluator 24/24; validator
  PASS.

### 14.3 Calibration candidate (scoped co-location) — kept isolated, not promoted

Source rows for case_21 (`user_21`):
- evidence_92 `account_service.profile_lookups`: `{"resource_id": "account_6440", "status_code": 404, "profile": null}`
- evidence_92 `analytics_platform.profiles`: `{"resource_id": "analytics_5335"}`, with `customer_reference`,
  `email`, `phone` and `email_hash` all null.
- `activity_3661` (DELETE `account_6440`, 09-05 11:01:53) and `activity_3665` (ANONYMIZE `analytics_5335`,
  11:31:34) name no subject.

case_23 (`user_23`) is a **genuine competing request**:
- the same `reported_at` (09-05 12:00) and `audit_at` (09-15 12:00);
- submitted 09-02, before both actions;
- directly linked in the same file (evidence_92: `export_7364`, `search_898`);
- no directly linked account or analytics profile.

case_21 is directly linked there only through `audit_record_3737`. No schema field, trace, catalog row or
activity detail binds `account_6440` or `analytics_5335` to `user_21`. Elimination does not work either:
case_23's sample account (`account_410`) is itself an activity-only candidate shared with four other
accounts.

**Evidence required to promote the candidate**, any one of:
1. a participant-visible record tying either resource to `user_21` or its identifiers (customer
   reference, lookup key, trace or correlation key, catalog link);
2. an activity row naming the request or subject;
3. a source-backed invariant placing case_23's account and analytics on different, identified resources.

None exists in the corpus. Until T1/T2/T4 produce such a source, the candidate stays isolated and
`output.csv` is unchanged.

## 15. Sprint 10 — T4 defect validation and controlled patches

Champion before the sprint: `output.csv` `ee3fd9fa…8c34`, sample predictions `b156d290…187b`,
`code.zip` `20e119a0…`.

Each finding was reproduced from code and data, patched in an isolated copy of `code/`, and run over the
32 samples and the 518 requests. All candidates gave PUBLIC-SAMPLE METRICS of status 32/32, exact
15/32, TP=165, FP=0, FN=28, P / R / F1 = 100 / 85.49 / 92.18%, citations 63/63, 0 ordering errors, and
validator PASS (1 warning).

| Rank | Defect | Reproduced from | Main-set change | Decision |
|---|---|---|---|---|
| 1 | **D4** Lexicographic suppression in `resolve_workflow` | When one request had ≥ 2 workflow candidates of a capacity-1 category that only it explained, unit propagation took the alphabetically first and pruned the rest (case_102 got `account_2056` over `account_8166`, `8473`, `868`) | 16 rows: arbitrary or cascaded account checks removed (case_102, 103, 152, 212, 253, 282, 313, 328, 342, 382, 402, 452, 485, 492, 502, 512); no step added | **KEEP**: such candidates now stay unresolved |
| 2 | **D2** Contradictory catalog identities | `catalog_links` accepted a claim when *either* the email or the user matched the subject | 0 (all 12 real claims are internally consistent) | **KEEP**: claims whose email owner and named user are different known people are rejected; partial claims are still accepted |
| 3 | **D1** Snapshot/store identity | SQL JSON-record tables: observation store `table:collection`, scope store `table`, so they never matched | 0 | **KEEP D1b** (scope store = `table:collection`). Prefix matching rejected as looser. |
| 4 | **D3** Cross-file aggregation at the latest instant | Only one file was kept when several files shared the latest observation time | 0 (no real ties; broader per-store-latest variant also 0 disagreements) | **KEEP narrow fix** (union of files at the latest instant; cite a file holding the surviving copy). Broad per-store-latest rejected: stale replica rows could override newer evidence. |
| 5 | **D6** Phone recognition | `+`-anchored recognizer: 77 detections (74 known identity phones) | n/a | **REJECT broadening**: 0 known phones occur without `+`; a national-format pattern only adds false hits (trace IDs, 20-digit numbers, fragments). Tests added. |
| 6 | **D5** Unresolved identity (case_63) | Unchanged: no admissible evidence; any status is semantically imperfect | 0 | **DEFER** (documented in §11.5) |

- **Interaction check:** the combined D1b + D2 + D3 + D4 candidate is byte-identical to D4 alone.
- **Promotion order:** D4 → D2 → D1b → D3. The output hash changed only at D4.

New regression tests in `Sprint10Regressions`, 9 tests:
- **Defect tests** (each fails on the previous Champion code and passes now):
  - two exclusive same-category candidates are not picked;
  - a contradictory catalog claim is rejected;
  - a later snapshot of the same table and collection proves absence;
  - simultaneous observations from two files are aggregated.
- **Closest-false-positive controls** (pass on both versions):
  - a single exclusive candidate is still assigned;
  - partial catalog claims are accepted;
  - a different table proves nothing;
  - a strictly later lookup still decides;
  - phone positives and adversarial negatives.

**Release gates:**
- **Tests:** evaluator 24/24, pipeline 46/46, controller 12/12.
- **Output:** byte-identical across the default run, `PYTHONHASHSEED=7` and `--engine pipeline`;
  `output.csv` `d6795660b65f6509…`, 518 rows, 154 / 354 / 10, 0 future citations, validator PASS (1 warning).
- **Samples:** byte-identical (`b156d290…`).
- **Runtime:** 2.38 s, 48.0 MB.
- **Main-set account coverage:** established 203 (was 219), ambiguous 295, none 10.

## 16. Sprint 11 — evidence-driven controller, tool contracts, fail-closed execution

Champion reproduced first from a frozen copy: `output.csv` `d6795660…1e4e`, samples `b156d290…187b`, 82/82 tests.
**Both files are byte-identical after the change** (default run, `--engine pipeline`, `PYTHONHASHSEED` 0 / 1 / 4242).
No predictions changed; this sprint changes architecture and robustness only.

### 16.1 Code-rubric gap analysis (measured on the previous controller)

| Dimension | Finding | Label | How it was verified |
|---|---|---|---|
| A. Architecture | The controller was a fixed script: 518 cases produced 4 tool-sequence shapes, 496 of them the same 7 tools. Only intent, identity, catalog and reference checks branched. No agenda existed, although "agenda empty" was reported as a stop reason. | PROVEN | Tool sequences collapsed per case from `--trace`; code reading of the old `investigate_case` (one straight-line `try` block) |
| A. Architecture | References were followed in one pass, from links known before workflow assignment; resources added by that pass were never expanded. | PROVEN (code); 0 effect on this corpus | Probe: 0 unfollowed references from catalog-, reference- or workflow-linked resources |
| B. Tool craft | Tools were plain methods: no declared inputs, outputs or preconditions, and nothing checked calls against a contract. | PROVEN | Code reading |
| B. Prompt craft | No prompt is sent to any model: no model is called. (The instruction documents under `code/prompts/`, added later, describe deterministic components; see §21.) | PROVEN, by design | `usage_report.md`; see §16.2 |
| C. Robustness | One tool exception in one case aborted all 518 rows, and no `output.csv` was written. | PROVEN | Injected `RuntimeError` into `resolve_resource_state` for case_100: the run raised |
| C. Robustness | Nothing checked that emitted resources were linked or that citations existed and were dated at or before `audit_at` (only the tests did). | PROVEN | Code reading; now `check_admissibility` |
| C. Robustness | Budget exhaustion discards discovered evidence (INCOMPLETE `[]`). | PROVEN; kept deliberately (fail closed) | case_34 at budget 4 |
| D. Engineering | Resource-state resolution, catalog-document interpretation and reference resolution were implemented twice (pipeline and controller). | PROVEN | Diff of `audit.py` and the old `agent.py` |
| D. Engineering | `code.zip` was older than the source: 5 test or harness files differed and `code/tests/__init__.py` was missing. | PROVEN | `package.py --verify code.zip` hashes vs working tree |
| D. Engineering | No hard-coded machine paths or secrets in the source. | PROVEN | `package.py` secret/path scan |

### 16.2 Candidates compared

1. **Current deterministic controller.** Correct, but scripted; see A and C above.
2. **Evidence-driven deterministic controller (implemented).** Explicit `Investigation` state, a priority agenda,
   result-driven routing, a phase state machine, enforced tool contracts, an admissibility gate and fail-closed
   fallback.
3. **Optional model-driven investigator (not built, DEFERRED).** On this corpus it could not change an answer
   without breaking a rule:
   - *Intent:* all 518 + 32 decisions come from an explicit rule match (508 erasure, 10 explicit non-deletion
     scope, 0 from the "nothing expressed" default), so a model opinion would never decide a case.
   - *Investigation:* the deterministic agenda already runs every admissible query (max 14 per case, budget 256,
     never reached). A model choosing among the same tools can only skip queries, which lowers recall, or propose
     links outside the evidence, which is forbidden.
   - *Remaining 28 sample false negatives:* §11.1 traces all of them (16 account checks, 7 security records, 5
     analytics records) to symmetric ties between requests, post-cutoff-only captures, or resources absent from
     participant data. Recovering any of them would need an unsupported attribution.
   - *Environment:* no model credentials or network access; the solution is offline.

   Adding a model here would be decorative, so it was not built.

### 16.3 What changed (`code/ghost_audit/`)

- `agent.py` (rewritten):
  - **Tool registry.** `ToolSpec`/`TOOLS` declares 10 tools; `check_inputs` enforces each contract on every
    queue and execute; `describe_tools()` / `--list-tools` prints them.
  - **State and loop.** `Investigation` holds the state; tasks are `(phase, wave, key, seq)`; per-tool handlers
    route results; `_advance` performs the phase transitions.
  - **Admissibility.** The new `check_admissibility` tool checks that steps name only linked resources and cite
    ids that are known and dated at or before `audit_at`.
  - **Fallback.** On a tool failure, contract violation, controller error or rejected row, the case falls back to
    the reference pipeline, re-validated; otherwise it fails closed.
- `audit.py`:
  - one shared implementation each of `resource_state`, `document_observations`, `references_from` and
    `reference_system`, used by both engines;
  - reference links are followed in waves to closure, and never from workflow-inferred resources.
- `loaders.py`: keeps `audit_id → event_time` so `audit_id` citations can be validated.
- `main.py`: `--list-tools`.

### 16.4 Tests (`code/tests/test_controller.py`, 21) mapped to requirements

| Requirement | Tests |
|---|---|
| Routing follows returned evidence | `reference_chain_followed_to_closure`, `no_reference_in_evidence_means_no_reference_query`, `catalog_queried_only_when_a_claim_names_the_subject` |
| Terminates under an explicit budget | `budget_stops_a_runaway_agenda` (an endless reference stream stops at exactly 12 queries), `reference_cycle_terminates` |
| Repeated queries are suppressed | `repeated_queries_are_suppressed` |
| Tool failure, malformed output and contradictory evidence cannot bypass validation | `tool_exception_falls_back_to_reference_pipeline`, `failed_fallback_fails_closed`, `malformed_tool_output_cannot_bypass_validation`, `contradicted_catalog_claim_is_recorded_not_linked` |
| No unsupported resource or citation reaches output | `unlinked_resource_cannot_reach_output`, `unknown_or_post_cutoff_citation_cannot_reach_output`, `every_emitted_step_is_linked_and_admissible` (real data) |
| Non-deletion and temporal cutoff enforced | `non_deletion_request_stops_after_authorization`, `unknown_identity_stops_after_identification`, `post_cutoff_evidence_never_links_or_cites` |
| Deterministic fallback reproduces the Champion | `forced_fallback_reproduces_the_controller_output` (every case forced onto the fallback; all 518 rows identical) |
| Tool definitions take part in execution | `every_query_is_a_declared_tool_in_its_declared_phase`, `changing_a_contract_changes_execution`, `inputs_are_checked_against_the_contract`, `cli_lists_the_registry` |

**Mutation check.** Each mutant copies the candidate and removes one capability:
- reference closure in the pipeline;
- multi-hop routing in the controller;
- admissibility gate;
- de-duplication;
- contract enforcement;
- fallback;
- budget (×100);
- catalog-claim guard.

All 8 mutants are detected; each fails the test written for its capability.

### 16.5 Release gates

| Gate | Result |
|---|---|
| Public samples | 32/32 status, exact 15/32, precision 100% (TP 165, FP 0), recall 85.49% (FN 28), F1 92.18%, citations 63/63, 0 inversions. Unchanged. |
| Full run | 518 rows, 154 / 354 / 10, self-check errors 0, validator PASSED (1 known warning, case_63) |
| Determinism | `output.csv` `d6795660…` for agent, pipeline and three hash seeds |
| Controller trace | 0 fallbacks and 0 admissibility violations on the real data. Every case's query log is identical to the previous controller's, plus one `check_admissibility` call. |
| Tests | evaluator 24, pipeline 46, agent 12, controller 21; `unittest discover -s code` 103, all OK |
| Runtime (mean of 5) | 1.98 s → 2.31 s; peak memory 47.2 → 48.7 MB. Contract checks and the gate account for the difference. |

Not established by this sprint: any change in the organizer's code-quality score. These are local tests, not
the organizer's scorer.

## 17. Sprints 12–13 — workflow assignment depends on resource names (known limitation, kept)

Champion: `output.csv` `d6795660…1e4e`, samples `b156d290…187b` (public: 32/32 status, 15/32 exact, TP 165 / FP 0 /
FN 28, F1 92.18%, citations 63/63, 0 inversions). An external audit reported that renaming opaque resource IDs changes
the answers. Reproduced with a harness that renames IDs consistently in every text input, re-runs, maps the predictions
back and re-serializes them.

### 17.1 Reproduction and cause (proven)

- **Public effect:** renaming drops the public score to 14/32 exact, TP 164 / FP 1 / FN 29 (`case_03`).
- **Main-set effect:** 7 rows change: case_44, 195, 196, 326, 327, 374 and 375.
- **What does not matter:** row shuffles, JSON key order, hash seeds, account-only renamings and ±45 s timestamp jitter
  change nothing. Renaming analytics IDs does.
- **Cause:** `linking.resolve_workflow` walks `sorted(by_rid)` and commits the first resource that clears the 5-minute
  proximity margin; capacity propagation then forces the other resource onto the runner-up request.
- **Shape of every affected component:** two analytics records are acted on 15–45 s apart, and both are nearer the same
  request. Example: `analytics_9406` and `analytics_5589` are about 6.5–6.75 min from case_44's nearest anchor and about
  23.7–23.9 min from case_03's. Only the identifier order decides which goes where.

### 17.2 Is there an admissible signal? (tested on content-grounded data, never on gold)

Method: same-slot request pairs whose owners are known from content evidence; hide the owners and test whether each
signal recovers them.

| Signal | Result | Verdict |
|---|---|---|
| Nearest worker anchor, margin ≥ 5 min (the shipped proximity rule) | USER_PROFILE 49 right / 49 wrong; ANALYTICS 10 / 12 | chance |
| Minimum total temporal cost over the pair (profile categories) | 5 right / 8 wrong; all 52 USER_PROFILE pairs tied exactly | chance |
| Worker-stream adjacency across categories | 207 same-subject boundaries vs 207–228 under within-batch permutation (95%) | chance |
| Readback-log adjacency | 18/202 consecutive reads share a subject; for case_03 it points to the *other* record | none |
| `audience_cohort` | 4 values; no identity field to link them to | inadmissible |

Mechanism: the worker processes one category at a time (all accounts, then analytics, then support, flows and backups)
in an arbitrary request order. "Nearest anchor" therefore measures how many later categories a request has, not
ownership.

### 17.3 Candidates (each in an isolated copy, scored with the unchanged evaluator)

| Candidate | Renaming (10 transforms) | ±45 s jitter | Public exact | TP / FP / FN | Main-set change |
|---|---|---|---|---|---|
| A. Shipped greedy solver | fails (7 main + 1 public row) | stable | 15/32 | 165 / 0 / 28 | — |
| B. Global minimum-cost matching | invariant | fails (adds a public FP) | 15/32 | 165 / 0 / 28 | −14 steps |
| C5. Every maximum matching, 5-min margin | invariant | stable | 14/32 | 164 / 0 / 29 | −17 steps |
| C∞. Every maximum matching, no timing | invariant | stable | 14/32 | 164 / 0 / 29 | −45 steps |

All candidates only remove steps (no status change; the validator passes).

### 17.4 Why the promotion gate cannot be met (smallest counterexample, proven)

The gate asked for invariance to renaming and ±45 s jitter with no regression on the public metrics. In the
case_03 / case_44 component:
- Shifting the two activities by 15 s each produces exactly the renamed input. `test_invariance.py` asserts this.
- A solver with both invariances must therefore treat `analytics_9406` and `analytics_5589` identically. With one
  analytics profile per request, it can assign neither.
- That loses case_03's true positive. The only remaining distinguishing features are shown above to be uninformative.

**Decision: keep A.** The limitation is pinned by an `expectedFailure` test, which will report an unexpected success if
it is ever removed. C∞ is the evidence-grounded alternative should the public-metric constraint be relaxed. Its expected
hidden-set effect, if the timing decisions are coin flips, is about −0.1 F1 points and about −1.6 exact rows; this is
not measured.

## 18. Sprint 13 — fail-closed handling of unreadable evidence

### 18.1 Defect (reproduced on fresh dataset copies)

Every one of these exited 0 and overwrote `output.csv`:
- `pdftotext` not installed;
- a corrupt PDF;
- malformed JSON;
- a missing evidence file;
- a modified screenshot.

Four of them silently changed predictions. Without `pdftotext`, case_33 became COMPLETE with `[]`.

### 18.2 Change

- **`ghost_audit/evidence.py`:**
  - `EvidenceStore.gaps()` lists every manifest file that was not actually read;
  - a missing `pdftotext` (`unavailable`) is distinguished from an unreadable PDF (`unreadable`);
  - missing and unsupported files carry a reason.
- **`ghost_audit/loaders.py`:** the required CSV columns per input file.
- **`main.py`:**
  - checks the columns before running (exit 2);
  - if any evidence file is unread, names each file and its reason, writes nothing (a good `output.csv` is never
    clobbered) and exits 3;
  - `--allow-unparsed-evidence` writes a degraded run but exits 4, never 0.

### 18.3 Evidence

- **Fault scenarios:** all five now exit 3 with the previous output untouched.
- **Tests:** `test_evidence_gate.py` (12) covers each fault, the explicit degraded run, missing columns and files, and
  the real-data `pdftotext` case (exit 3; with the flag, exit 4 and exactly one changed row, case_33).
- **Mutation check:** four mutants (gate disabled, missing file treated as parsed, degraded run reported as success,
  column check removed) are each detected.
- **Ordinary runs are byte-identical:** `output.csv` `d6795660…` (agent and pipeline engines, `PYTHONHASHSEED` 3 and 77)
  and samples `b156d290…`.
- **Runtime:** 1.74 s against 1.69 s (6 interleaved runs each), within noise.

## 19. Sprint 14 — one-record-per-request limited to profile categories

### 19.1 Defect

`linking.resolve_workflow` treated every category as one record per request. Two consequences:
- **D4 rivals check:** when a request had two or more anonymous workflow candidates of any category, it assigned none
  of them.
- **`prune()` eliminations:** a request already holding a record of the category could take no further workflow
  candidate of that category, and was eliminated from contests for it.

The evidence contradicts this for most categories. Among content-linked resources, five requests hold two or more
support records and case_45 holds six security records. No request holds two USER_PROFILE or two ANALYTICS_PROFILE
records.

### 19.2 Change, and the counterexample that amended it

**First version.** `CAPACITY_ONE = {USER_PROFILE, ANALYTICS_PROFILE}`; `prune()` and the D4 rivals check applied only
to these categories. It was byte-identical on the default run.

**Counterexample (proven).** An independent read-only reviewer falsified it under a non-default option:
- with `--activity-only USER_PROFILE,ANALYTICS_PROFILE,SUPPORT_ATTACHMENT,SEARCH_INDEX_ENTRY`, 28 rows changed;
- case_202 gained `search_4062`, `search_8463` and `search_9921`, three entries that a later export attributes to three
  other people; two of their deletions had FAILED;
- the blanket one-per-request rule had been the only guard for candidates known solely from worker activity;
- candidates of unknown category were also treated as unlimited.

**Amended rule (`linking.one_per_request`).** A candidate counts against a one-per-request limit in its category if
any of these hold:
- its category is a profile category;
- its category is unknown;
- it is known only from workflow activity.

The reviewer then showed (proven) that both the first amendment and a group-drop fix still made some synthetic
outcomes depend on which resource name sorted first.

**Final structure: `resolve_workflow` now works in two passes.**
1. **Pass 1, unlimited resources** (observed, co-located, non-profile). These constrain nothing, so each is decided on
   its own: by a single remaining candidate, or by the per-resource proximity rule (`nearest`). Their categories then
   count as held.
2. **Pass 2, limited resources.** These go through the unchanged production propagation.

**Third review (proven).** A resource that is observed for one request but activity-only for another had been pruned
per candidate, which made one synthetic instance name-dependent. Pass 2 now uses production's `prune()`: every pass-2
resource has a limited candidate, so it is limited as a whole.

**Name-dependence fuzz.** Three seeds × 20,000 random instances × two margins, four renamings each, run twice: once
with uniform limited status per resource and once with status drawn per request:
- 0 instances depend on names under the change but not under production;
- the change depends on names in fewer instances than production (seed 1: 146 vs 239 without a margin, 314 vs 500 with
  it).

**Byte-identity.** The change is identical to production under 12 CLI configurations, including the extended
`--activity-only` runs, `--ambiguous all`, no proximity, strict co-location, no workflow links, identity lookback and
no evidence window.

### 19.3 Fixtures and effect

Synthetic fixtures (production → change):

| Fixture | Production | After the change |
|---|---|---|
| Two anonymous support records of one request | none | both |
| Two anonymous search entries | none | both |
| Direct support record plus an anonymous one | anonymous one dropped | assigned |
| Support record contested by two requests, one already holding a support record | given to the other request by elimination (unsupported) | unresolved |
| End-to-end, identifier-less support tickets co-located with the subject's ticket and acted on by the worker | ignored | attributed |

Unchanged:
- two anonymous analytics profiles (none assigned);
- account elimination when the rival already holds an account;
- decoys, proximity, symmetric pairs, cascades, replica-only records;
- invariance under renaming and shuffling.

Twelve tests were added (`test_pipeline.py::CapacityOneScope`). They include the reviewer's three counterexamples
and a fuzz-found mixed component. Results by version:
- production fails the six multiplicity and semantics tests;
- the intermediate versions fail the name-invariance tests aimed at them;
- the final version passes all twelve.

**Not addressed (pre-existing, non-default).** With `--proximity-margin 0`, `nearest()` accepts an exact tie and breaks
it by case order. Production behaves the same.

### 19.4 Release gates

- In this data every workflow candidate is a profile record, so predictions do not move:
  - `output.csv` `d6795660…` (agent and pipeline engines, `PYTHONHASHSEED` 5);
  - samples `b156d290…`;
  - public metrics unchanged (15/32 exact, TP 165 / FP 0 / FN 28, citations 63/63, 0 inversions);
  - validator PASSED.
- 135 tests OK (1 expected failure) at the Sprint 14 baseline (expanded to 204 tests in Sprints 15–17).
- The benefit is latent: inputs with multiple anonymous support or search records. It does not change the score on
  this dataset.

### 19.5 Evaluator no longer reports metrics without the public answers

**Defect (found by the same reviewer, reproduced).** The evaluate command was run from an extracted `code.zip` with no
`dataset/` beside it, or with a wrong `--golden` path. It printed `Status Accuracy 0.00% (0/0)` and 100% precision and
recall, and exited 0.

**Fix.** `code/evaluation/main.py evaluate` now exits 2 with an error when the answers are missing or empty. Two
subprocess tests cover an explicit wrong path and an unresolvable default, and both fail if the check is removed.


## 20. Sprints 15–17 — Cumulative Hardening & Release Safety Integration

### 20.1 Prompts & Configuration Deliverable Integration
- **Deliverables added:** Documents under `code/prompts/` (mirrored to archive `prompts/`): `agent_system_prompt.md`, `intent_classification.md` and `visual_transcription.md`. The first versions contained material that the code and data contradict; §21 records the defect and the rewrite.
- **Configuration loading:** Standalone JSON configuration (`code/config/audit_config.json`, mirrored to `config/audit_config.json`) specifying effective system defaults loaded by `code/main.py` with CLI flag precedence.
- **Verification:** Unit tests added in `code/tests/test_config_prompts.py` (2 tests). They checked only that the files exist and are longer than 200 characters, which is why the defects in §21 passed; §21 adds 6 accuracy tests.

### 20.2 Executable Semantic Precondition Guards
- **Defect / Rubric Gap:** `AuditAgent.describe_tools()` previously lacked executable runtime entry barriers for declared preconditions.
- **Implementation:** Added executable semantic guard functions in `code/ghost_audit/agent.py` enforced in `check_preconditions` before task dispatch.
- **Verification:** 14 hostile contract, regression, and positive control tests in `code/tests/test_agent_preconditions.py` (14 tests, 100% pass).

### 20.3 Hardened Evidence-Step Citation Provenance
- **Defect / Rubric Gap:** Admissibility gates previously validated that an evidence ID was known and prior to cutoff, without proving that the observation substantiated the specific operation, state, and policy action.
- **Implementation:** Implemented `AuditTools._substantiates()` verifying: direct observation matching operation/policy/presence/absence/PII/flow/restore; store-matching scoped absence; and catalog-backed proof.
- **Verification:** Tests added in `code/tests/test_evidence_provenance.py` (7 tests) and `code/tests/test_provenance_bypass_hunt.py` (7 tests) (14 tests total, 100% pass).

### 20.4 All-or-Nothing Atomic Output Publication
- **Defect / Rubric Gap:** A failed run or crash previously had the potential to overwrite or truncate an existing valid output file on disk.
- **Implementation:** Implemented `publish_csv` in `code/ghost_audit/output.py` with atomic replacement (`tempfile.mkstemp` + `os.replace`), pre-publication validation, permission preservation, and immediate cleanup on exception. Updated `code/main.py` with explicit pre-run directory sanity checks and failure exit codes (1, 2, 3, 5, 6).
- **Verification:** 27 adversarial tests in `code/tests/test_publication.py` and 12 recovery tests in `code/tests/test_recovery.py` (39 tests total, 100% pass).
- **Release Parity:** Preserves 100% bitwise parity on `output.csv` (SHA-256 `d6795660…`) and `sample_predictions.csv` (SHA-256 `b156d290…`). Full test suite with dataset: 204 discovered tests (202 passed, 1 expected failure, 1 skipped: `test_unresolvable_default_golden_is_an_error`). Clean-room suite without dataset: 204 discovered methods, 145 evaluated in `testsRun` (29 passed, 1 expected failure, 125 recorded skip events; 174 individual methods skipped).


## 21. Prompt documents contradicted the code and data (found, reproduced, fixed)

**Defect (PROVEN, by reading the files in `code.zip` `fa27269f…` against the code and dataset).** The three
documents under `code/prompts/` described things the submission does not have:

| Document | What it claimed | What is true |
|---|---|---|
| `visual_transcription.md` | Inventory of 11 screenshots with SHA-256s and resources | 5 of the 11 file names do not exist in `dataset/evidence/images/` (`identity_verification_workbench…`, `crm_profile_viewer…`, `order_management_system…`, `data_warehouse_query…`, `backup_restore_drill…`), nor do their hashes or resources (`kyc_5541`, `crm_8812`, `order_9201`, `warehouse_3390`, `snapshot_7110`). 5 real images were missing. The support image was listed as `support_3104`; the cache holds `support_1034`. |
| `visual_transcription.md` | "the exact system prompts … utilized during this transcription" | No such prompt existed. The images were opened with the coding assistant's file viewer in the development session (2026-10-24, about 13:16 UTC); see the session trace. |
| `agent_system_prompt.md` | A "You are an autonomous … agent" system prompt, and seven tool names | No model receives any prompt. None of `evaluate_request_intent`, `resolve_subject_identity`, `catalog_claims_for_subject`, `find_reference_links`, `retrieve_active_flows`, `retrieve_backup_restore_risks` or `plan_case_actions` exists. The registry has ten other tools (`--list-tools`). |
| `intent_classification.md` | A "You are a regulatory compliance … agent" prompt, and a calibration table | The table attached invented texts to real case ids. For example, `case_63` was shown as a marketing opt-out (`false`), but its real text is a deletion request, and `output.csv` has it as COMPLETE. The table's Spanish example ("…eliminar mi perfil") is classified as *not* deletion by `intent.py`, because `perfil` was not a recognised target. |

The two original tests checked only that the files existed and were over 200 characters.

**Fix.**
- The three documents were rewritten to describe what exists, and each states that no model receives it. File
  names are unchanged.
- `intent.py` now also accepts `perfil`, `profil`, `プロフィール` and `프로필` as targets.
- 6 tests in `test_config_prompts.py` tie each document to the code or the data:
  - the tool table must equal the enforced registry;
  - every intent example must get its stated label from `classify()`;
  - every quoted request must be the dataset text of the named case;
  - the image table must equal the `visual.py` cache (hashes and resources) and the PNG files in the dataset;
  - no document may present a "You are …" prompt.
- `test_pipeline.py::IntentTests` gains 4 profile-only requests (es, fr, ja, ko).

**Verification.**
- All 6 new accuracy tests fail on the previous documents, and the 4 new intent cases fail on the previous `intent.py`.
- Five deliberate breaks are each caught: a renamed tool, one changed hash digit, a flipped intent label, the old
  `support_3104` id, and the reverted classifier change.
- `output.csv` and `sample_predictions.csv` are byte-identical (`d6795660…`, `b156d290…`), as is the pipeline engine.
  None of the 550 participant requests depends on the new target words.

**Score relevance: UNKNOWN.** This changes no prediction. It removes statements a reviewer could check and find
false.

**Independent review.** A read-only reviewer subagent checked every concrete claim against the code, the dataset and
the session transcript, and returned ACCEPT-WITH-CONDITIONS. Its conditions were:
1. The README overstated the new tests. They check the tables, not the prose. The wording now says so.
2. Three prose sentences were imprecise and have been corrected:
   - the reference-only screenshot fields do take part in the state calculation;
   - an unresolved state exits 5 only when the reference pipeline also fails;
   - non-English non-deletion patterns cover negation only.

The persona check now matches "you are" anywhere in a document, not only at the start of a line.

**Promotion preflight.** A line-by-line audit before promotion made four more corrections:
- each document now says that it is a specification reconstructed from the code, not a verbatim historical prompt;
- the cutoff rule is qualified by the default configuration and the workflow window;
- the assignment rule now describes the 5-minute proximity tie-break;
- the cross-check sentence names the files actually searched.

`IntentTests` gained 6 negative controls, so that negation in es/pt/fr/ja/ko still overrides the new profile targets.

## 22. Temporal attribution conflicts in activity-only account inference (documentation only)

This section records two conflicts found by a read-only audit of the live output. It changes no
prediction, code, dataset, or gold. The audited full output is `output.csv`
(SHA-256 `d6795660b65f65090a2f06d928303e91d80b8603974f5208ee00bc1bfc781e4e`); the sample
predictions are unchanged at `b156d290a9fda76a83b79ad78e601935e8389228b5b0d585633881425d99187b`.

**`case_192` / `account_8423`.**
- Verified audit cutoff: `2026-07-18T11:00:00Z` (`deletion_requests.csv`).
- The live output's step 9 is `VERIFICATION` `account_service` / `account_8423` /
  `VERIFY_RECORD_ABSENT` (a future check with an empty `evidence_id`). The attribution rests on
  deletion-worker `activity_2600` (`2026-06-30T11:12:29Z`, `DELETE_RECORD`, reported `FAILED`) and
  the scoped-absence observation `evidence_20` (`customer_data_read_replica_capture`, captured
  `2026-07-08T09:00:00Z`; store `customer_data_read_replica:customer_profiles`).
- A later direct claim in `case_177` (audit `2026-07-23T12:00:00Z`) cites `evidence_84` (captured
  `2026-07-23T11:43:30Z`), a direct observation of `account_8423` for another subject. It was
  captured after the earlier cutoff and is not cited by `case_192`.

**`case_262` / `account_2981`.**
- Verified audit cutoff: `2026-06-13T11:00:00Z` (`deletion_requests.csv`). The audit brief listed
  `2026-07-13T11:00:00Z`; that date is inconsistent with the dataset and with the stated
  `2026-06-18` later claim, so it is treated as a transcription error. The verified `audit_at` is
  used here.
- The live output's step 3 is `VERIFICATION` `account_service` / `account_2981` /
  `VERIFY_RECORD_ABSENT` (a future check with an empty `evidence_id`). The attribution rests on
  deletion-worker `activity_2236` (`2026-06-06T10:46:22Z`, `DELETE_RECORD`, reported `SUCCESS`) and
  the scoped-absence observation `evidence_24` (`customer_data_read_replica_capture`, captured
  `2026-06-13T11:00:00Z`; store `customer_data_read_replica:customer_profiles`).
- A later direct claim in `case_247` (audit `2026-06-18T12:00:00Z`) cites `evidence_78` (captured
  `2026-06-18T11:44:03Z`), a direct observation of `account_2981` for another subject. It was
  captured after the earlier cutoff and is not cited by `case_262`.

**PROVEN.** The later direct claims conflict with the earlier workflow-based attributions. The
records, cutoffs, capture times and current steps above were inspected. Both later claims were
captured after the respective earlier cutoffs.

**UNKNOWN.** Whether the hidden grader penalizes either earlier step, and what any score impact
would be. The earlier steps are not proven incorrect solely because later evidence exists: at each
cutoff the account was identifier-less, and the deletion-worker activity plus the scoped absence
were the support available at that time. The conflict exposes uncertainty in activity-only ownership
inference; it does not retroactively make the earlier steps evidence-backed by the later claim.

**No admissible correction has been demonstrated.** Suppressing the two would require one of:
(a) using later evidence as an input or citation for the earlier cases, contrary to AGENTS §6.2;
(b) dropping all activity-only accounts, which would also remove four public-sample account
verification steps that match the public gold (`case_01`, `case_02`, `case_07`, `case_08`); or
(c) changing the workflow anchor or widening the assignment competition. The public-gold account
activities are within 1.23 hours of `reported_at`; widening to the request window adds other-slot
candidates, and the global-assignment variants in §10.2 are ambiguous. Neither is supported. The
later evidence is not used as a prediction input or citation for the earlier cases anywhere in the
release.

**No prediction change.** This section is documentation only; the full-output and sample-prediction
hashes above are unchanged.

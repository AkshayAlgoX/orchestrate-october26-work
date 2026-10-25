# HackerRank Orchestrate - October 2026

## Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must first determine whether deletion was actually requested. It then returns `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, reports any remaining data or recreation risk, and produces the ordered remediation and verification steps required to finish an authorized deletion safely.

Read [`problem_statement.md`](./problem_statement.md) for the complete challenge contract.

---

## Solution: setup and run

Requirements: Python 3.12 (standard library only) and `pdftotext` (poppler-utils) on `PATH` for the 4 PDF evidence files. No API keys, no network, no model calls at run time.

If the participant `dataset/` folder is not next to `code/` (e.g. in an extracted `code.zip`), point to it with
`--dataset /path/to/dataset` or `export GHOST_AUDIT_DATASET=/path/to/dataset` (tests honour the variable too).

```bash
python3 code/main.py                       # audits dataset/deletion_requests.csv -> ./output.csv (518 rows)
python3 code/main.py --dataset /path/to/dataset --output /path/to/output.csv
python3 code/evaluation/main.py validate output.csv --requests dataset/deletion_requests.csv

# public-sample check (never overwrites dataset/sample_output.csv)
python3 code/main.py --requests dataset/sample_deletion_requests.csv --output code/evaluation/sample_predictions.csv
python3 code/evaluation/main.py evaluate code/evaluation/sample_predictions.csv   # exits 2 if the answers are missing

# tests
python3 code/evaluation/main.py test               # output-contract validator and evaluator-formula tests (26)
python3 code/tests/test_pipeline.py                # pipeline property, metamorphic and authored-scenario tests (58)
python3 code/tests/test_agent.py                   # investigation-controller behaviour tests on the real dataset (12)
python3 code/tests/test_controller.py              # routing, budget, fail-closed and tool-contract tests (21)
python3 code/tests/test_evidence_gate.py           # unreadable evidence or malformed input never yields a "successful" run (12)
python3 code/tests/test_invariance.py              # row order, JSON key order, account renaming, +-45 s jitter; naming limit (6)
python3 code/tests/test_config_prompts.py          # configuration loading; prompt documents checked against code and data (8)
python3 code/tests/test_agent_preconditions.py     # semantic precondition guards, red-team regression & contract tests (14)
python3 code/tests/test_recovery.py                # fault isolation, resource-scoped degradation, fail-closed handling (12)
python3 code/tests/test_publication.py             # all-or-nothing publication, destination preservation, atomic rename, cleanup (27)
python3 code/tests/test_evidence_provenance.py     # evidence-step substantiation, direct observations, scoped absence (7)
python3 code/tests/test_provenance_bypass_hunt.py  # adversarial citation verification, mutation detection, falsification resistance (7)
python3 -m unittest discover -s code               # full test suite (210 tests, honours GHOST_AUDIT_DATASET)

# reproducible submission archive (allowlist; reports under evaluation/ and code/evaluation/)
python3 code/package.py --out code.zip    # builds, then verifies manifest, report copies, secrets, exclusions

# optional diagnostics
python3 code/main.py --trace /tmp/traces.jsonl --inventory
python3 code/main.py --list-tools          # the investigation tool contracts, as JSON
```

Exit status of `code/main.py`:

- `0`: success.
- `1`: contract self-check errors.
- `2`: a required input file or column is missing.
- `3`: an evidence file could not be read (for example a corrupt file, or `pdftotext` not installed); **no output is
  written**, because unread evidence can hide remaining personal data and turn an INCOMPLETE case into a false COMPLETE.
- `4`: a degraded run explicitly requested with `--allow-unparsed-evidence`; output is written but the run is never
  reported as successful.
- `5`: at least one case was not fully investigated (query budget, failed-closed tool path, or unresolved resource state). No output is published unless `--allow-incomplete-output` is explicitly passed; that diagnostic publication exits `5`, or `4` if evidence was also left unread under `--allow-unparsed-evidence`.
- `6`: output directory or atomic publication failure (destination file preserved unchanged).

What `code.zip` contains, against the submission table below:

| Required item | Where it is |
|---|---|
| Runnable solution | `code/main.py` (entry point) and `code/ghost_audit/` |
| Prompts | `prompts/` and `code/prompts/` (`visual_transcription.md`, `intent_classification.md`, `agent_system_prompt.md`). No model is called at run time (`code/evaluation/usage_report.md`), so no prompt is sent anywhere: these are the instruction documents for the deterministic components. They cover how the 11 PNGs were transcribed during development (facts bound to SHA-256 in `code/ghost_audit/visual.py`), the intent rules in `intent.py` with verbatim participant examples, and the controller protocol and tool contracts in `agent.py`. `code/tests/test_config_prompts.py` checks the tables in these documents against the code and data: the tool table, the intent examples and the image inventory. The surrounding prose is reviewed, not tested. |
| Configuration | `config/audit_config.json` and `code/config/audit_config.json` (effective runtime defaults), loaded by `code/main.py` (`--config`) and `Config` in `code/ghost_audit/audit.py`; individual CLI flags override JSON defaults. Investigation tool contracts via `--list-tools`. |
| README | this file |
| `evaluation/` reports | `evaluation/evaluation_report.md` and `evaluation/usage_report.md`, plus byte-identical copies in `code/evaluation/` |
| Tests | `code/tests/` and `code/evaluation/test_*.py` (`python3 -m unittest discover -s code`, 210 tests) |

Each request is investigated by a bounded, evidence-driven controller (`code/ghost_audit/agent.py`):

- **Explicit state and agenda.** An `Investigation` holds the phase, linked resources, resolved states, unresolved
  candidates and contradictions. Queued tool calls run in phase order (authorize → identify → discover →
  investigate → compile → validate), and a phase starts only when the previous phase's agenda is empty.
- **Routing from results.** Each tool's result decides what is queued next. A non-deletion request or an unknown
  identity ends the case. The catalog is queried only when a claim names the subject. A record that names another
  resource queues a reference check, followed to closure, never from workflow-inferred resources.
- **Tool contracts.** Every tool has a declared contract (`TOOLS`: phase, purpose, typed inputs, returns,
  preconditions; `--list-tools` prints them), enforced on every call.
- **Bounds.** Queries are de-duplicated and capped by `--max-queries`; budget exhaustion fails closed (INCOMPLETE,
  no steps).
- **Fail closed.** Before a row is emitted, `check_admissibility` rejects any step naming an unlinked resource or
  citing an id that is unknown or dated after `audit_at`. A failed tool, a broken contract or a rejected row sends
  the case to the reference pipeline. That output is re-validated, and if it also fails, the case fails closed.
  One failing case never aborts the run.
- **Trace.** Every query is logged with its reason, inputs, evidence references and result (`--trace`), together
  with the stop reason.

Competing requests are settled once in a shared preparation pass before any case is investigated. `--engine
pipeline` runs the single-pass reference implementation (identical output). No model is called: every intent
decision in the data comes from an explicit rule match, and the controller already runs every admissible query
within budget, so a model router could only skip queries or invent links (see the evaluation report, §16).

Pipeline (`code/ghost_audit/`), deterministic end to end:

1. **intent.py** decides whether `request_text` authorizes deletion. It covers six languages, and an explicit negation or narrower request (opt-out, access, correction) overrides.
2. **loaders.py** builds the identity index: current plus historical emails, hashes and phones from `user_audit_log.csv`. Matching is case-insensitive and exact; names never establish identity.
3. **evidence.py** parses every manifest file into typed observations. It reads SQL into in-memory SQLite, JSON bundles and sub-snapshots, eight log syntaxes joined by trace ID, handover text, and PDFs via `pdftotext`. Each observation keeps the file `captured_at`, the record or line time, the physical store and a locator.
4. **visual.py** holds a cached, SHA-256-bound transcription of the 11 PNG screenshots, with provenance.
5. **linking.py** attributes resources to the subject in three ways:
   - **direct:** an identifier token appears in the record or its trace;
   - **catalog:** a `LINK_RESOURCE_IDENTITY` claim that the evidence does not contradict;
   - **workflow:** identifier-less resources acted on by the deletion worker, assigned across all requests by constraint propagation; ambiguous ones are dropped.
6. **planner.py** derives the state at `audit_at`. It only uses evidence captured between `requested_at` and `audit_at`, takes the latest observation (a replica copy keeps the record present), and treats a snapshot as proving absence only for the store it enumerates. It then applies `deletion_rules.csv` by record category to build findings, remediation and verification.
7. **output.py** sorts steps by phase, then operation-table rank, then `system_id` and `resource_id`. Rows are in natural `case_id` order, as in `deletion_requests.csv`. It also runs contract self-checks.

Reports: [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md), [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

Start your AI coding tool from the **repository root** so it automatically reads [`AGENTS.md`](./AGENTS.md) and logs your session turns to `log.txt` in the root directory.

Build your solution in `code/main.py`, or use another language and document its setup and run command clearly in your README.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction row for every request in `dataset/deletion_requests.csv`
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file for submission must be the root-level `output.csv`.


## Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 518 requests |
| `sample_deletion_requests.csv` | 32 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, plain text, or SQL snapshot exports. SQL captures may contain a primary database, regional replicas, a reporting replica, or a failover replica so replication drift can be investigated directly. The `.sql` files are readable as text and executable with SQLite.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

---

## Output

Create `output.csv` with these columns in this exact order:

```text
case_id,deletion_status,steps_json
```

Produce exactly one row per request. `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation actions, and verification checks. Use an empty array (`[]`) for `NOT_APPLICABLE`. Evidence references may be a `file_id`, `activity_id`, or `audit_id`. Determine the resource state at `audit_at` from the available evidence; reported workflow statuses and operation results are claims. Use `dataset/sample_output.csv` as the format reference.

The final file must contain 518 data rows sorted by `case_id` (plus the header row). The grader parses `steps_json` as JSON, so object-key order and whitespace do not matter; array order does. Order findings, remediation, and verification in that phase order. Within a phase, follow the operation order in the problem statement, then sort by `system_id` and `resource_id`.

---

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
code/evaluation/evaluation_report.md
code/evaluation/usage_report.md
```

- `evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases created by you, failures found, and improvements made.
- `usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

**Important:** Place `evaluation_report.md` and `usage_report.md` inside `code/evaluation/` (matching `code/evaluation/main.py` in this starter layout), write your complete evaluation metrics, custom evaluation test cases, and token/cost breakdowns directly inside those Markdown files, and keep prompts in source code or Markdown files. Automated code evaluation ignores raw data files (`.csv`, `.tsv`, `.json`, `.jsonl`, `.sql`, `.log`), so content stored only in those formats will not be visible to the code judge or AI interviewer.

---

## Chat Transcript

This repository includes [`AGENTS.md`](./AGENTS.md), which instructs compatible AI coding tools to append conversation summaries to `log.txt` in the repository root:

| Platform | Path |
| --- | --- |
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

- Always launch your AI coding agent from the repository root (or explicitly ask it to read `./AGENTS.md` first) so every turn is logged to `log.txt` with the required `User Prompt`, `Agent Response Summary`, `Actions`, and `Context` sections.
- Upload `log.txt` directly as your `chat_transcript` submission on HackerRank.
- Do not include API keys, tokens, credentials, or secrets in prompts, code, configuration, or `log.txt`.

---

## Packaging `code.zip` and Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, and `evaluation/` reports |
| `output.csv` | One answer row for each of the 518 requests in `dataset/deletion_requests.csv` |
| `chat_transcript` | The root `log.txt` file generated by your coding agent |
| AI interview | Live explanation of your architecture, approach, and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `README.md` inside `code.zip`.

This submission's `code.zip` is built with `python3 code/package.py --out code.zip`. The packager uses an allowlist,
places the reports at both `evaluation/` (as `problem_statement.md` requires) and `code/evaluation/`, and verifies
exclusions, report copies and secrets. The starter commands below bundle only `README.md` and `code/`, so they omit the
top-level `evaluation/` copies.

To package `code.zip` from the repository root without bundling the dataset (starter commands):

**macOS / Linux:**

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

**Windows (PowerShell):**

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

When packaging `code.zip`:

- Keep your evaluation reports in `code/evaluation/` so they are bundled automatically with `code` (or add `evaluation` to the archive command above if you placed them in a top-level `evaluation/` directory).
- Do **not** include `dataset/`, virtual environments (`.venv`, `venv`, `node_modules`), or build caches (`__pycache__`). On Windows, keep `.venv` at the repository root (outside `code/`) and remove any `code/__pycache__` folders before running `Compress-Archive`.
- Do **not** include nested archives (`.zip`, `.tar`, `.gz`, `.tgz`, `.7z`, `.rar`) inside `code.zip` — submissions containing nested archive files will be rejected.

Before submitting, confirm that:

- `output.csv` has the exact header `case_id,deletion_status,steps_json` and contains all 518 data rows sorted by `case_id`;
- every `steps_json` value is a valid JSON array with consecutive step numbers starting at `1`;
- findings cite valid evidence IDs, and `COMPLETE` verification steps cite evidence proving the current state;
- proposed remediation and future verification steps use an empty `evidence_id` (`""`);
- `code.zip` includes your runnable code, `README.md`, `code/evaluation/evaluation_report.md`, and `code/evaluation/usage_report.md`;
- `log.txt` follows the `AGENTS.md` turn format and is uploaded to the `chat_transcript` slot; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.

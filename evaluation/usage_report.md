# Usage Report — final full-dataset run

Run that produced `output.csv` (SHA-256 `d6795660b65f65090a2f06d928303e91d80b8603974f5208ee00bc1bfc781e4e`):

```bash
python3 code/main.py
```

| Item | Value |
|---|---|
| Model providers used at run time | none |
| Model names used at run time | none |
| Model / API calls | **0** |
| Input tokens | **0** |
| Output tokens | **0** |
| Total tokens | **0** |
| Average tokens per request (518 requests) | **0** |
| Estimated total cost | **$0.00** |
| Estimated cost per request | **$0.00** |
| Per-model totals | not applicable (no model used) |
| Wall-clock runtime | 1.74 s (mean of 6 runs with `/usr/bin/time`, sprint 13, default `--engine agent`) |
| Peak memory | 48.9 MB RSS |
| External tools | `pdftotext` (poppler) for the 4 PDFs |

## How these numbers were established

- The investigation controller (`code/ghost_audit/agent.py`) runs deterministic tools only. Its
  per-case queries (at most 14, mean 9.5 on the 518 requests) are local evidence and policy lookups,
  not model calls. Why no model is used: evaluation report §16.2.
- The pipeline imports no model SDK and opens no network connections. The only subprocess it starts is
  `pdftotext`. No API key environment variables are read or required.
- Each of the 518 cases is decided by deterministic code (`code/ghost_audit/`). Running it twice gives
  byte-identical output; `code/tests/test_pipeline.py::test_deterministic_repeat` checks this.

## Model-derived input that was *not* produced by this run

- **What it is:** the 11 PNG screenshots were transcribed into structured facts once during development
  (2026-10-24), by the coding assistant (Claude Code, model `claude-opus-5-5`) viewing each image.
- **Where it lives:** the facts are cached in `code/ghost_audit/visual.py`, keyed by each image's
  SHA-256. If an image's bytes change, its facts are ignored and the file is reported as uncovered.
- **Usage not counted:** the tokens for that interactive transcription were part of the development
  session. This pipeline did not meter them, so they are **not** included above. The session itself is
  recorded in `log.txt` and the agent trace.
- **Why not an API call:** no OCR engine was available, and no model API is configured for the
  pipeline. Re-running extraction with a metered API would require adding a provider and reporting its
  real usage here.

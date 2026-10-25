# Investigation controller protocol

**No model receives this file.** The full-dataset run makes zero model calls
(`code/evaluation/usage_report.md`). The investigation controller in `code/ghost_audit/agent.py`
is deterministic code. This document is a specification reconstructed from that code after it was
written. It is not a verbatim historical prompt: no prompt was ever used for the controller. The file keeps the name `agent_system_prompt.md` because that is the path the
packager and earlier reports use. `code/tests/test_config_prompts.py` fails if the tool table below
differs from the registry the controller enforces.

## What the controller does

For one request, the controller keeps an investigation state and an agenda of typed tool calls.
What a tool returns decides what is queued next. The policy kernel (`planner.py`, `policy.py`)
stays authoritative. The controller chooses which admissible queries to run and when to stop. It
never changes what a rule means, and it can only emit steps that the kernel compiled from linked
resources and admissible evidence.

A phase starts only when the agenda of the previous phase is empty:

1. `AUTHORIZE`: decide whether `request_text` authorizes deletion. If it does not, stop with
   `NOT_APPLICABLE` and `[]`.
2. `IDENTIFY`: map the request email, current or historical, to one `user_id` and all of its
   identifiers. An unresolved identity stops the case.
3. `DISCOVER`: find records that carry the subject's exact identifiers in files captured inside the
   request window. A catalog claim is queried only when one names the subject. References carried
   by content-linked records are followed to closure. They are never followed from
   workflow-inferred resources.
4. `INVESTIGATE`: take the identifier-less resources that the deletion workflow acted on, settled
   once across all open requests (`Auditor.prepare`). Then resolve the state at `audit_at` of every
   linked resource.
5. `COMPILE`: apply `deletion_rules.csv` to the resolved states.
6. `VALIDATE`: run the output-contract self-check and the admissibility check. Every step must name
   a linked resource. Every citation must be a known id, available at `audit_at`, that
   substantiates the asserted state.

## Tool contracts (enforced on every call)

`python3 code/main.py --list-tools` prints the same contracts as JSON, with inputs and return
shapes. A call whose precondition does not hold is refused before it runs.

| Tool | Phase | Precondition id |
|---|---|---|
| `classify_intent` | AUTHORIZE | none |
| `resolve_identity` | IDENTIFY | `AUTHORIZATION_REQUIRED` |
| `search_direct_evidence` | DISCOVER | `SUBJECT_RESOLVED` |
| `query_catalog` | DISCOVER | `CATALOG_CLAIMS_NAMING_SUBJECT` |
| `follow_reference` | DISCOVER | `CONTENT_LINKED_SOURCE` |
| `query_workflow_context` | INVESTIGATE | `WORKFLOW_CONTEXT_PREPARED` |
| `resolve_resource_state` | INVESTIGATE | `LINKED_RESOURCE_REQUIRED` |
| `compile_obligations` | COMPILE | `ALL_LINKED_STATES_RESOLVED` |
| `validate_contract` | VALIDATE | `COMPILED_OUTPUT_REQUIRED` |
| `check_admissibility` | VALIDATE | `COMPILED_OUTPUT_REQUIRED` |

## Stop conditions (recorded in each case trace)

- The request does not authorize deletion: `NOT_APPLICABLE`, `[]`.
- The identity is unresolved: `INCOMPLETE`, `[]`.
- The agenda is empty: the compiled row is emitted. Any workflow candidates still unresolved are
  listed in the trace.
- The query budget (`max_queries`, default 256) is exhausted: fail closed with `INCOMPLETE`, `[]`.
  The run then exits with status 5 and does not publish `output.csv` unless
  `--allow-incomplete-output` is passed.
- A tool fails, a tool contract is violated, or the compiled row fails validation: fall back to the
  reference pipeline (`Auditor.audit`) and re-validate. If that also fails, fail closed.
- The state of one resource cannot be evaluated: the reference pipeline is tried first. If it
  succeeds, its re-validated answer is used and the run can still exit 0. If it also fails, the
  fault is scoped to that resource. The case never becomes `COMPLETE` while a state is unresolved,
  and the run exits with status 5.

## Rules the controller applies

- Workflow statuses, activity results and system messages are claims. State comes from
  observations in evidence files captured at or before `audit_at`.
- With the default configuration, evidence files captured after `audit_at` are never cited and never
  used to link or decide a case. (`post_cutoff_retention` is an experiment flag and is off.) Workflow
  activity is read only in a window from 2 h before to 2.5 h after `reported_at`. For every
  participant request that window ends before `audit_at`, but the code does not enforce this
  separately.
- Names alone never link a record to a subject. Lookalike identifiers never link. Only exact
  tokens do.
- An identifier-less resource is assigned to a request in two cases:
  - a single open request remains after constraint propagation. A request may hold at most one
    resource per category for profile categories, for resources of unknown category, and for
    resources known only from workflow activity; or
  - the resource is observed in evidence, and one request's deletion-worker activity on its directly
    linked resources is closer to it than every other request's by at least 5 minutes.

  Resources known only from workflow activity are never assigned by proximity. Anything else stays
  unassigned and is listed in the trace.

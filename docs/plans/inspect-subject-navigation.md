# Inspect Subject Navigation

## Status

Proposed for human approval. Implementation starts only after approval.

## Goal

Make `too AGENT inspect` the stable shell entry point for read-only execution
diagnostics. An operator can list records, use shell history to replace the
last subject with a printed Pointer, navigate to a related record collection,
and optionally project a diagnostic representation from the final subject.

```text
too AGENT inspect SUBJECT... [PROJECTOR] [--human | --json]
```

`runs` and `steps` are subjects, not projectors. A projector is an optional
terminal operation applied only after the complete subject chain resolves.

## Success Criteria

- `inspect threads` and `inspect runs` list records from the selected agent's
  execution store.
- `inspect THREAD_POINTER runs` lists Runs owned by that Thread.
- `inspect RUN_POINTER steps` lists Steps owned by that Run.
- Existing Thread, Control, Run, Step, and field Pointer inspection remains
  compatible.
- `inspect MODEL_STEP_POINTER model-call` displays the complete normalized
  `ModelCall` captured for that Step.
- Exact static names take precedence over generic Pointer parsing.
- One typed transition registry drives dispatch legality, allowed-value errors,
  help, shell completion, and transition tests.
- Every form is local, offline, read-only, and supports human and canonical
  JSON output.

## Current Behavior

The CLI currently exposes:

```sh
too AGENT threads
too AGENT runs [--thread THREAD]
too AGENT inspect POINTER [--human | --json]
```

`threads` and `runs` render bounded human tables. `inspect` resolves one
durable Pointer identifying a Thread, Control, Run, Step, or record field:

```text
term_ab12
term_ab12@0
run_ab12
run_ab12@0
run_ab12.0.2
run_ab12.0/output
```

`RunStore` already persists content-addressed facts for a normalized model
call and can rebuild the `ModelCall` owned by a model Step. Provider-native
requests and responses are not persisted.

An earlier unmerged design used `model_call@STEP` and included prospective
call preparation and provider request behavior. This definition replaces that
selector and scope.

## Vocabulary And Grammar

- **Agent** is the required outer CLI target whose execution store is read.
- A **subject** is a selected collection, record, or field.
- A **subject chain** is evaluated from left to right.
- A **collection name** is a fixed subject name such as `threads`, `runs`, or
  `steps`.
- A **Pointer** is the existing durable record or field address.
- A **projector** is an optional final name that converts the resolved subject
  into a diagnostic representation.

The initial grammar is closed:

```text
INSPECT_QUERY  := SUBJECT_CHAIN [ PROJECTOR ]
SUBJECT_CHAIN := ROOT_SUBJECT [ RELATION ]

ROOT_SUBJECT  := "threads" | "runs" | POINTER
RELATION      := "runs" | "steps"
PROJECTOR     := "model-call"
```

The valid forms are:

```text
threads
runs
THREAD_POINTER runs
RUN_POINTER steps
POINTER
MODEL_STEP_POINTER model-call
```

For example:

```sh
too eve inspect runs
too eve inspect term_ab12 runs
too eve inspect run_ab12 steps
too eve inspect run_ab12.0.2 model-call
```

The first implementation does not append an item ID to a collection, map a
projector over a collection, or flatten a relation after a collection result.
Individual records remain directly addressable by global Pointer.

## Static Dispatch And Typed Transitions

Parsing is deterministic and never tries handlers until one succeeds. It uses
this precedence:

1. recognize a supported projector only as the final token after a subject;
2. match exact static subject names valid for the current subject kind;
3. parse `run_` Run, Control, Step, and field Pointers;
4. parse canonical Thread Pointers;
5. otherwise report an invalid subject token.

The generated Thread prefixes are currently `term_`, `script_`, and `web_`,
but dispatch must not treat that list as the complete Thread grammar. The
Pointer contract permits any canonical execution ID outside the reserved
`run_` namespace.

Exact `threads`, `runs`, and `steps` tokens are reserved by the inspect
grammar. They never fall through to a same-named Thread lookup. Generated
Thread IDs do not collide with them. `model_call@STEP`, `modelcall`, and
`model_call` are not compatibility forms for `model-call`.

Subject chains use this typed transition table:

| Current subject | Static child | Result subject |
| --- | --- | --- |
| Agent | `threads` | Thread records |
| Agent | `runs` | Run records |
| Thread record | `runs` | Run records |
| Run record | `steps` | Step records |

A whole-record Pointer determines the record subject kind. A Pointer with an
RFC 6901 field suffix resolves to a terminal field subject. Control and Step
records, fields, and collection results have no child subjects in this
feature.

Therefore `term_ab12 runs` and `run_ab12 steps` are valid, while these are not:

```text
term_ab12 steps
run_ab12 runs
run_ab12/output steps
term_ab12 runs steps
```

One closed registry owns the table. Dispatch resolves the head and then looks
up each exact child token for the current subject kind. The same registry
provides completion candidates, help, allowed-value errors, and parameterized
transition tests. The command must not duplicate these relationships in
target-specific conditionals.

## Collection Semantics And Presentation

Collection subjects expose durable records, not aggregate documents:

- `threads` keeps the current ordering and newest-50 bound.
- `runs` keeps the current ordering, visibility, and newest-50 bound.
- `THREAD runs` selects root and child Runs whose durable `thread` equals the
  selected Thread ID. It uses the same visibility and newest-50 bound as
  `runs`; it does not splice a fork source Thread's history prefix.
- `RUN steps` selects every ordinarily visible Step owned by the Run,
  including nested StepPaths, in numeric StepPath order.

An empty collection succeeds with headings and no rows. A missing scoped
Thread or Run fails before its relation is evaluated. Pagination, `--all`, and
new collection filters are outside this feature; the top-level list commands
retain their existing filters.

Human output reuses the compact list vocabulary of existing list commands.
Every row begins with an unmodified Pointer that can be copied into another
`inspect` invocation. Step rows use the complete durable StepPath.

JSON output is a bare array of canonical record objects. Each object matches
the JSON returned by inspecting that whole record Pointer. It adds no inspect
envelope, summary, or nested synthetic record.

Human remains the default; `--human` and `--json` are mutually exclusive.
Existing `inspect POINTER` presentation does not change.

## `model-call` Projector

The initial projector registry has one entry:

| Final subject | Projector | Result |
| --- | --- | --- |
| model Step record | `model-call` | normalized `ModelCall` |

The projector requires one whole Step record. It is invalid after a field,
collection, Run, Thread, or Control. After the subject-kind check it verifies
that the Step kind is `model`; other Step kinds report
`step is not a model call: STEP_POINTER`.

The projector rebuilds the persisted normalized call through execution-owned
history/store behavior. `STEP_POINTER/given/call` remains distinct: that field
shows compact persisted references, while the projector shows their complete
logical value.

Human output shows the complete structured call, including instructions,
messages, tool definitions, and continuation data. `--json` emits the
canonical normalized `ModelCall` without an inspect envelope.

Projection is historical and read-only. It does not prepare a prospective
agic call, select a model, generate a provider request, send traffic, create
records, or modify accounting.

These future projectors require separate definitions and are not accepted now:

```text
model-result    normalized logical result
model-request   actual provider-native request
model-response  actual provider-native response
```

Actual provider requests and responses require explicit capture, redaction,
persistence, size, streaming, and compatibility decisions. Reconstructing
current adapter behavior must not be presented as historical transport truth.

## Scope And Compatibility

This feature includes execution collection subjects, existing Pointer
subjects, typed subject dispatch, the historical `model-call` projector,
human and JSON presentation, help, completion, tests, and directly affected
CLI documentation.

It excludes:

- `models`, model selection, and all future projectors;
- catalogs, providers, adapters, toolsets, tools, sandboxes, and other future
  developer-facing collections;
- caps, jobs, tasks, chores, psyches, skills, services, prompts, and other
  user-facing resources;
- prospective preparation, provider request projection, and send mode;
- mutation, wildcards, collection flattening, or projector mapping;
- database migrations, execution-event changes, HTTP API changes, and live
  provider tests.

Developer-facing collections may later join the transition registry.
User-facing resources stay in their product commands instead of turning
`inspect` into a general resource browser.

Compatibility requirements are:

- preserve `inspect POINTER`, Pointer grammar, `--human`, `--json`, required
  agent-prefix routing, offline access, and exit behavior;
- preserve the top-level `threads` and `runs` commands and their filters;
- document `inspect threads` and `inspect runs` as the preferred developer
  diagnostic workflow;
- intentionally reserve exact bare `threads`, `runs`, and `steps` instead of
  treating them as generic Thread IDs;
- do not accept the unmerged `model_call@...` syntax as an alias.

## Errors

Syntax is validated before store lookup. Store lookup precedes relation and
projector semantics. Tests must distinguish:

| Condition | Error category |
| --- | --- |
| malformed token or Pointer | invalid syntax |
| valid absent Pointer owner | missing record |
| static child invalid for current subject | invalid transition with allowed names |
| projector unsupported for final subject | invalid projector |
| `model-call` on a non-model Step | wrong semantic Step kind |

Exact wording may retain existing Click prefixes. No handler may fall through
to a different interpretation after one category is selected.

## Design Touchpoints

- `src/toolang/cli/toolang/commands/thread.py`: retain control commands and
  delegate inspection or keep only its thin entry point.
- `src/toolang/cli/toolang/commands/inspect.py`: own query parsing, typed
  subjects, transition and projector registries, dispatch, and presentation.
- `src/toolang/execution/history.py`: expose read-only scoped Step listing and
  historical model-call reconstruction.
- `src/toolang/execution/store.py`: reuse current record selection, Run/Step
  listing, and `rebuild_model_call()`; do not change the schema.
- `src/toolang/cli/toolang/main.py` and `routing.py`: preserve required
  agent-prefix routing while accepting the subject chain.
- `tests/unit/cli/`, `tests/integration/cli/test_local_core_commands.py`, and
  `tests/integration/execution/test_run_history_scenarios.py`: cover parsing,
  transitions, presentation, and history reads.
- `docs/api.md`: document the canonical workflow.

Moving existing inspect-only helpers from `thread.py` into `inspect.py` is
allowed within implementation scope if their behavior remains compatible.

## Acceptance Tests

1. **Static precedence:** exact root collections win over same-named Thread
   lookup; `run_` forms win over generic Thread syntax; arbitrary canonical
   non-`run_` Thread IDs remain supported.
2. **Transition matrix:** every allowed transition succeeds; every disallowed
   subject pair reports only the children permitted by the shared registry.
3. **Collections:** root collections preserve current bounds and ordering;
   `THREAD runs` uses direct ownership and ordinary visibility; `RUN steps`
   includes nested owned StepPaths in numeric order; empty scopes succeed and
   missing scope records fail.
4. **Reusable output:** every human Pointer selects the intended record in a
   follow-up invocation; JSON array items equal individual Pointer JSON.
5. **Compatibility:** the existing Pointer inspection suite and top-level list
   command filters continue to pass except for the documented reserved words.
6. **Model call:** human and JSON output reconstruct a complete persisted call;
   `given/call` still shows references; invalid subject and non-model Step cases
   fail; projection creates no record, provider call, or accounting.
7. **Shared registry:** completion, help, allowed-value errors, dispatch, and
   transition tests consume the same transition declarations.
8. **Default verification:** Ruff check and format, ty, and the default pytest
   suite pass.

## Risks

- Static names shadow theoretically valid custom Thread IDs. This is
  intentional but must be called out in implementation and release notes.
- Earlier `model_call@...` work contains prospective and provider-request scope;
  implementation must start from this definition and current `main` behavior.
- Fork-aware history and direct Run ownership are different queries;
  `THREAD runs` deliberately uses direct ownership.
- The transition registry will drift if any handler, help, completion, or test
  bypasses it.

## Open Questions

None.

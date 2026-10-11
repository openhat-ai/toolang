# Run Settings and Overrides

Status: Implemented from the definition approved on 2026-08-31. The current
input grammar is owned by [Chat Commands and Run Directives](chat-prefix-rules.md)
and [Input Syntax](../input-syntax.md).

## Goal and Scope

Give Chat one UI-independent session setting and each run an input-local
override. Preserve the execution request, policy precedence, queue snapshots,
and local/remote parity. Command parsing and materialization must agree on the
lifetime of each value.

## Values and Ownership

| Value | Meaning | Owner |
| --- | --- | --- |
| `ChatCommand` | One standalone slash or colon command | Chat input and command dispatch |
| `SessionSetting` | Concrete defaults for subsequent runs in a Chat session | Chat session |
| `RunOverride` | Sparse settings and root-runnable selection for one submission | Shared submission parser |
| `CallInput[str]` | Primary-input and named-argument sources | Call-input grammar |
| `RunRequest` | Materialized request accepted for execution | Execution protocol |

`RunOverride` describes the parsed result, not another command category.
A colon command updates the session atomically. Slash commands inspect or control
Chat. Slash directives supply run overrides or a root runnable call; dollar and
at directives describe Content. The complete grammar and signature requirements
are defined once in [Input Syntax](../input-syntax.md).

## Session and Run Semantics

- The surface baseline is captured at Chat creation. Session changes replace
  the immutable `SessionSetting` value without changing that baseline.
- Run materialization combines the surface, session, and submission override
  into one self-contained `RunRequest`. Queued requests retain that snapshot.
- Model bodies share the parser used by `:model` and `/model`. Parameter-only
  updates retain model identity and unspecified parameters. Selecting an
  identity clears unspecified explicit parameters before applying assignments.
- `:model default` selects the effective default inside the session's allowed
  model collection. `/model default` restores the surface model for one run;
  `/model unset` removes that run's inherited binding. Reasoning effort and
  budget lower to the existing typed model parameters and are validated against
  the effective model before acceptance.
- `:runnable`, `:agic`, and `:flow` select session defaults. Only the generic
  `:runnable default` resets to the surface default. Root runnable calls select
  the final public State binding for one run; a runnable named `default` remains
  an ordinary target.
- Session allow updates replace the authored fields. Run allow values add a
  ceiling intersected with the session ceiling. Repeated query values merge in
  authored order; incompatible sentinel combinations reject.
- Limit updates replace the authored fields. Duplicate fields and invalid
  values reject before any setting changes.
- `:workdir` queries or changes the session workdir. `/workdir` applies only to
  its run, including when the supplied path matches the session or when the
  session changes before a queued run starts.
- Parsing, reference resolution, available metadata validation, and session
  replacement are atomic. A failure leaves the current session unchanged.
  Core capability validation remains authoritative at run acceptance.

An editor completion edits a draft only. It neither submits a command nor
changes settings. Missing required command bodies show focused usage; `:workdir`
without a body queries the session.

## Materialization

| Override field | Effective run value |
| --- | --- |
| Absent | Inherit the session value |
| Model parameters only | Retain identity and update authored parameters |
| Explicit model identity | Select it, clear unspecified parameters, apply assignments |
| Model `default` | Captured surface model |
| Model `unset` | No inherited model binding |
| Runnable reference | Exact resolved public target |
| Allow | Additional ceiling intersected with the session ceiling |
| Limit | Replace the authored session limit field for this run |
| Workdir | Resolve a run-local workdir from the submitted path |

Only materialized model/runnable requests, input, policy, and workdir values
enter `RunRequest`. Parser state, completion objects, and mutable UI state do
not enter the execution protocol. Persistence, retry/rerun, collection queries,
model catalogs, and adapters retain their existing contracts.

## Implementation Touchpoints

- `execution/types.py`: `SessionSetting` and `RunOverride`.
- `execution/input.py`: setting bodies, run directives, and override merging.
- `execution/policy.py`: session updates and run-setting materialization.
- `execution/calls.py`: runtime reference and input resolution.
- `cli/toolang/commands/chat/input.py`: normalized Chat submission classification.
- `cli/toolang/commands/chat/commands.py`: standalone commands and feedback.
- `cli/toolang/commands/chat/policy.py`: session reconciliation and request creation.

## Acceptance

1. Commands and run submissions follow the current prefix-plan matrix.
2. Model identity and parameter updates preserve their distinct boundaries.
3. Session ceilings may change allowed collections; run ceilings cannot broaden
   them. Limits reject duplicate or invalid fields atomically.
4. Failed setting updates leave the session unchanged; queued requests remain
   unchanged after later session updates.
5. Local, remote, and line Chat produce equivalent requests from the same
   surface, session, and submitted input.
6. Explicit workdir overrides never write back into the active session.
7. The default offline suite passes with existing protocol and storage behavior.

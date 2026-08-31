# Define Agic Messages and Runtime Context

## Status

Proposed for review. This plan defines behavior only; it does not approve
implementation.

## Goal

Make model-call messages deterministic across root, child, nested, and parallel
runs. Separate Thread context, Run ancestry, and one agic run's model messages
without adding a `turn` concept.

## Success Criteria

- Every agic and flow run can read one immutable root snapshot of `far` and
  `near` plus its branch-local `line`.
- Root, child, and parallel behavior does not depend on execution timing.
- Agic messages follow the recalled + rendered + appended formula, while flow
  runs own no model messages.
- Root auto recall is `far, near`; child auto recall is empty; explicit recall
  is deterministic.
- ModelCall snapshots keep instructions, messages, and other normalized fields
  separate through the adapter boundary.
- Unreferenced runnable input and internal child messages never enter model or
  Thread context implicitly.
- The behavior is covered by offline language, execution, store, and adapter
  tests without introducing a `turn` abstraction.

## Decisions

### Vocabulary

Use these terms and types:

```text
far thread context  far: Part[]
near thread context near: Message[]
line context        line: Part[]
```

Use `Thread`, `Run`, and `ModelCall` for existing types. In prose, use thread,
run, root run, child run, agic run, flow run, model call, tool call, and run
call. Do not introduce Turn, AgicExecution, or RunCall types. A run call remains
the existing model ToolCall classified as a runnable request.

### Thread Context

Thread context is one public conversation split at a compaction boundary:

```text
thread context = far + near
```

- `far` represents the compacted prefix.
- `near` contains the uncompacted suffix with native message roles.
- The values have no overlap or gap.
- Compaction may move a prefix of `near` into a replacement `far` between root
  runs, but never mutates an active snapshot.

The runtime captures `far` and `near` once when it accepts a root run. Every
descendant inherits that immutable snapshot instead of querying the Thread
again. Parallel branches therefore cannot observe sibling completion order.

Only public root-run interaction contributes to a future Thread snapshot:
accepted user input, applied steers, and the accepted root output under existing
Thread visibility rules. Child-run messages, intermediate model output, tool
protocol, flow locals, and repair messages remain internal.

The compaction algorithm, threshold, and model are a separate feature. This
plan defines the resulting `far`/`near` contract and its consumption.

### Line Context

`line` describes the ordered Run path from root through the current Run,
inclusive. Each entry contains the resolved runnable identity and its resolved
primary and named input. It excludes outputs, model/tool protocol, siblings,
and descendants.

A child Run extends its parent's line. Parallel children share a prefix and
then diverge. A same-Run handoff does not extend line; a run call that creates a
child Run does.

The runtime renders line as `Part[]`, preserving input Parts and representing
other typed values as canonical JSON text. It uses plain labels and Part
boundaries, not XML.

### Runtime Locals And Runnable Input

Every agic and flow run receives read-only `far`, `near`, and `line` locals.
These names are reserved and cannot be runnable parameters or flow bindings.

Templates reference them directly:

```too
user:
  Run line context:
  {{line}}
```

Within a message block:

- `far` and `line` splice their Parts under the block's role;
- `near` renders as canonical JSON, so its roles remain data; and
- only recall can insert `near` as native, role-preserving messages.

Primary `_` and named parameters remain runnable input. An agic message or flow
statement uses input only by referencing it. Unreferenced input is discarded;
the runtime no longer appends primary input to a fallback user message.

This gives every runnable the same inputs and context values. The declaration
decides which values matter.

### Recall

Recall is agic-only and has this closed grammar:

```too
recall = auto
recall = none
recall = far
recall = near
recall = far, near
```

Omission means `auto`. Its behavior is fixed:

```text
root agic run  auto => far, near
child agic run auto => none
flow run            => no recall; use locals explicitly
```

An explicit source selection applies to any agic run. `none` disables insertion
without hiding the locals. `line` is never a recall source. The owning Run
determines root or child status, so a same-Run handoff retains that status.

Recall always uses this order:

```text
recalled messages = far user message + near messages
```

If `far` is nonempty, runtime creates one synthetic message:

```text
role: user
content:
  Far thread context:
  <far Parts>
```

An empty `far` creates no message. `near` preserves its messages, roles, and
order. Runtime recall adds no XML tags.

Only `far, near` is accepted as the combined spelling. Duplicates, `near, far`,
and combinations with `auto` or `none` are invalid. The legacy `default`,
`history`, and `memory` values have no aliases. `memory` is reserved for a
future independent capability.

### Agic Messages

One agic run owns one mutable message list:

```text
agic messages =
    recalled messages
  + rendered messages
  + appended messages
```

These are logical segments, not record types.

- **Recalled messages** are built once from the root snapshot and recall
  directive. They do not refresh between model calls.
- **Rendered messages** are authored user/assistant blocks after Content,
  prompt, local, and selected `context` rendering. They never receive
  unreferenced runnable input.
- **Appended messages** are model output, tool and run-call results, steers, and
  runtime retry/repair messages added in observed order.

A child agic run starts new agic messages and never inherits its parent's list.
A flow run has no agic messages.

Before each model call:

```text
ModelCall.messages = snapshot(agic messages)
```

The first call normally has an empty appended segment. Later calls retain the
same recalled/rendered prefix and see the growing appended suffix.

### ModelCall And Adapters

`instructions` are not agic messages. The normalized call remains:

```text
ModelCall
  instructions
  messages
  tools
  output_schema
  continuation
```

The adapter consumes these independent fields and constructs its
provider-specific request. An adapter may encode instructions as a provider
system/developer field or message, but that does not mutate agic messages or
`ModelCall.messages`. Persistence and inspection retain the normalized
ModelCall, not the provider wire request.

### Roles And Delimiters

Runtime-created `far` uses a dedicated user message so recalled data does not
gain instruction authority. Explicit `line` or prefetched reference data should
normally use another dedicated user message immediately before the current
task:

```too
agic review(_: Text):
  user:
    Run line context:
    {{line}}

  user:
    {{_}}
```

Message boundaries and plain labels are the default delimiters. XML is optional
authoring syntax for a specific text prompt, not a runtime convention or a
security boundary. This also keeps multimodal `Part[]` intact.

### Future Memory Plugins

Memory retrieval is explicit and separate from Thread recall:

- a pre-model query returns `Part[]` or structured data into an ordinary local,
  which the agic normally renders in a dedicated user message before the task;
- a model-requested query stays in its assistant tool-call/tool-result sequence
  and is not duplicated as a user message; and
- memory results cannot inject native Message roles and do not propagate to a
  child except through ordinary explicit input.

Memory plugin API and retrieval behavior are outside this plan.

### Persistence And Reproduction

The root `far`/`near` snapshot must be durable or durably referenced. Retry,
rerun, descendants, and historical inspection reuse that snapshot even after
later Thread activity or compaction. Submitting a new root run captures a new
snapshot.

Line derives only from durable root-to-current Run controls. Historical
ModelCalls continue to retain exact content-addressed messages; they are never
rebuilt from current Thread or Run state. Descendants may reference one
root-owned context snapshot rather than duplicate its storage.

## Current Behavior To Replace

The implementation already separates `ModelCall.instructions` and messages,
maintains a mutable agic-local message list, and persists each exact ModelCall.
The implementation changes are:

- replace `default | history | memory` recall with
  `auto | none | far | near`;
- snapshot Thread context once per root tree instead of querying it for every
  agic preparation;
- project public root interaction instead of replaying internal Runs;
- add the complementary compacted `far` representation and the `line` local;
- make child auto recall empty; and
- remove implicit primary-input fallback.

## Scope And Touchpoints

Implementation includes language validation, runtime locals, context snapshot
and persistence, line construction, agic-message assembly, inspection, and
tests. It is expected to touch:

- `src/toolang/lang/validate.py`, `lower.py`, `input.py`, and language types;
- `src/toolang/execution/types.py`, `records.py`, and `store.py`;
- `src/toolang/execution/executor/executor.py`, `common.py`, and `prepare.py`;
- `src/toolang/execution/executor/runs/agic.py` and `steps/model.py`;
- adapter conformance tests if they conflate instructions and messages; and
- `docs/program.md`, `docs/input-syntax.md`, and focused tests.

Excluded are compaction strategy, memory plugins, provider wire formats, UI
presentation, and new Turn, AgicExecution, or RunCall types.

## Acceptance Tests

1. Validation accepts only the canonical recall forms and reserves `far`,
   `near`, and `line` from parameters and flow bindings.
2. Root `auto` recalls `far, near`; child `auto` recalls nothing; explicit
   child recall and `none` behave as defined.
3. Every descendant and parallel branch observes the root's immutable Thread
   snapshot, with no child or sibling message leakage.
4. Thread context contains public root interaction only and preserves the
   no-overlap/no-gap invariant across a simulated compaction boundary.
5. Line follows root-to-current Run order, includes only runnable identity and
   resolved input, extends only for child Runs, and diverges across branches.
6. Direct Part interpolation preserves Parts; direct `near` interpolation emits
   JSON under the authored role without role injection.
7. Unreferenced primary and named input does not enter rendered messages or
   affect flow evaluation.
8. Multi-call agics preserve one recalled/rendered prefix and append model,
   tool, steer, run-call, and repair messages in order.
9. Each persisted `ModelCall.messages` equals its agic-message snapshot while
   instructions and other normalized fields remain separate.
10. Retry, rerun, descendants, and historical inspection use the original
    durable snapshot after later Thread changes.
11. The complete default verification suite passes offline.

## Tradeoffs And Risks

- Root `auto` is the only root/child asymmetry. It avoids entry-point boilerplate
  but must be visible in effective ModelCall inspection.
- The new reserved locals, recall values, and explicit-input rule intentionally
  break sources that use those names or rely on legacy recall/fallback behavior.
- `far` trades exact detail for size; compaction quality belongs to its separate
  policy. `near` preserves exact Messages.
- A synthetic user message is lower authority than instructions but is not
  verbatim user input; its fixed label exposes its origin.
- JSON interpolation of `near` can be expensive. Authors needing native roles
  should use recall.
- Line may expose ancestor input, but only when an author explicitly uses it.
- Durable snapshots add storage; one root-owned, content-addressed snapshot
  avoids descendant duplication.

## Open Questions

None within this scope.

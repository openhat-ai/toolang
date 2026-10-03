# Flow exec and runtime exec naming

## Status, goal, and scope

Proposed definition for review. This PR changes documentation only. Implement
the grammar in a separate `openhat-ai/tree-sitter-toolang` PR; implement Toolang
after this definition is approved and the grammar dependency is available.

Use one explicit operation for replacing the runnable within an existing Run:
`run` calls a child and returns; `exec` replaces the current runnable and never
returns on success. Expose it as a Flow statement and as `_toolang/exec`.

Success means that a Flow can prepare a successor, check its published generation
through fresh named child Runs, and transfer from inside `repeat`. The old loops
end, the successor starts under the same Run identity, and progress clearly
separates the two runnable invocations with display numbering restarted at zero.

In scope: syntax and language consumers, native Flow handoff, runtime-tool
renaming, durable Step/control facts, repeat unwinding, and shared progress and
inspection presentation. Out of scope: root-Run spawning, new scheduling or
restart services, ordered State revisions, `me` publication receipts, hot reload
of an active runnable, broader cap authority, and a built-in evolution policy.

## Verified baseline

- The shared executor already dispatches agics and flows through a handoff loop.
  `_toolang/execute` replaces its calling Run; Flow is already a legal target.
- Native Flow statements have no handoff operation. Named child calls bind the
  latest published State; generated inline declarations retain their owning plan.
- `repeat` owns nested Steps within its Flow's Run. It creates no separate Run.
  Generic Flow Step error handling currently wraps a transfer exception as failure.
- Execute acceptance requires a running Tool Step, and its input provenance
  assumes a model ToolCall. Both restrictions must be generalized for authored exec.
- Physical Step indices continue across handoffs. Progress currently detects
  handoffs through the execute Tool Step and its committed result.

Source owners: [executor](../../src/toolang/execution/executor/executor.py),
[Flow Step boundary](../../src/toolang/execution/executor/common.py),
[store](../../src/toolang/execution/store.py), and
[progress projector](../../src/toolang/cli/common/execution_progress/projector.py).

## Language and call contract

```too
exec grow_v2
exec {{successor}}
exec {{next.runnable}}
```

- `exec` is a standalone statement in a Flow body or nested repeat body. It has
  no result binding, inline agic body, argument-list syntax, or modifiers.
  Reject both `let result = exec ...` and `let exec ...`. Later statements may
  remain authored but are unreachable after a successful handoff.
- A literal target uses the existing named-runnable token. A dynamic target is
  exactly one `{{path}}` reference: a local name, optionally followed by dotted
  field names. Use existing local/field selection semantics, including `_` as a
  root. No mixed text, prompt expansion, expressions, or string construction.
  Resolve once to nonempty Text, then parse as an existing runnable reference.
- Resolve the target and its signature against one captured latest published
  State at this statement's acceptance boundary. Bind that same snapshot; do
  not reread latest State during preparation or commit. Forward targets may be
  absent from the Flow's original State. Validate their existence at execution,
  not by requiring them in the accepted Flow's old program.
- Literal targets are explicitly authorized by authored code, like `run`.
  Dynamic targets must also match the Flow's explicit or inherited `handoffs`
  selection; `none` or user-requested-only authority cannot authorize autonomous
  dynamic dispatch. Preserve module visibility and generated-target restrictions.
- Bind primary input and declared arguments from current Flow locals using the
  existing named-`run` rules and target signature. Pass only selected inputs;
  do not copy arbitrary locals. Missing required input and incompatible values
  fail before committing any handoff. Preserve their typed provenance.

The lowered AST adds `ExecStmt` with no binding and a typed literal/reference
target. Lowering, formatting, AST serialization, source diagnostics, and static
validation own this syntax. Runtime evaluates lowered references and binds values
without reparsing source text. References are never Python or shell expressions.

## Shared handoff semantics

Both frontends use one preparation/commit mechanism:

1. Resolve and authorize the target, check its input, effective resources, and
   lineage, and prepare the replacement binding before committing anything.
2. Commit the handoff to the current Run, preserving its ID, parent, thread,
   captured Setup, resource ceiling, and root-tree time/token/cost accounting.
   Adopt the target's code, instruct, caps, and settings from the selected State.
3. Start the target from its entry. Discard the outgoing runnable's locals except
   explicitly bound successor input. Successful exec never returns to old code.

Keep current restrictions: a target in the current/ancestor runnable lineage is
rejected, including earlier handoff targets; the final result must satisfy the
Run's original entry output contract. Neither frontend expands cap authority.
Run-level retry continues to reject histories containing committed handoffs;
use the existing rerun behavior instead. Agic-local counters retain existing
reset semantics; this feature does not reset root accounting.

Before commit, rejection leaves the original binding intact. A runtime-tool
rejection returns its ordinary error to the calling model; an authored Flow
exec failure fails that Step and follows ordinary Flow failure propagation.
After commit, cancellation or delivery failure cannot roll back the handoff.

### Repeat and durable records

- Add an `exec` Step kind with `given = ExecStmt`, no output, and a successful
  `ExecStepNoted` containing the applied ControlRef and resolved runnable ref.
  It represents a control operation, not a fabricated child Run or ToolCall.
- Preserve the existing durable `execute` control kind and payload role. The
  public spelling change does not require renaming historical control records
  or ordinary internal execution functions. Permit a running native exec Step
  as its trigger alongside a running Tool Step, with matching Run ownership.
- Generalize handoff input preparation to accept evaluated values and their
  typed provenance. Model calls keep their existing raw-input references; Flow
  calls use selected local references. Do not fabricate model-input JSON for Flow.
- A successful native exec closes its Step and every still-open repeat ancestor
  in the same Run. Loops use `status = succeeded`, `termination = exec`, and
  `aborted_by` pointing to the handoff control. Retain the number of fully
  completed iterations; an interrupted current iteration is not completed.
- Commit the native exec control and these terminal Step facts atomically before
  dispatching the successor. Emit terminal notifications from inner to outer,
  then new target Steps. Transfer propagation must bypass generic failure
  wrapping and ordinary result binding; it must not emit duplicate StepEnd facts.
- Unwind only to the current Run boundary. A parent's `run` Step remains running
  until this Run's successor finishes. Child exec cannot exit a parent's repeat.
- Advance affected store/prepared-state schema versions under existing policy:
  incompatible execution stores are rejected without mutation, and stale prepared
  caches are rebuilt. Add no ad hoc migration or rewriting of historical calls.

## Runtime-tool rename

Expose `_toolang/exec`, encoded to model providers as `_toolang__exec`, with the
existing `{runnable, input}` arguments and `{controls: [...]}` success receipt.
It remains the only tool call allowed in that model response. Keep its captured
advertised-catalog binding behavior; the rename does not switch it to an unseen
newer target while a model response is being processed.

Remove `_toolang/execute` from callable definitions and dispatch; add no alias.
Update the narrow runtime protocol, authorization/error text, model instructions,
current docs/examples, adapters' expectations, and tests together. Do not blindly
rename generic `execute()` functions, ordinary prose, or historical plan text.
Presentation of persisted old tool names remains a read-only concern, not an
invocation compatibility alias or a history rewrite.

## Presentation

Use the shared Script/Chat progress projector and inspection views. An exec
boundary has the same indentation as its owning Run's entry boundary, outside
all exited repeat blocks. It opens no new Run and produces no intermediate Run
footer. Target Steps belong at the Run's top level, not beneath the old loop.

```text
Run R · flow grow                                      running
└─ Step 0 · repeat                                     handed off
   └─ exec grow_v2                                    succeeded

── exec → flow grow_v2 ────────────────────────────────────────
├─ Step 0 · run evaluate                               succeeded
└─ Step 1 · repeat                                     handed off
   └─ exec grow_v3                                    succeeded

── exec → flow grow_v3 ────────────────────────────────────────
└─ Step 0 · run improve                                running
```

This is a hierarchy illustration, not a requirement to add Run headers to
surfaces that currently omit them. Retain the existing divider style/width rules.
The first displayed target Step is zero, with subsequent local numbering within
that invocation. Physical StepRefs remain monotonic and unique across the Run;
never reset stored indices or change links/API pointers. Show the canonical
pointer in detail/navigation surfaces when display numbers would be ambiguous.
Derive boundaries from committed handoff facts, not model prose or tool intent.
Nested loops close as handed off, not failed, canceled, exhausted, or satisfied.
Empty successors still show a committed boundary; rejected exec shows none.
Costs and elapsed time accumulate across the same Run, with no double counting.

## Separate tree-sitter PR

Implement grammar changes only in
[`openhat-ai/tree-sitter-toolang`](https://github.com/openhat-ai/tree-sitter-toolang):

- Add `exec_statement` with a `target` field: existing `runnable` for literals,
  or `exec_target_reference` for the single placeholder form. The reference has
  a `path` field containing the existing local/field selection path vocabulary.
  Whitespace inside braces is allowed; a target never crosses a physical line.
- Add `flow_exec_keyword` and scanner keyword recognition at Flow statement and
  implicit-prose boundaries. Keep explicit text containing `exec` literal and
  identifier prefixes such as `executor` unchanged. Ensure exec cannot enter
  `let` through a generic value-operation production.
- Update `grammar.js`, `src/scanner.c`, `GRAMMAR.md`, applicable queries, corpus
  cases, fixture/binding tests, and generated `src/grammar.json`,
  `src/node-types.json`, and `src/parser.c`; regenerate rather than hand-edit.
- Cover literal/dotted targets, comments/docs, nested repeat and dedent ownership,
  LF/CRLF/EOF, and malformed/missing targets, inline bodies, bindings, trailing
  tokens, and multiline or mixed-text placeholders. Invalid structural exec must
  not recover into implicit prose or consume a later sibling.
- No package version bump or release in the grammar implementation PR. Publish
  through the existing separate release workflow. Toolang then updates its grammar
  dependency/lock and consumers together; no grammar fork or source-text fallback.

## Implementation touchpoints and acceptance

| Concern | Likely files |
| --- | --- |
| Syntax, AST, formatting, validation | `src/toolang/lang/{ast,lower,format,validate,flow_validation,contracts,cst}.py`; language tests; `pyproject.toml`, `uv.lock` |
| Handoff and repeat lifecycle | `src/toolang/execution/executor/{executor,common,tool_runtime}.py`, `stmts/`, `steps/loop.py`, `runs/flow.py` |
| Records and tool protocol | `src/toolang/execution/{types,records,events,schemas,store}.py`, `src/toolang/base/protocols/tool.py`, `src/toolang/execution/tools/_toolang.py` |
| Presentation and documentation | `src/toolang/cli/common/execution_progress/`, `src/toolang/execution/inspection/`, protocol prompts, `docs/{flow-syntax,program,call-input,execution-presentation}.md` |

Acceptance tests must prove:

1. Both target forms parse/format/serialize round-trip; invalid forms retain exact
   diagnostics. A target added after Flow acceptance resolves at the exec boundary.
2. Flow-to-Flow, Flow-to-Agic, and Agic-to-Flow handoffs preserve Run identity,
   input provenance, limits, resource scope, and original output validation.
3. Exec inside nested repeats stops every enclosing loop in that Run, skips their
   remaining body/until and later Flow statements, and closes each Step exactly
   once. An enclosing parent Run continues waiting and subsequently resumes.
4. Bad/missing targets, missing inputs, unauthorized dynamic routes, and lineage
   reentry commit no handoff. Failure injection around native commit leaves either
   an uncommitted failure or complete committed terminal facts; never a half-closed
   old loop followed by successor work. Cancellation does not undo committed exec.
5. `_toolang__exec` retains the one-call rule and receipt semantics;
   `_toolang__execute` is no longer advertised or callable.
6. Repeated handoffs, nested loops, empty successors, and failed exec render
   consistently in Script, Chat, and inspection. Dividers align with the owning
   Run, display numbering restarts, and canonical references remain unique.
7. A generation-check child observes its newly bound instruct, returns false
   after waiting when stale, and a later fresh child can observe the new marker.
   Generation stays an author-managed convention, not an ordered State revision.

For this definition PR, verify source/link accuracy and `git diff --check`.
Implementation requires the repository's full default verification. The separate
grammar PR requires `npm run check`, Python binding tests, and `cargo test`.

## Risks and open questions

Renaming the tool is an intentional callable-API break. Reserving lowercase
`exec` changes implicit prose beginning with that token; explicit text stays
literal. Native exec must not be swallowed by generic Step failure handling.
Dynamic targets require authorization and exact-snapshot binding. Display
ordinals must never leak into durable addressing. Grammar publication is a
delivery dependency.

No unresolved design alternatives are required to implement this proposal.
Human approval of this definition and merge/release decisions remain pending.

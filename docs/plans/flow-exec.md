# Flow exec and runtime exec naming

Follow-up definitions: [live State resolution](runtime-live-resolution.md)
replaces binding, resource-freezing, and lineage rules below; the separate
[grammar plan](https://github.com/openhat-ai/tree-sitter-toolang/pull/41)
replaces target syntax with named runnables and inline agics, excluding dynamic
target expressions. Unrelated handoff and presentation rules remain unchanged.

## Goal and scope

Proposed definition; this PR contains no implementation. `run` calls a child
and returns; `exec` replaces the current runnable within the same Run and never
returns on success. Add Flow `exec` and rename `_toolang/execute` to `_toolang/exec`.
Success includes handoff from nested repeats and clear presentation of each successor.

Scope: language consumers, runtime tools, handoff records, repeat unwinding,
and shared Script/Chat/inspection presentation. Root spawning, scheduling,
ordered State revisions, hot reload, and broader resource authority are excluded.
Grammar implementation belongs in a separate `openhat-ai/tree-sitter-toolang` PR.

## Syntax and binding

```too
exec grow_v2
exec {{successor}}
exec {{next.runnable}}
```

- Allow exec directly in Flow and repeat bodies. It has no result binding,
  inline body, argument-list syntax, or modifiers; both `let x = exec ...` and
  `let exec ...` are invalid. Successful exec skips all remaining old statements.
- Literal targets use the existing named-runnable token. Dynamic targets accept
  exactly one `{{path}}`: a local, including `_`, followed by optional dotted
  fields. Permit horizontal whitespace inside braces. No multiline references,
  mixed text, expressions, or prompt expansion. The selected value must be
  nonempty Text containing an existing runnable-reference form.
- Flow exec captures the latest published State once at acceptance and uses it
  for resolution, input validation, and binding. The target may have been added
  after the old Flow was accepted; do not require it in the old program.
- Literal targets carry authored-call authority, like `run`. Dynamic targets
  must match explicit or inherited `handoffs`; `none` or user-requested-only
  authority is insufficient for autonomous dispatch. Preserve module visibility
  and generated-target restrictions.
- Bind only the target's declared inputs from current locals, using named-`run`
  rules and preserving typed provenance. Missing or incompatible inputs fail
  before handoff. Lower to a typed `ExecStmt`; runtime must not reparse source.

## Handoff and records

The [shared executor](../../src/toolang/execution/executor/executor.py) already
supports agic/Flow replacement. Reuse it through these two frontends:

| Frontend | State and input |
| --- | --- |
| Flow `exec` | Latest published State at acceptance; selected Flow locals. |
| `_toolang/exec` | Existing advertised catalog snapshot; `{runnable, input}`. |

Preserve Run ID, parent, thread, captured Setup, effective resource ceiling,
root accounting, and the original entry output contract. Start the successor
from its entry with its selected State and bound inputs. Current/ancestor lineage
reentry, including earlier handoff targets, remains forbidden. Existing agic-local
counter resets and Run retry/rerun rules remain unchanged.

- Add an `exec` Step kind whose `given` is `ExecStmt`, with no output. Its terminal
  facts identify the committed control and resolved target; do not fabricate a
  child Run or model ToolCall. Preserve the durable `execute` control kind and
  extend its trigger/input handling to native Steps and typed Flow locals.
- Commit the native handoff and closure of its Step and open repeat ancestors
  atomically before starting the successor. Each loop ends once with
  `status = succeeded`, `termination = exec`, and `aborted_by` referencing that
  control. Count only fully completed iterations. Bypass generic Step failure
  wrapping and result binding while propagating the transfer.
- Unwind only within the current Run. A parent's `run` Step continues waiting
  for the successor's final result; child exec cannot exit a parent's repeat.
- Pre-commit rejection changes no binding. Tool rejection returns an error to
  the model; native exec failure follows normal Flow failure propagation.
  Post-commit cancellation or delivery failure cannot undo the handoff.

Keep the tool's existing success receipt, `{controls: [...]}`, and requirement
that exec be the only tool call in its model response. Expose `_toolang__exec`
to providers; remove the old callable name without an alias. Update protocol,
current docs/examples, and tests; retain ordinary internal `execute()` functions
and historical records. Follow existing store/cache versioning policy where
encodings change, without adding migrations solely for the public rename.

## Presentation

Align the handoff divider with its owning Run's entry boundary, outside exited
repeat blocks. New Steps belong at the Run's top level. Add no intermediate Run
footer or new Run identity; preserve existing divider width/style rules.

```text
Run R · flow grow                         running
└─ Step 0 · repeat                        handed off
   └─ exec grow_v2                       succeeded

── exec → flow grow_v2 ───────────────────────────
└─ Step 0 · run improve                   running
```

The hierarchy illustrates alignment; it does not add Run headers to surfaces
that omit them. Display numbering restarts at zero for each successor. Physical
StepRefs stay monotonic and unique; links and API pointers remain unchanged.
Details expose canonical pointers when display numbers are ambiguous.

Derive boundaries from committed handoffs, including empty successors. Rejected
exec creates no boundary. Loops display “handed off”; Run totals continue without
reset or double counting. Apply the same rules in Script, Chat, and inspection.

## Separate grammar PR and implementation touchpoints

In [tree-sitter-toolang](https://github.com/openhat-ai/tree-sitter-toolang), add
`exec_statement` with `target: runnable | exec_target_reference`; the reference
exposes `path`. Add `flow_exec_keyword` and scanner recognition at Flow structural
boundaries. Explicit text and prefixes such as `executor` stay unchanged. Keep
exec out of bindable operations and prevent invalid exec from recovering as prose.

Update grammar, scanner, `GRAMMAR.md`, queries, corpus/fixture/binding tests, and
regenerated parser artifacts together. Cover nesting/dedents, comments/docs,
LF/CRLF/EOF, and malformed targets/bindings/bodies. Grammar release/version bumps
remain separate; Toolang updates the published dependency and consumers together.

| Concern | Toolang owners |
| --- | --- |
| Syntax and validation | `src/toolang/lang/`; `pyproject.toml`, `uv.lock` |
| Handoff and loop lifecycle | `src/toolang/execution/executor/` |
| Records and tool contract | `src/toolang/execution/{types,records,events,schemas,store}.py`; `src/toolang/base/protocols/tool.py`; `src/toolang/execution/tools/_toolang.py` |
| Presentation and docs | `src/toolang/cli/common/execution_progress/`; `src/toolang/execution/inspection/`; protocol prompts and current language/runtime docs |

## Acceptance and risks

Acceptance tests must cover:

1. Target syntax/format/AST round trips and precise invalid-form diagnostics;
   a successor published after Flow acceptance resolves from the captured State.
2. Flow-to-Flow, Flow-to-Agic, and Agic-to-Flow transfers preserve identity,
   provenance, resource limits, accounting, and original output validation.
3. Nested repeats close exactly once and skip remaining body/until statements;
   the enclosing parent Run keeps waiting and later resumes normally.
4. Invalid targets/inputs, unauthorized dynamic routes, and lineage reentry
   commit no transfer. Failure injection verifies atomic native handoff records
   and cancellation behavior after commit.
5. `_toolang__exec` retains the receipt and one-call rules; the old name is neither
   advertised nor callable. Repeated/empty/failed handoffs render consistently,
   with Run-aligned dividers, reset display numbering, and unique stored pointers.

Verify this definition against source and run `git diff --check`. Implementation
requires each repository's default checks and the acceptance tests above.

Risks: the tool rename breaks callers; reserving lowercase `exec` changes implicit
prose starting with that token; transfer exceptions need dedicated handling;
parser publication gates Toolang integration. No open technical alternatives;
human definition approval and merge/release decisions remain pending.

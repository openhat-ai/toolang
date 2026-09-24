# Flow Rules Outline

Design outline for implementation preparation. Scope: runnable contracts, flow
execution, and runtime context. Resolve the open decisions before implementing
the affected behavior.
Success: determine signatures locally, validate operation contracts before calls,
and provide bounded, unambiguous iteration and thread context.
tree-sitter-toolang is outside this scope.

## 1. Determine the Signature

- Agic / flow declarations: explicit fields take precedence; restore omitted
  fields using declaration defaults: parameters `(_)`, output `Text`.
- Adhoc agics: infer parameters from their own template references; use the
  enclosing operation's output default when the output is omitted.
- Explicit `()` declares no parameters. Declared/inferred `_` is required.
- Parameter type defaults: `_` is `Part[]`; other parameters are `Text`.
- Inference includes ordinary external references and respects template scope.
  Runtime variables are supplied by execution rather than inferred as parameters.
- Determine the signature locally, then check its compatibility at each use.

## 2. Check the Operation Contract

Defaults below apply to adhoc agics. Declared runnables keep their own output
contracts. Constraints apply to both. `T` means any supported value type.

| Operation / role | Child primary input | Default output | Required output type |
| --- | --- | --- | --- |
| run / seek | According to signature | Text | T |
| scatter | May include or omit `_` | Text[] | T[] |
| storm | May include or omit `_` | Text | T |
| map | Must include `_` | Text | T |
| keep / drop predicate | Must include `_` | Boolean | Boolean |
| sort scorer | Must include `_` | Number | Number |
| gather | Must include `_` | Text | T |
| settle with from | Must include `_` | Text | T; initial value must also satisfy T |
| settle without from | Must include `_` | Text | Must match the source element type |
| until | May include or omit `_` | Boolean | Boolean |

- Validate required arguments, input types, output types, and runtime context.
- Map/keep/drop/sort/settle pass one element as `_`; gather passes the whole list.
- Scatter/storm pass `_` when it appears in the child's signature.
- Map/storm collect child outputs into `T[]`, preserving array-valued elements.
- Gather/settle produce one value of type `T`, which may itself be an array.
- `ask` and content-valued `let` produce `Part[]`.

## 3. Execute Collection Operations

- `scatter using ...`: call once; the child's full array type and returned length
  determine the result.
- `storm N using ...`: call N times and preserve result order.
- List consumers require present, list-shaped input.
- Empty input: map/sort/keep/drop return typed `[]` with zero child calls;
  gather/settle report an error before child calls.
- Empty map results use the child's output type; sort/keep/drop preserve the
  source element type. Positional keep/drop follows the same empty-input rule.
- Validate known contracts before calls and bind results after successful completion.

## 4. Initialize and Execute Settle

```too
settle:
  Incorporate {{_}} into {{_1._}}.
  Preserve the report structure.

  from:
    Research report
    No findings yet.
```

- The main body processes elements; an optional final `from:` clause supplies initial Content.
- Infer the adhoc signature from the main body only. Resolve `from` references
  against the surrounding flow context; they are initializer dependencies.
- With `from`, the body's output type T determines the type of `_1._`. Render and
  convert the initializer to T before the first child call; validate later results as T.
- Evaluate `from:` once before entering settle's iteration scope, using the
  surrounding context and let's Content rules. Outer iteration history remains
  visible during initialization. Its value belongs to settle state; flow locals stay unchanged.
- Each call receives the current element as `_` and the previous result as `_1._`
  under the shared-frame proposal below.
- With `from`, the initial value supplies the first `_1._`; N elements require N calls.
- Without `from`, the first element supplies the initial `_1._`; iterate from the
  second element, requiring N-1 calls. A singleton returns its element after
  contract validation; empty input still fails.
- Without `from`, require the body and final output to have the source element
  type. Validate the locally determined signature against that type; use an
  explicit output annotation when the default Text does not match.
- Named reducers accept the same trailing initializer, without changing their
  signature. Omit the block when no initializer is needed:

```too
settle using merge:
  from:
    Initial report.
```

## 5. Retain Iteration History

Proposed retention and shared-frame rules:

```too
repeat 10 times holding 3:
  run: Improve {{_}}.
  until:
    Current result: {{_}}
    {{#_2}}
    Return true only if the current result, {{_1._}}, and {{_2._}} are equivalent.
    {{/_2}}
    {{^_2}}
    Return false.
    {{/_2}}

settle using merge:
  from:
    Initial report.
```

- Repeat accepts optional `holding N`, default 3; N is a positive integer.
  Capacity counts prior frames, excluding the current iteration, independently
  of the iteration limit. Nested repeats use their own setting/default.
- Settle has no retention clause. For an agic reducer, infer capacity as
  `max(1, highest referenced history index)`: `_1` needs 1; `_1` and `_3` need 3.
  With no history references, retain the single accumulator frame.
- Derive that requirement locally for both named and adhoc agics from their own
  resolved templates, including section guards and authored instruct/context.
  Keep it separate from the parameter/output signature. Do not scan callees or
  settle's `from`, whose history references belong to the surrounding scope.
- `_1`, `_2`, ... are frame snapshots, nearest first, for both operations.
  `_1._` reads the previous output; repeat also exposes `_1.report` etc.
- Settle frames contain the cumulative output as `_`, of type T. `from` supplies
  one seed frame, not a list of frames, even when T is an array. After the first
  call, `_1._` is the new output and `_2._` is the seed, subject to window capacity.
- Repeat starts with no prior frames; settle starts with its single seed frame.
  Keep only actual entries; never pad by repeating a seed/output or inventing values.
- An absent `_k` within the window can be guarded with a template section;
  reading it in a rendered branch is an error. References beyond the configured
  window, or without an iteration scope, are errors even in guards.
- History-frame section guards test presence, independent of empty/false/zero
  output values. Existing template section scoping applies: render the current
  `_` outside a history-frame section; use qualified `_k.field` inside it.
- Until evaluates normally with the available history. Guard comparisons that
  need more frames and return false during warm-up, as above. Conditions based
  only on current values can still terminate immediately.
- History stays fixed throughout an iteration, including `until`.
- Order: execute body -> evaluate until against current locals and prior frames ->
  save the successful frame -> stop or continue.
- Save ordinary frame values once per completed iteration, including unchanged
  values. Failed iterations add nothing; retries preserve one entry per iteration.
- Snapshots retain types and provenance, exclude runtime variables, and remain
  immutable. Compaction refreshes live thread variables, not saved frame values.
- Nested iterations replace the history family and restore it on exit. Resolve
  history exclusively within the active iteration scope; concurrent runs are isolated.
- Runtime history references are reserved, read-only execution values.

## 6. Select Thread History

| Variable | Value |
| --- | --- |
| `_f` | Older thread-history summary as Text; empty Text when absent |
| `_n` | Ordered recent messages with roles/content; empty array when absent |
| `_h` | Combined messages: summary followed by recent messages |

Read history:

| Execution | History behavior |
| --- | --- |
| Root agic | Use runtime history variables; automatically include messages selected by recall |
| Child agic | Use runtime history variables explicitly; accumulate its own model/tool conversation |
| Root flow | Use runtime history variables; descendant agics follow the child rule |
| Child flow | Use runtime history variables; descendants follow the child rule |

- Root execution establishes the shared historical boundary. Recall controls
  automatic message inclusion independently of variable availability.
- Flow Content and until can read `_f`, `_n`, and `_h`. The runtime supplies them
  from one shared history version across the active root and its descendants.
- Successful compaction atomically publishes a new version. Subsequent model
  calls and flow frame evaluations acquire that version; automatic recall,
  `_f`, `_n`, and `_h` within one evaluation all use the same version.
- In-flight evaluations keep their acquired version. Record the selected version
  for replay; failed compaction leaves the previous version active.
- Pass current execution progress through `_`, named arguments, or iteration
  frames. Compaction changes the representation of prior thread history; it does
  not publish the active run's intermediate work into that history.
- Nested iterations retain the thread-history context while replacing `_1` etc.

Contribute history:

- Only root runnables contribute a thread-history exchange. Root agics preserve
  their existing model/tool exchange and terminal reply behavior.
- Proposed root flow policy: retain the entry `_` as a user message when present,
  followed by the final output as an assistant message. Keep existing control
  and failure/cancellation handling; do not manufacture a successful output.
- Child transcripts and intermediate flow frames remain execution records;
  they do not become independent thread-history exchanges.

## 7. Configure Execution

- Agic configuration: tools, caps, models, hands, handoffs, recall, instruct, context.
- Flow configuration: lanes. Resource choices belong to agics or agent setup.
- `instruct:` and `context:` retain their existing setting forms.
- Proposed caps configuration: one union of psyches/skills/services/prompts,
  using existing selection operations and scope rules.
- Lane precedence: statement clause > enclosing flow directive > built-in default.
- The built-in lane count is 4.
- `lanes = N`: one positive integer per flow; statement lane clauses are optional.
- Apply lanes to storm/map/predicate keep/drop/sort, preserving result order.
  Repeat bodies use their enclosing flow's setting; named flows use their own.
  Settle runs sequentially.

## 8. Validate and Migrate

- Validate signatures, arguments, types/shapes, operation contracts, configuration,
  counts, lanes, and runtime availability; report known failures before model calls.
- Acceptance coverage: defaults, adhoc inference, use-site compatibility, empty
  inputs, lane precedence, initializer timing, and history scope/isolation;
  repeat retention defaults/overrides, settle's local depth inference (including
  guarded references and excluding the initializer), eviction, warm-up with empty/false/zero
  values, retry/resume, nested scopes, and compaction during concurrent calls.
- Update affected examples and prepared caches for changed contracts.
- Use `_f`, `_n`, `_h`, and numbered history variables as the runtime names.
  Remove the old runtime bindings; provide no compatibility aliases. Reserve
  runtime names against user bindings and exclude them from signature inference.
  Migrate runtime uses of bare `far`/`near` and settle's `item` without rewriting
  unrelated user-defined names.
- Documentation check: `git diff --check`. Implementation: repository default checks.
- Touchpoints: `src/toolang/lang/{ast,lower,input,format,validate}.py`, template
  runtime-variable resolution, execution
  `executor/stmts/{repeat,settle}.py`, runnable frames, `assembly/{history,prompting}.py`,
  and store projections. Main risks: contract migration, frame retention, and
  mixing history versions during concurrent evaluation.

## 9. Open Decisions

- Confirm the root flow input-plus-final-output history policy.
- Confirm repeat's `holding N` (default 3), inferred settle retention for agic
  reducers, frame-valued `_k`, and guarded warm-up behavior. The proposal treats
  `from` as one seed, not prefilled history.
- Define settle retention for flow reducers and indirect/dynamic history
  dependencies; local agic inference does not determine callee requirements.
- Caps-key migration and removal of existing flow resource directives.

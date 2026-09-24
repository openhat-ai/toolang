# Flow Rules Outline

Goal: local signatures, validated operation contracts, and bounded iteration/thread
context. This is a design outline; resolve open decisions before implementation.
tree-sitter-toolang is outside scope.

## 1. Determine the Signature

- Agic / flow declarations: explicit fields take precedence; omitted fields use
  declaration defaults: parameters `(_)`, output `Text`.
- Adhoc agics: infer parameters from their own scoped template references; omitted
  output uses the operation default. Runtime references are not parameters.
- Explicit `()` declares no parameters. Declared/inferred `_` is required.
- Parameter type defaults: `_` is `Part[]`; other parameters are `Text`.
- Parameter/local names cannot start or end with `_`, except primary `_`.
  Internal underscores and data-field names are unrestricted by this rule.
  Reject unknown reserved references rather than inferring parameters.
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
- Bind results only after successful completion.

## 4. Initialize and Execute Settle

```too
settle:
  Incorporate {{_}} into {{_1._}}.

  from:
    Initial report.
```

```too
settle using merge:
  from:
    Initial report.
```

- Each call receives the current element as `_` and the previous result as `_1._`.
- Optional trailing `from:` supplies one initial value, even when that value is
  an array. Named reducers use the same clause; omit the block when unused.
- Determine the reducer signature independently of `from`. Evaluate the
  initializer once using let's Content rules and the outer context, before
  entering settle's iteration scope. Outer history remains visible; no local is created.
- With `from`: convert the seed to reducer output type T before calls; every
  result must satisfy T. N elements require N calls.
- Without `from`: use the first element as seed and make N-1 calls. Reducer
  output must match the source element type; declaration/operation defaults
  remain unchanged. A singleton returns its element after validation; `[]` fails.

## 5. Retain Iteration History

```too
repeat 5 times windowing 3:
  run: Improve {{_}}.
  until:
    Current result: {{_}}
    {{#_2}}
    Return true only if the current result, {{_1._}}, and {{_2._}} are equivalent.
    {{/_2}}
    {{^_2}}Return false.{{/_2}}
```

- Repeat: optional `windowing N`, default 3; N is a positive integer counting
  prior frames, excluding the current round. The iteration limit is independent;
  nested repeats use their own setting/default.
- Settle: no window clause. For named/adhoc agic reducers, infer capacity as
  `max(1, highest referenced history index)` from their own resolved templates,
  including guards and authored instruct/context. This is separate from the
  signature; exclude callees and `from`.
- `_1`, `_2`, ... select historical frames, nearest first. Each completed frame
  contains entry and exit snapshots; the pair occupies one window slot.

| Reference | Snapshot value |
| --- | --- |
| `_k.name` | Local `name` at that iteration's end |
| `_k._name` | Local `name` at that iteration's entry |
| `_k._` | Primary output at that iteration's end |
| `_k.__` | Primary value at that iteration's entry |

- Repeat: capture entry -> execute body, updating locals after each statement ->
  evaluate until -> save entry/exit snapshots -> stop or continue. History stays
  fixed throughout the body and `until`.
- Settle captures entry locals after binding the current element as `_`, before
  invoking the reducer. Its exit snapshot replaces `_` with the cumulative
  output of type T; reducer-private state is excluded.
- Repeat starts without history; settle starts with one seed frame containing
  exit `_` only. After its first call, `_1.__` is the processed element, `_1._`
  is the result, and `_2._` is the seed if retained. Never pad missing history.
- Snapshot selection applies to the first field after `_k`; later fields read
  data, as in `_2._report.title`. Entry/exit access requires the same history depth.
- Missing bindings remain absent: a local first created during a round has no
  entry value. Direct reads fail; do not substitute exit values or outer locals.
- An absent `_k` within the window can be guarded with a template section;
  reading it in a rendered branch is an error. References beyond the configured
  window, or without an iteration scope, are errors even in guards.
- History-frame section guards test presence, independent of empty/false/zero
  output values. Existing template section scoping applies: render the current
  `_` outside a history-frame section; use qualified `_k.field` inside it.
- Until can terminate before the window fills; guard history-dependent comparisons
  and return false during warm-up, as above.
- Save one pair per successful round, including unchanged values. Failed rounds
  add nothing; retries do not duplicate entries. Settle saves after each reducer call.
- Capture only ordinary locals and the primary `_` on both entry and exit.
  Exclude injected runtime bindings such as `_1`, `_2`, `_f`, `_n`, and `_h`,
  even when present in iteration input; generate entry selectors only for the
  retained bindings. Neither `_k._1` nor `_k.__1` retains earlier history frames.
- Filtering applies to frame bindings, not fields inside ordinary data values.
  Snapshots retain types and provenance and remain immutable. Compaction
  refreshes live thread variables, not saved frame values.
- Nested iterations replace the history family and restore it on exit. Resolve
  history exclusively within the active iteration scope; concurrent runs are isolated.

## 6. Select Thread History

| Variable | Value |
| --- | --- |
| `_f` | Older thread-history summary as Text; empty Text when absent |
| `_n` | Ordered recent messages with roles/content; empty array when absent |
| `_h` | Combined messages: summary followed by recent messages |

- All runnables, including flow Content and until, can read runtime-supplied
  `_f/_n/_h`. Only root agics automatically include messages selected by recall;
  child agics reference history explicitly and keep their own model/tool conversation.
- The root and descendants share a historical boundary and version. Recall
  controls automatic inclusion, not variable availability; nested loops preserve
  thread context while replacing iteration history.
- Successful compaction atomically publishes a new version for subsequent model
  calls and flow frame evaluations. Each evaluation's recall and `_f/_n/_h` use
  one version. In-flight evaluations keep theirs; failed compaction keeps the old
  version. Record the selected version for replay.
- Current progress travels through `_`, named arguments, or iteration frames;
  compaction does not add active-run intermediates to prior thread history.
- Only roots contribute thread exchanges. Root agics keep their existing
  model/tool exchange and terminal reply. Child transcripts remain execution records.
- Proposed root flow exchange: entry `_` as user message when present, then final
  output as assistant message. Preserve control/failure/cancellation handling.

## 7. Configure Execution

- Agic configuration: tools, caps, models, hands, handoffs, recall, instruct, context.
- Flow configuration: lanes. Resource choices belong to agics or agent setup.
- `instruct:` and `context:` retain their existing setting forms.
- Proposed caps configuration: one union of psyches/skills/services/prompts,
  using existing selection operations and scope rules.
- Lane precedence: statement clause > enclosing flow directive > built-in 4.
- `lanes = N`: one positive integer per flow; statement lane clauses are optional.
- Apply lanes to storm/map/predicate keep/drop/sort, preserving result order.
  Repeat bodies use their enclosing flow's setting; named flows use their own.
  Settle runs sequentially.

## 8. Validate and Migrate

- Validate signatures, arguments, types/shapes, operation contracts, configuration,
  counts, lanes, and runtime availability; report known failures before model calls.
- Acceptance: signature defaults/inference and use-site contracts; empty inputs;
  lane precedence; initializer timing; window defaults/overrides and inferred
  settle depth; entry/exit values and missing bindings; eviction and warm-up with
  empty/false/zero values; nested isolation, retry/resume, and concurrent compaction.
- Verify reserved names, primary `_` and internal-underscore exceptions; filter
  injected bindings before entry projection while preserving ordinary data fields.
- Update affected examples and prepared caches for changed contracts.
- Resolve entry selectors within history frames using existing template path
  characters. Validate snapshot availability and the reserved binding namespace.
- Use the new runtime names without compatibility aliases. Migrate runtime uses
  of bare `far`/`near` and settle's `item`; preserve unrelated user-defined names.
- Documentation check: `git diff --check`. Implementation: repository default checks.
- Touchpoints: `src/toolang/lang/{ast,lower,input,format,validate}.py`, template
  resolution, execution `executor/stmts/{repeat,settle}.py`, runnable frames,
  `assembly/{history,prompting}.py`, and store projections.
- Risks: contract migration, frame retention, and mixed history versions.

## 9. Open Decisions

- Confirm the root flow input-plus-final-output history policy.
- Confirm inferred settle retention and guarded warm-up behavior.
- Define settle retention for flow reducers and indirect/dynamic history
  dependencies; local agic inference does not determine callee requirements.
- Caps-key migration and removal of existing flow resource directives.

# Flow Rules Outline

Design outline for implementation preparation. Scope: runnable contracts, flow
execution, and runtime context. Resolve the open decisions before implementing
the affected behavior.
Repeat history is a future extension. tree-sitter-toolang is outside this scope.

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
| settle | Must include `_` | Text | T; initial value must also satisfy T |
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
  Incorporate {{_}} into {{_1}}.
  Preserve the report structure.

  from:
    Research report
    No findings yet.
```

- The main body processes each element; the final `from:` clause supplies initial Content.
- Infer the adhoc signature from the main body only. Resolve `from` references
  against the surrounding flow context; they are initializer dependencies.
- The body's output type T determines the type of `_1`. Render and convert the
  initializer to T before the first child call; validate every later result as T.
- Evaluate `from:` once before processing, using the surrounding context and
  let's Content rules. Its value belongs to settle state; flow locals stay unchanged.
- Each call receives the current element as `_` and the previous result as `_1`.
  The initial value supplies the first `_1`; N elements require N calls.
- Settle exposes only the preceding result, including that initial value.

## 5. Retain Iteration History

- `_1`, `_2`, ... address preceding iterations, nearest first.
- Repeat retains the latest N frame snapshots, including ordinary named locals.
  `_1._` reads the previous primary value; `_1.report` reads its `report` local.
- History stays fixed throughout an iteration, including `until`.
- Order: execute body -> evaluate until against current locals and prior frames ->
  save the successful frame -> stop or continue.
- Save ordinary frame values once per completed iteration, including unchanged
  values. Failed iterations add nothing; retries preserve one entry per iteration.
- Nested iterations replace the history family and restore it on exit. Resolve
  history exclusively within the active iteration scope; concurrent runs are isolated.
- Runtime history references are reserved, read-only execution values.

## 6. Select Thread History

| Variable | Value |
| --- | --- |
| `_f` | Older thread-history summary as Text; empty Text when absent |
| `_n` | Ordered recent messages with roles/content; empty array when absent |
| `_h` | Combined messages: summary followed by recent messages |

Proposed root/child policy:

| Execution | History behavior |
| --- | --- |
| Root agic | Prepare history variables; automatically include messages selected by recall |
| Child agic | Inherit variables; select history through templates; accumulate its own model/tool conversation |
| Root flow | Prepare history variables; descendant agics follow the child rule |
| Child flow | Inherit variables; descendants follow the child rule |

- Root execution establishes the shared historical boundary. Recall controls
  automatic message inclusion independently of variable availability.
- Flow Content and until can read `_f`, `_n`, and `_h`.
- Pass current execution progress through `_`, named arguments, or iteration
  frames; the thread-history snapshot stays separate from current activity.
- Nested iterations retain the thread-history context while replacing `_1` etc.

## 7. Configure Execution

- Agic configuration: tools, caps, models, hands, handoffs, recall, instruct, context.
- Flow configuration: lanes. Resource choices belong to agics or agent setup.
- `instruct:` and `context:` retain their existing setting forms.
- Proposed caps configuration: one union of psyches/skills/services/prompts,
  using existing selection operations and scope rules.
- Lane precedence: statement clause > enclosing flow directive > built-in default.
- `lanes = N`: one positive integer per flow; statement lane clauses are optional.
- Apply lanes to storm/map/predicate keep/drop/sort, preserving result order.
  Repeat bodies use their enclosing flow's setting; named flows use their own.
  Settle runs sequentially.

## 8. Validate and Migrate

- Validate signatures, arguments, types/shapes, operation contracts, configuration,
  counts, lanes, and runtime availability; report known failures before model calls.
- Acceptance coverage: defaults, adhoc inference, use-site compatibility, empty
  inputs, lane precedence, initializer timing, and history scope/isolation.
- Update affected examples and prepared caches for changed contracts.
- Documentation check: `git diff --check`. Implementation: repository default checks.

## 9. Open Decisions

- Built-in lane count; proposed 4.
- Final runtime spellings and migration of bare far/near references.
- Root-only automatic recall and history behavior across compaction updates.
- Whether `from:` is required, its omission behavior, and the named-reducer form.
- Initializer access to outer iteration history; proposed evaluation before
  entering settle's own iteration scope.
- Repeat history window N, its configuration, and insufficient-history checks.
- Caps-key migration and removal of existing flow resource directives.

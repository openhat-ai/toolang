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
    Return true only if {{_}}, {{_1._}}, and {{_2._}} are equivalent.
```

- Repeat: optional `windowing N`, default 3; N is a positive integer counting
  prior frames, excluding the current round. The iteration limit is independent;
  nested repeats use their own setting/default.
- Settle: retain exactly one prior frame for any reducer. No window clause or
  capacity inference; `_2` and higher are outside its iteration scope.
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
  is the result, and this frame replaces the seed. Never pad missing history.
- Snapshot selection applies to the first field after `_k`; later fields read
  data, as in `_2._report.title`. Entry/exit access requires the same history depth.
- Missing bindings remain absent: a local first created during a round has no
  entry value. Direct reads fail; do not substitute exit values or outer locals.
- In body/reducer templates, guard absent frames within the window; reading one
  in a rendered branch is an error. Out-of-window or out-of-scope references are
  errors even in guards.
- History-frame section guards test presence, independent of empty/false/zero
  output values. Existing template section scoping applies: render the current
  `_` outside a history-frame section; use qualified `_k.field` inside it.
- For until agics, required depth is the highest history index in their own
  resolved templates, including guards and effective instruct/context, whether
  local or inherited; no references means 0. If fewer prior frames exist, until
  is false without rendering or invoking its runnable. Save the completed body
  frame and honor the iteration limit.
- Only the referenced depth must be available, not the full window: the example
  first evaluates until in round 3; a history-free condition can run in round 1.
  Invalid contracts, references beyond the window, and missing fields in existing
  frames remain errors rather than being converted to false.
- Save one pair per successful round, including unchanged values. Failed rounds
  add nothing; retries do not duplicate entries. Settle saves after each reducer call.
- Capture only ordinary locals and the primary `_` on both entry and exit.
  Exclude injected runtime bindings such as `_1`, `_2`, `_far`, `_near`, and `_past`,
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
| `_far` | Older thread-history summary as Text; empty Text when absent |
| `_near` | Ordered recent messages with roles/content; empty array when absent |
| `_past` | Combined messages: summary followed by recent messages |

- The root runtime owns the full versioned thread-history snapshot for root agics
  and flows. All descendants, including flow Content and until, use that source;
  child runnables never fetch or assemble thread history independently.
- Only root agics automatically include messages selected by recall. Child agics
  choose whether to reference the supplied variables and keep their own model/tool
  conversation. Nested loops preserve thread context while replacing iteration history.
- Proposed recall view: runtime derives `_far/_near/_past` from each runnable's
  effective recall and the root snapshot. All three bindings remain present;
  excluded sources become typed empty values. `_past` always combines the
  selected `_far` and `_near`.

| Effective recall | `_far` | `_near` | `_past` |
| --- | --- | --- | --- |
| `auto` / `far, near` | Summary | Recent messages | Summary followed by recent messages |
| `far` | Summary | `[]` | Summary message, or `[]` if absent |
| `near` | `""` | Recent messages | Recent messages |
| `none` | `""` | `[]` | `[]` |

- Recall is an overridable selection, not a resource ceiling. Derive each view
  from the full root snapshot, not its parent's filtered view: a child may select
  `near` under a parent using `none`, without changing root or sibling policies.
  Missing source history remains empty.
- Successful compaction atomically publishes a new version for subsequent model
  calls and flow frame evaluations. Each evaluation's automatic recall and
  `_far/_near/_past` use one version and one effective policy. In-flight evaluations keep
  theirs; failed compaction keeps the old version. Record version and policy for replay.
- Current progress travels through `_`, named arguments, or iteration frames;
  compaction does not add active-run intermediates to prior thread history.
- Only roots contribute thread exchanges. Root agics keep their existing
  model/tool exchange and terminal reply. Child transcripts remain execution records.
- Root flow exchange: entry `_` as user message when present, then final
  output as assistant message. Preserve control/failure/cancellation handling.

## 7. Configure Execution

- Agic and flow configuration: models, tools, psyches, skills, services, prompts,
  hands, handoffs, recall, instruct, context, lanes. Keep the four capability-kind
  selectors and their independent operations.
- `lanes` and `recall`: omission inherits the direct parent's effective value;
  explicit configuration overrides it. Root defaults are lanes 4 and recall auto.
  Explicit recall auto selects both sources, even under a narrower parent policy.
- Lane precedence: statement clause > current runnable directive > inherited
  value > built-in 4. Allow at most one `lanes = N` per runnable; N must be a
  positive integer. Children may override with a larger value.
- Apply lanes to storm/map/predicate keep/drop/sort, preserving result order.
  Agics supply the default for descendants; repeat bodies use the enclosing
  runnable's value. Statement overrides affect only that operation, not the
  default passed to its children. Limits are per operation, not a shared subtree
  budget; settle is sequential. Repeat windows remain local to each loop.

Proposed inheritance, pending confirmation:

- Resource selectors (`models`, `tools`, and the four capability kinds): root
  base is the agent's allowed resources; child base is its immediate parent's
  effective resources, intersected with the child's module visibility. Compare
  stable identities, not bare names; module changes grant no additional resources.
- Omission inherits the base. Apply directives in source order: `=` intersects
  the active set, `-=` removes matches, and `+=` restores matches from the fixed
  base. A child cannot restore anything excluded by its parent. Kinds are independent.
- Use this rule for agic/flow, named/adhoc, statement calls, public `run`, and
  `execute` transfers. Transfers retain the outgoing runnable's resource boundary.
  State refresh and resume reapply the same boundary without resetting to agent scope;
  siblings do not share mutations. Existing model-binding validation still applies.
- `hands`/`handoffs` authorize the current caller's model-driven routes, not the
  whole descendant call tree. Omission inherits; explicit `=` replaces the route
  selection from public runnables, and empty `hands =` / `handoffs =` disables it.
  With no inherited value, routes are empty. Flow provides defaults to descendants;
  authored flow statements do not require a matching route.
- Route selections may differ from or exceed the parent's selections: a parent
  with `hands = worker` can call a worker declaring `hands = helper`. Neither
  delegation nor transfer expands resource sets; existing recursion checks remain.
- `instruct:` / `context:` keep their forms. Omission inherits; an explicit value
  replaces the inherited setting, `none` disables it, and `default` selects the
  runnable's own module default. Only authored settings propagate; without one,
  use the receiving agic's existing defaults. Bind template references in the
  declaring module, then render in the child's frame; do not concatenate settings
  or implicitly capture parent locals.
- Flow forwards prompt settings to descendants without rendering model messages
  itself. Inherited template dependencies are checked at use sites, not added to
  declaration signatures.
- Cost: parents must allow resources needed by their descendants. Calls relying
  on agent-scope resets or additional module-local capabilities need migration.

| Concern | Current implementation | Proposed target |
| --- | --- | --- |
| Agic/flow resource selectors | Both support models/tools and four capability kinds | Keep these selectors |
| Flow prompt/routing settings | Rejects hands/handoffs/recall; has no instruct/context fields | Same directive set as agic |
| Lanes | Statement clauses only | Agic/flow defaults inherited by children; statement override stays local |
| Statement child agic | Uses enclosing flow resources, otherwise agent resources | Uses immediate parent resources |
| Nested flow | Resets to agent resources | Inherits parent boundary |
| Public run / execute | Rebuilds target resources from agent scope | Preserves caller boundary, including transfers |
| Resource operators | `=` intersects; `+=` adds within the chosen base | Keep operators; consistently use the parent base |
| Hands/handoffs | Each agic selects independently; omission disables routes | Inherit defaults; explicit selection replaces them |
| Instruct/context | Each agic resolves its own setting/default | Inherit defaults; explicit setting replaces them |
| Recall | Each agic independently selects history for model calls; variables remain unfiltered | Inherit/override on agic/flow; proposed filtered views; automatic inclusion only at root agic |

## 8. Validate and Migrate

- Validate signatures, arguments, types/shapes, operation contracts, configuration,
  counts, lanes, and runtime availability; report known failures before model calls.
- Acceptance: signature defaults/inference and use-site contracts; empty inputs;
  lane precedence; initializer timing; repeat window defaults/overrides and fixed
  settle depth 1; entry/exit values and missing bindings; eviction and warm-up with
  empty/false/zero values; nested isolation, retry/resume, and concurrent compaction.
- Verify reserved names, primary `_` and internal-underscore exceptions; filter
  injected bindings before entry projection while preserving ordinary data fields.
- Verify until warm-up returns false with zero child calls, still saves each
  successful round, and distinguishes missing frames from invalid references/fields.
- Verify root flow input/final-output exchanges and shared thread-history snapshots
  across child agics/flows, with automatic recall only in root agics.
- Verify every recall view, omission versus explicit auto, child near under parent
  none, missing sources, and policy/version consistency across compaction and replay.
- Verify lane inheritance through agic/flow chains, root fallback 4, child overrides,
  and statement limits without changing descendant defaults or sibling operations.
- Verify resource narrowing across every call/transfer path, module visibility,
  per-kind operators, empty sets, sibling isolation, and reload/resume boundaries.
  Parent `{a,b}`, child `= a; += b` yields `{a,b}`; parent `{a}`, child `+= b`
  remains `{a}`. Selecting an unavailable concrete model remains an error.
- Verify route inheritance/replacement/empty selections, worker-to-helper routing,
  prompt inheritance/override/none/default with declaring-module resolution, and
  inherited until templates contributing history depth.
- Update affected examples and prepared caches for changed contracts.
- Resolve entry selectors within history frames using existing template path
  characters. Validate snapshot availability and the reserved binding namespace.
- Use the new runtime names without compatibility aliases. Migrate runtime uses
  of bare `far`/`near` and settle's `item`; preserve unrelated user-defined names.
- Documentation check: `git diff --check`. Implementation: repository default checks.
- Touchpoints: `src/toolang/lang/{ast,lower,input,format,validate}.py`, template
  resolution, execution `executor/stmts/{repeat,settle}.py`, runnable frames,
  `executor/{executor,resources,frame}.py`, `runnables.py`,
  `assembly/{history,prompting}.py`, and store projections.
- Risks: contract/resource-scope migration, inherited prompt dependencies,
  frame retention, and mixed history versions.

## 9. Open Decisions

- Define required iteration-history depth for until flows and indirect/dynamic
  dependencies. This concerns `_k`, not the shared thread variables `_far/_near/_past`;
  local agic inference does not determine callee requirements.
- Confirm the proposed inheritance rules, including parent resource boundaries
  across public calls/transfers, module-local capability restrictions, overridable
  route/prompt defaults, and recall-filtered `_far/_near/_past` views from a shared
  root snapshot.

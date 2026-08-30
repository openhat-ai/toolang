# Inspect Historical Execution Trees

## Status

Approved and merged by pull request 413. The canonical execution record fields
prerequisite was implemented by pull request 415.

## Goal

Make historical execution investigation overview-first and pointer-driven. A
user can render a Run as a durable execution tree, inspect the actual call
owned by a supported Step, find slow or failed work, and follow exact Run IDs
and StepPaths into records, values, and child execution. `tree` is Run-only;
`call` is Step-only and dispatches by the durable Step kind. These additive
overviews do not replace primitive record and relation investigation.

## Success Criteria

- `inspect RUN tree` shows the selected Run's complete visible Run/Step subtree.
- `inspect CONTAINER_STEP call` shows only execution owned by that Step, rooted
  at the Step rather than its child Run.
- `inspect MODEL_STEP call` and `inspect TOOL_STEP call` show the normalized
  call owned by the selected Step; `model-call` is removed without an alias.
- Every row contains an unmodified, reusable Run ID or StepPath.
- Rows show operation, status, wall duration, occurrence, failure, and durable
  subtree metrics without claiming exact event replay.
- `inspect STEP runs` lists direct child Runs; `inspect LOOP_STEP steps` lists
  direct nested same-Run Steps.
- `RUN steps` never crosses a Run ownership boundary: a Run Step exposes its
  direct child-Run count and continues through `STEP runs`, then `RUN steps` on
  the selected child Run.
- Existing implicit `fields`/`value` projections remain unchanged, while root
  `runs`, `THREAD runs`, and `RUN steps` expose ownership in flat form.
- Implicit `fields`/`value` and the `steps`/`runs` relations do not construct a
  tree, aggregate tree metrics, or depend on a structural projector being
  registered.
- Only Runs advertise `tree`; only supported whole Step records advertise
  `call`. Run and Step subjects never expose equivalent projector tokens.
- Human and JSON structural projections use the same node set and order; only
  Human output may best-effort resolve error text.
- Inspection stays local, read-only, and offline; this feature introduces no
  additional store schema change.

## Durable Model And Current Gap

The current inspector has root `runs`, flat `RUN steps`, Pointer fields, and a
model-Step `model-call` projector. It cannot show the complete execution owned
by a flow Step or project a tool Step's normalized call. This feature replaces
`model-call` with the Step-only `call` projector; it does not keep a spelling
alias.

Durable execution has two ownership edges:

1. An invoked runnable is a separate `RunRecord` whose `parent` is the invoking
   StepPath. The child Run owns its own Steps.
2. A repeat-body statement stays in the same Run; its `StepPath.parent` is the
   enclosing loop StepPath.

```text
run_root (flow:research)
  -> run_root.1 (scatter Step)
       -> run_expand (child Run)
            -> run_expand.0 (model Step)
  -> run_root.2 (parallel map Step)
       -> run_search_a (child Run)
       -> run_search_b (child Run)
```

The store has Runs, Steps, Controls, timestamps, occurrences, errors, outputs,
and model accounting. It does not persist the complete ordered `RunEvent`
stream or part deltas. Historical inspection can reconstruct structural truth,
not exact event interleaving or replay.

## Prerequisite

Pull request 415 implemented `docs/plans/canonical-execution-record-fields.md`.
Execution trees use the resulting durable `ThreadRecord.id`, `RunRecord.occur`,
and `StepRecord.occur` fields and the explicit `str` Run identity type. This
plan does not repeat that schema break or add another migration.

## Scope

Included:

- Run-only `tree` projection and Step-only, kind-dispatched `call` projection;
- normalized model and tool call projection plus Step-rooted structural calls
  for `run`, `par`, and `loop` Steps;
- direct Step-to-Run and loop-Step-to-Step relations;
- ownership-aware human columns for existing root `runs`, `THREAD runs`, and
  `RUN steps`;
- one execution-owned structural snapshot and metric projection;
- human tree and stable JSON-node output;
- subject-aware availability, bounded errors, help, docs, and offline tests.

Excluded:

- event persistence, exact timeline/replay, or database migration;
- call result envelopes, collection-wide calls, filtering, folding, depth
  limits, critical path, diff, query, or wildcard projectors;
- ejected-history audit, mutation, retry/rerun, live progress, or HTTP changes;
- changes to execution ownership, events, or existing inspect JSON contracts;
- explicit `fields`, `value`, or `records` grammar tokens.

## Workflow And Grammar

```sh
too examples/deep_search.too inspect runs
too examples/deep_search.too inspect script_root runs
too examples/deep_search.too inspect run_root
too examples/deep_search.too inspect run_root tree
too examples/deep_search.too inspect run_root.2 call
too examples/deep_search.too inspect run_root.2 runs
too examples/deep_search.too inspect run_search_a steps
too examples/deep_search.too inspect run_search_a.0 call
too examples/deep_search.too inspect run_search_a.1 call
```

New forms:

```text
RUN tree
SUPPORTED_STEP call
STEP runs
LOOP_STEP steps
```

`CONTAINER_STEP` means a whole Step record whose kind is `run`, `par`, or
`loop`; no other Step kind owns durable child execution today.
`SUPPORTED_STEP` means a whole `model`, `tool`, `run`, `par`, or `loop` Step.
`call` returns a normalized call value for model/tool Steps and a structural
execution tree for container Steps. Agent, human, and value Steps have neither
a separately persisted normalized call nor child execution, so they do not
advertise `call`.

`inspect POINTER` retains the approved implicit projector model: a browsable
canonical value selects `fields`, while a scalar, empty container, resolved
value, or specialized block selects `value`. `fields`, `value`, and `records`
do not become input tokens or new reserved names. Existing Human Pointer
resolution and JSON non-resolution remain unchanged.

Availability derives from the selected subject. Implicit projector kinds are
not reported as child names:

| Subject | Implicit projector | Available child/explicit names |
| --- | --- | --- |
| Thread | `fields` | `runs` |
| Control | `fields` | none |
| Run | `fields` | `steps`, `tree` |
| `run` Step | `fields` | `runs`, `call` |
| `par` Step | `fields` | `runs`, `call` |
| `loop` Step | `fields` | `runs`, `steps`, `call` |
| model Step | `fields` | `call` |
| tool Step | `fields` | `call` |
| agent, human, value Step | `fields` | none |
| browsable field | `fields` | none |
| other field | `value` | none |

An eligible relation remains valid and returns an empty collection when no
visible child was created. Fields never inherit the owner's relations or
projectors. The existing subject-transition registry owns relations, the
projector registry owns `call` and `tree`, and one subject-aware
availability function combines them for dispatch, help, and allowed-name
errors. No renderer or tree handler duplicates availability rules.

## Primitive Investigation Without Trees

The executor remains legible through canonical records and ownership
relations even when `tree` is absent:

```text
ThreadRecord.id
  -> THREAD runs
       RunRecord.parent, control, state, occur
       -> RUN steps
            StepRecord.path (encodes same-Run parent), given, state, noted, occur
            -> STEP runs
            -> LOOP_STEP steps
```

Whole-record `fields` starts from exact durable facts rather than a summary.
Run fields expose the invoking Step, accepted preparation Control, State
Control, and occurrence; Step fields expose its complete path,
authored/runtime begin facts, State, completion facts, status, error, and
occurrence. Existing Human Pointer resolution and its `*TYPE` marker remain in
effect; `--json` stays exact and non-resolving. A Step's same-Run parent is
derived by removing the final index from `path`; it is not a synthetic
canonical record field.

Flat relations expose the same ownership edges with reusable pointers:

- root `runs` lists every visible Run and shows its Thread and `PARENT STEP`;
- `THREAD runs` lists every visible Run in the Thread, including root and child
  Runs, and shows each Run's `PARENT STEP`;
- `RUN steps` preserves its current flat meaning: every visible Step physically
  owned by the selected Run, including nested same-Run Steps, with `PARENT
  STEP` showing nesting;
- `STEP runs` lists only Runs whose `parent` is the selected StepPath;
- `LOOP_STEP steps` lists only same-Run Steps whose `path.parent` is the
  selected loop StepPath.

The human columns are:

| View | Columns |
| --- | --- |
| root `runs` | `RUN`, `THREAD`, `PARENT STEP`, `RUNNABLE`, `OCCUR`, `STEPS`, `STATUS`, `CREATED` |
| `THREAD runs` | `THREAD RUN`, `PARENT STEP`, `RUNNABLE`, `OCCUR`, `STEPS`, `STATUS`, `CREATED` |
| `RUN steps` | `RUN STEP`, `PARENT STEP`, `ACTIVITY` (right of pointers, left-aligned), `OCCUR`, `RUNS`, `STATUS`, `CREATED` |
| `STEP runs` | `PARENT STEP`, `RUN`, `RUNNABLE`, `OCCUR`, `STEPS`, `STATUS`, `CREATED` |
| `LOOP_STEP steps` | `CHILD STEP`, `ACTIVITY` (right of pointers, left-aligned), `OCCUR`, `RUNS`, `STATUS`, `CREATED` |

A root Run uses `-` for `PARENT STEP`; a top-level Step uses `-`. Human
`RUNNABLE` values use the tree's `<flow> name` or `<agic> name` label.
`ACTIVITY` uses the same `[<StepKind>] <operation>` label as the Human tree.
`RUNS` is the number of direct visible child Runs whose `parent` is that
StepPath; it is not a recursive subtree count. `STEPS` is the number of visible
Steps physically owned by that Run, including nested same-Run Steps but
excluding Steps owned by child Runs. Human tables may wrap but never truncate
pointers. JSON relation output remains a bare array of canonical records in the
same order.

### Run Step Walkthrough

For this executor chain:

```text
run_parent -> run_parent.2 (run Step) -> run_child -> run_child Steps
```

`inspect run_parent tree` preserves every ownership boundary:

```text
NODE                   ACTIVITY
run_parent             <flow>  parent
└─ run_parent.2        [run]   <agic> child
   └─ run_child        <agic>  child
      ├─ run_child.0   [model] openai/gpt-5
      └─ run_child.1   [tool]  search
```

The equivalent primitive investigation is deliberately split:

```sh
too AGENT inspect run_parent steps
too AGENT inspect run_parent.2 runs
too AGENT inspect run_child steps
```

The first command contains the parent Step row with `RUNS = 1`; it does not
embed `run_child` or Steps physically owned by `run_child`. The second returns
the direct child Run with `STEPS = 2`. The third returns `run_child.0` and
`run_child.1`. Each command has canonical JSON for only its own relation.

`inspect run_parent.2 call` starts at the Run Step and includes `run_child` and
its Steps, omitting `run_parent` and siblings. `inspect run_child tree` starts
at the child Run and includes its Steps, omitting `run_parent.2`.

Primitive views use focused store reads and dedicated inspection descriptors;
they do not call `RunHistory.describe_runs()`, resolve inputs/errors, walk root
ancestry, aggregate metrics, or instantiate tree nodes. The tree builder
composes the same typed ownership queries; ownership queries do not call the
tree builder. Removing or failing the Run `tree` or container-Step `call`
handler therefore does not change primitive grammar or results. Tree-only
orphan/cycle/control validation and metrics failures do not block an otherwise
decodable record or focused relation view. A primitive view still fails when a
record inside its own requested scope is undecodable or lacks data required for
that view; it does not scan unrelated ancestors or descendants first.

A primitive table that needs several durable tables reads only its requested
scope under one store lock and SQLite read transaction. For example, a Run
collection batches its selected Runs, entry Controls, and Step counts; it does
not load descendant Runs or tree metrics. A one-record `fields`/`value`
projection retains its existing single-record read behavior.

## Consistent Structural Snapshot

Execution history reads the selected subtree, visible Steps, entry Controls,
errors, and accounting under one store lock and one SQLite read transaction.
An active execution may advance after the read, but one result never mixes
records from different database revisions.

Subject parsing may perform the CLI's existing owner lookup, but a Run `tree`
or container-Step `call` projection must re-read and validate its depth-zero
record inside this transaction and use that copy. The frozen snapshot, not the
earlier `RecordSelection` and not later store queries, supplies every tree node,
operation, metric, and best-effort Human error target.

The typed snapshot alternates records:

```text
Run node
  -> top-level Step nodes (StepPath.parent is None)

Step node
  -> merged direct children:
       same-Run Step nodes (StepPath.parent equals this StepPath)
       child Run nodes (RunRecord.parent equals this StepPath)
```

Nested same-Run Steps appear only under their owner, not again under the Run.
Child Runs expose their own top-level Steps. Merging the two Step child sources
is required so a loop can interleave body Steps and condition Runs by logical
iteration. Each visible record appears once.

`RUN tree` accepts root or child Runs and never climbs to ancestors.
`CONTAINER_STEP call` uses the selected Step as depth zero and omits its owning
Run and siblings. The selected Step and every child Run remain separate nodes:
the Step represents the invocation boundary, while the Run represents the
independently identified execution that it accepted. A `run` Step therefore
renders `[run] <kind> name` followed by `<kind> name` for its child Run when
that Run exists; the two rows must never be folded together.

The builder belongs to an execution-owned tree module that depends on the
primitive inspection vocabulary, never the reverse. The CLI only renders typed
nodes and lazily loads the tree handler for Run `tree` and container-Step
`call`; model/tool `call` does not import or instantiate tree machinery.
Missing parents, cycles, or missing entry Controls inside the selected
projection fail before output instead of silently reparenting or dropping data.
The selected depth-zero Run or Step is an intentional boundary: its own
external parent is not loaded or validated.

## Step `call` Projection

`call` is one Step-only projector with kind-specific handlers:

| Step kind | Human result | JSON result |
| --- | --- | --- |
| `model` | existing normalized model-call presentation, including persisted result Parts | canonical normalized `ModelCall` |
| `tool` | normalized invocation plus the persisted tool-result Part when present | canonical normalized `ToolCall` |
| `run`, `par`, `loop` | structural tree rooted at the selected Step | the same flat tree-node array as Run `tree` |

The model handler renames the existing `model-call` behavior without changing
its logical `ModelCall` or Human result presentation. It continues to rebuild
compact model-call references through the store. `model-call` is no longer a
recognized projector and is not retained as an alias.

The tool handler reads `ToolStepGiven.call`, which is already stored as a
complete normalized `ToolCall`. Human output identifies the selected plugin,
shows the call ID and tool-call ID, renders the tool name and input, and reuses
the persisted `ToolResultPart` from the Step output when present. It may show
the durable begin/end summaries as secondary text but does not synthesize a
result when output is absent. JSON is the bare canonical `ToolCall`; it does not
add plugin, summary, result, status, or an inspect envelope. Those facts remain
available on the Step record. Its exact keys are `tool_call_id`, `call_id`,
`name`, and `input`, using the stored values without redaction or current-plugin
lookup. The model and tool handlers perform no subtree read or metric
aggregation.

The container handler invokes the same structural builder as Run `tree`, but
passes the selected Step as depth zero. It does not delegate by selecting the
child Run: a `run` Step and its child Run are different durable records and
therefore different adjacent nodes. A container Step with no accepted visible
child execution still returns its one Step node.

`call` requires a whole Step record. It is invalid after a field, collection,
Run, Thread, or Control. `tree` requires a whole Run record. Dispatch validates
the subject and Step kind before loading a specialized handler, so invalid
`call`/`tree` combinations cannot trigger call reconstruction or tree reads.
Both projectors are historical, local, read-only, and offline: they do not
prepare a prospective call, select a model or tool, load current Agent State,
send provider traffic, create records, or mutate accounting.

## Ordering

Traversal is depth-first pre-order. Sibling order reflects executor semantics:

- a Run's top-level Steps use numeric StepPath order;
- a `run` Step has at most one visible child Run; more is a tree-structure
  error;
- a `par` Step's child Runs use `occur.item.index`, then Run ID;
- a `loop` Step merges direct same-Run Steps and child Runs, groups them by
  `occur.iteration.index`, orders `body` before `until`, orders same-Run Steps
  before direct Runs within a phase, and then uses numeric StepPath or
  `occur.item.index`;
- a tree rejects missing item/lane coordinates under `par` and missing
  iteration/phase coordinates under `loop`, because the current executor always
  records them.

When malformed data needs a deterministic non-semantic tie-breaker, use
`created_at` and canonical Run ID. Do not read, expose, or interpret SQLite
`rowid` as executor or event order.

Every parallel child still displays item and lane occurrence; lane assignment
does not determine tree order. Human and JSON tree output share this order.

## Node Facts And Metrics

Every node has:

- canonical pointer, record kind, nullable Step kind, direct in-projection
  parent, and relative depth;
- operation, status, and occurrence;
- started/finished timestamps and canonical error;
- subtree Run/model-call/tool-call counts, tokens, and selected USD cost.

Operation comes from durable typed data: a Run entry runnable; or, for a Step,
its stored flow statement via `format_statement_head()`, recorded model ID,
or recorded tool name.

Human labels distinguish record kinds consistently. A Run converts its
canonical runnable reference to `<runnable-kind> name`: `flow:parent` becomes
`<flow>  parent` and `agic:child` becomes `<agic>  child`. An unexpected
noncanonical runnable is displayed as `<?>     raw-value` without making the
tree unreadable. Every Step uses `[<StepKind>] <operation>`, such as `[model]
openai/gpt-5`, `[tool]  search`, `[run]   <agic> child`, or `[par]   map
search_web par 4`. A `run` Step remains identified by its leading `[run]` tag;
when its stored statement contains a canonical runnable reference, that token
uses the same `<runnable-kind> name` rule as a Run. Other statement operands,
such as scatter cardinality, remain visible. The ASCII type tag occupies a
seven-character, left-aligned slot followed by one space;
therefore the content after all angle- and square-bracket tags starts at the
same display column. When a flow statement head begins with the same word as
its Step kind, remove that one redundant word. For non-`run` flow Steps,
otherwise retain the complete statement head. These are Human formatting rules
only. Padding is not data. JSON keeps the raw runnable operation,
`record_kind`, the durable Step `kind`, and `operation` structurally separate.

Metrics count each visible node once:

- Run count means Run nodes strictly below the metric owner, so a Run never
  counts itself and a Step counts every child Run in its subtree;
- model/tool Steps count themselves as calls;
- tokens prefer `ModelAccounting` totals and fall back to legacy
  `ModelStepNoted.tokens`;
- cost prefers the selected USD accounting amount and falls back to legacy
  `ModelStepNoted.cost` as an approximate USD amount;
- usage is complete only when every counted model Step has known input and
  output tokens;
- cost is complete only when every counted model Step has either a selected,
  complete USD accounting amount or a legacy cost value; estimated or legacy
  cost makes approximation explicit;
- ejected nodes contribute nothing.

Known partial tokens and cost use the `+` lower-bound convention; approximate
cost uses `~$`, allowing `~$0.01+`. Completely unknown values are omitted in
human output, not displayed as zero. In JSON, completely unknown token totals
are `null`, while partial known totals remain numeric with
`usage_complete: false`. Zero counted model Steps makes usage and cost complete
with zero tokens and a `null` cost. Wall duration is each node's own recorded
`finished_at - started_at`, is never summed, and is omitted for a running node
or missing timestamp.

## Human Projection

```text
NODE                   ACTIVITY                        OCCUR                STATUS     DURATION  METRICS
run_root               <flow>  research                                     succeeded  1m16s     26 runs · 32 model calls
├─ run_root.1          [run]   scatter 6 expand_queries                      succeeded  3.0s      1 run · 1 model call
│  └─ run_expand       <agic>  expand_queries                               succeeded  3.0s      1 model call
│     └─ run_expand.0  [model] openai/gpt-5                                 succeeded  3.0s      ↑184 ↓47
└─ run_root.2          [par]   map search_web par 4                           succeeded  31.0s     6 runs · ...
   ├─ run_search_a     <agic>  search_web             item 1/6 · lane 1/4  succeeded             ...
   └─ run_search_b     <agic>  search_web             item 2/6 · lane 2/4  succeeded             ...
```

Human tree output combines tree guides and the exact reusable pointer in one
left-aligned `NODE` column; there is no separate `TREE` column. `ACTIVITY` is
the column immediately to its right, is left-aligned, and begins with the
angle- or square-bracket type tag. The fixed-width tag slot makes both the tags
and their following content form stable visual edges. Occurrence, status,
duration, and metrics remain independent fact columns and omit absent values.
The renderer may wrap operations and facts but never truncates or
right-justifies the pointer tree.

A failed/canceled node gets one following error line, collapsed to whitespace
and bounded to 240 Unicode code points. A direct error message is displayed;
an error Pointer is resolved only for Human output. An unavailable target or
cycle leaves the raw Pointer visible with an `unresolved` marker instead of
failing the structural tree. Successful outputs, prompts, messages, and tool
payloads stay collapsed; their pointers lead to focused inspection. An empty
Run or eligible container Step still renders its own node.

The initial tree is complete and unbounded, matching current inspect
collections. Filtering and folding need separate completeness rules.

## JSON Projection

`--json` emits a flat depth-first array, avoiding recursive decoder limits:

```json
[
  {
    "pointer": "run_root",
    "record_kind": "run",
    "step_kind": null,
    "parent": null,
    "depth": 0,
    "operation": "flow:research",
    "status": "succeeded",
    "occur": null,
    "started_at": "2026-01-01T00:00:00Z",
    "finished_at": "2026-01-01T00:01:16Z",
    "error": null,
    "metrics": {
      "runs": 26,
      "model_calls": 32,
      "tool_calls": 8,
      "input_tokens": 43800,
      "output_tokens": 17600,
      "usage_complete": true,
      "cost_usd": "0.01",
      "cost_complete": true,
      "cost_approximate": false
    }
  }
]
```

`step_kind` is `null` for a Run and the durable `StepKind` for a Step. `parent`
is the direct parent inside this projection, so depth zero is always `null`.
`occur` uses the canonical `Occurrence` object. `error` preserves the record's
canonical message or Pointer and is never followed in JSON. Unknown tokens or
cost are `null`; partial known sums remain present with their completeness flag
false. Zero model calls emit zero input/output tokens, `usage_complete: true`,
`cost_usd: null`, and `cost_complete: true`.

## Direct Relations

`STEP runs` returns visible Runs with `run.parent == selected_step.path`, in
the semantic sibling order defined above. Its human form uses the
ownership-aware columns specified above. Unlike a tree, this focused relation
does not enforce container invariants; incomplete occurrence data falls back to
`created_at` plus Run ID and remains visible for diagnosis.

`LOOP_STEP steps` returns visible same-Run Steps with
`step.path.parent == selected_step.path`, in numeric StepPath order. It reuses
the focused child-Step columns specified above.

Both JSON forms remain bare arrays of canonical records. Neither relation is
recursive; returned pointers continue through the same relations or existing
`RUN steps`.

Existing root `runs`, `THREAD runs`, and `RUN steps` retain their collection
semantics and ordering. This feature changes their Human columns only,
replacing the derived `TITLE` column in Run collections with durable ownership,
runnable, and occurrence facts; JSON arrays remain canonical and
compatibility-stable.

## Errors And Help

- Syntax and owner lookup precede projector/relation validation.
- Missing owners retain existing record-not-found errors.
- `fields`, `value`, and `records` remain invalid terminal tokens.
- `steps` after `run`/`par` Steps reports `runs, call` as allowed.
- Model and tool Steps report only `call`; agent, human, and value Steps report
  no available child or projector name.
- `tree` on any Step and `call` on any Run are invalid projectors. The rejected
  `model-call` spelling is an invalid subject token, not a compatibility alias.
- Valid empty relations and one-node Run-tree/container-call projections
  succeed.
- Structural corruption fails a Run tree or container-Step call without partial
  Human or JSON output and remains isolated from separately requested primitive
  views.
- An unresolvable error Pointer is reported in Human output and retained raw in
  JSON; it is not an ownership-structure failure.
- Help calls `tree` a Run structural snapshot and `call` a Step-owned historical
  call. Neither is described as event replay.

## Design Touchpoints

- `src/toolang/execution/inspection.py`: focused primitive descriptors and pure
  ownership predicates/order keys; this module must not import the tree module.
- `src/toolang/execution/trees.py`: typed tree snapshot/nodes, ownership index,
  structural validation, semantic sibling ordering, canonical error facts, and
  bottom-up metrics; it imports primitive inspection vocabulary.
- `src/toolang/execution/store.py`: one consistent batched tree read carrying a
  complete selected set of durable records and Controls, plus focused
  transactionally consistent collection/direct-child reads; it returns no tree
  nodes and does not import the tree module. No schema change.
- `src/toolang/execution/schemas.py`: reuse canonical record selection and
  serialization; do not change HTTP `RunDetail` or make it own tree nodes.
- `src/toolang/cli/toolang/commands/inspect.py`: registries, child collections,
  kind-dispatched `call`, a lazy structural handler shared by Run `tree` and
  container-Step `call`, normalized model/tool call rendering, best-effort
  snapshot-local Human error resolution, and tree rendering; ordinary subject
  resolution does not import or call the tree module.
- `src/toolang/cli/common/execution_progress/formatting.py`: reuse only pure
  duration/count/token/cost formatting, never synthesize live events.
- `tests/unit/cli/test_inspect_subject_navigation.py` and
  `test_inspect_rendering.py`: availability, dependency isolation, and Human
  rendering.
- `tests/unit/execution/test_inspection.py` and `test_trees.py`: focused
  descriptors, ownership, validation, ordering, error facts, and metrics.
- `tests/architecture/test_package_boundaries.py`: primitive inspection and the
  store cannot import `toolang.execution.trees`.
- `tests/integration/execution/test_flow_scenarios.py` and
  `test_store_atomicity_scenarios.py`: real nested ownership, snapshot
  consistency, visibility, and structural errors.
- `tests/integration/cli/test_local_core_commands.py`: end-to-end human/JSON
  trees, relations, and reusable pointers.
- `docs/api.md` and `docs/run-step-records.md`: overview-first navigation,
  primitive ownership relations, and the structural-vs-live distinction.

## Acceptance Tests

1. Nested flow/agic history renders each visible Run and Step exactly once;
   every pointer selects the same canonical record in a follow-up command. The
   Human projection is one hierarchical pointer tree: Run labels use
   `<runnable-kind> name`, every Step label starts with its bracketed durable
   Step kind, all type tags occupy the same display width, tree guides and
   pointers share one left-aligned `NODE` column, and activity labels are
   left-aligned in the `ACTIVITY` column immediately to its right.
2. Root-Run trees include the whole subtree; child-Run trees and container-Step
   calls exclude ancestors and siblings. A Run-Step invocation renders as Run
   -> Run Step -> child Run -> child-Run Steps; `run Step call` starts at the
   Run Step and still renders the child Run as a separate node. Primitive
   investigation traverses the same chain through `RUN steps`, `STEP runs`, and
   child `RUN steps` without cross-Run flattening, with exact direct `RUNS`
   counts and physically owned `STEPS` counts at each boundary.
3. Parallel children use logical item order while retaining lane occurrence;
   repeat-body Steps and condition Runs are merged and grouped by iteration;
   missing required coordinates or multiple visible children under a `run`
   Step fail structural validation.
4. Counts, accounting-first/legacy-fallback tokens and cost, completeness,
   approximation, terminal duration, and visibility aggregate without
   double-counting or invented facts; no SQLite-private ordering field is read
   or emitted.
5. Failed, canceled, running, and empty structural projections render
   accurately; Human errors are bounded and best-effort resolved, while JSON
   errors remain canonical and unresolved.
6. Tree snapshots and multi-table primitive views are each internally
   consistent while an active execution writes new records concurrently;
   primitive reads do not expand beyond their requested scope.
7. Existing implicit `fields`/`value` selection, Human Pointer resolution, and
   JSON non-resolution remain compatible. Root `runs`, `THREAD runs`, `RUN
   steps`, `STEP runs`, and `LOOP_STEP steps` expose their defined ownership
   facts and reusable pointers when structural projectors are unregistered,
   importing the tree module is blocked, or the tree builder is replaced with
   a sentinel that fails if called. Model/tool `call` still works with the tree
   import blocked.
8. Only a whole Run advertises and accepts `tree`. Supported whole Steps
   advertise and accept only `call` as their explicit projector: model/tool
   Steps project normalized calls, while `run`/`par`/`loop` Steps project
   Step-rooted structural trees. Run `call`, Step `tree`, and the removed
   `model-call` spelling fail with subject-derived allowed names and perform no
   specialized read.
9. Direct Step relations return only direct canonical records, preserve
   visibility, and succeed empty for eligible Step kinds.
10. Corrupt orphan/cycle/control/occurrence data fails a structural projection
    before output but remains visible through a separately valid focused
    primitive view.
11. Inspection creates no records, provider calls, State loads, or migrations;
   exact event replay stays unavailable.
12. Existing collections, Pointers, and `RUN steps` remain compatible with the
    canonical record schema. Model `call` preserves the former normalized
    `ModelCall` Human/JSON result, tool `call` emits the exact normalized
    `ToolCall` JSON and persisted Human result, and no call projector performs
    live work. Ruff, ty, formatting, and default offline pytest pass.

## Follow-Up Extensions

Separate definitions should follow in this order:

1. Call-result JSON envelopes that combine call, result, status, and error
   without changing the bare `call` contracts defined here.
2. Tree filtering/folding: status, kind, runnable, occurrence, and call filters
   while retaining ancestors and marking omitted nodes.
3. Retry/rerun comparison: reused, replaced, added, and ejected work.
4. Exact timeline: first persist a versioned, ordered, bounded event journal;
   timestamps cannot replace event order.
5. Source/State provenance: recorded State control, module, declaration, and
   source line without presenting current authored state as historical truth.

## Risks

- Users may mistake structure for trace; naming and help must say durable tree.
- Structural projection may accidentally become the common read path;
  dependency tests must enforce that primitive views do not construct or
  validate a tree.
- Metrics can double-count; validate one node graph, then aggregate bottom-up.
- Store insertion is neither logical item order nor event order; sort from
  occurrence coordinates and always display them.
- Error resolution can fail on hidden or corrupt targets; retain the canonical
  Pointer so failure investigation still has evidence.
- Large trees are unbounded initially; later folding must mark incompleteness.
- Synthesizing live events would invent order and deltas; share formatting only.

## Open Questions

None.

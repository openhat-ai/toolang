# Executor Design

This document defines the target executor for the semantic AST in
`toolang.lang.ast`.


## Boundaries

The executor consumes the existing AST directly:

```text
Program.agics  -> AgicDecl[]
Program.flows  -> FlowDecl[]
FlowDecl.stmts -> FlowStmt[]
```

It must not introduce parallel `Plan`, `Runnable`, `Agic`, or `Flow` models.
Named and generated inline agics already live in `Program.agics`; generated
names such as `<agic:12>` are resolved exactly like authored names.

The runtime keeps these existing boundaries:

```text
UptimeContext  stable activation services and stores
RunBinding     accepted run identity, command input, selectors, and snapshot
LiveState      immutable prepared program and cap snapshot
```

`RunBinding.live` is fixed when the run is accepted. Child runs inherit that
same object and never read a newer `UptimeContext.live` value.


## Runtime Values

Toolang values are runtime data, not model messages. `Message` and `Part`
belong to model calls, command input, and response projection.

```python
Shape = Literal["none", "item", "list"]


@dataclass(frozen=True, slots=True)
class Local:
    value: object = None
    shape: Shape = "none"


locals: dict[str, Local]
```

Shapes describe flow cardinality, not Toolang value types:

```text
none  no value
item  one value, including one value whose type is T[] or Part[]
list  an ordered flow collection, including an empty collection
```

`_` is the primary local:

- accepted run input initializes `_`
- an unqualified statement writes `_`
- a completed run returns `_`

Named parameters and `let` bindings use other local names. There is no
`Locals`, `Cell`, or `Frame` class.

`Local` contains no durable references. `InputRef` and `OutputRef` belong to
the persistence projection maintained by `PersistSink`, not to executor data.


## Atomic Local Updates

A step receives a read-only snapshot of the current locals and returns one
`Local`. It must not mutate the supplied dictionary.

```text
snapshot = dict(locals)
result = execute step against snapshot
if the step succeeds:
  update locals according to stmt.binding
```

Bindings are:

```text
"_"    replace the primary local
name   replace one named local
None   discard the result
```

Failed or canceled steps leave locals unchanged. A nested control step uses a
private working copy; only the control step's final result can be committed to
its parent. Parallel branches each receive the same initial snapshot and can
never observe sibling updates.


## Snapshot Rule

External requests capture `LiveState` when accepted. Queued requests retain
that snapshot until they run.

Child runs are accepted immediately with their parent's snapshot:

```python
child = bind_run_request(context, request, live=parent.live)
```

A live update therefore affects only requests accepted later. It cannot
change a running run, an accepted queued run, or any descendant of either.


## Ownership

Request handling allocates ids and emits command events. It does not write
execution records.

```text
external queue  run_waiting, run_starting, run_steering, run_stopping
executor        child run_starting, run_begin, step/part events, run_end
```

The executor emits each trace fact once. Sinks consume that stream:

```text
Executor -> TraceEventHandler -> PersistSink
                             -> ReplySink(s)
                             -> resource event bus
```

- `PersistSink` is the only component that creates or updates execution
  records.
- `ReplySink` implementations produce buffered, streaming, channel, and UI
  replies.
- The executor never writes SQL, record objects, SSE payloads, or terminal
  output directly.


## Executor Shape

```python
class Executor:
    def __init__(
        self,
        context: UptimeContext,
        *,
        emit: TraceEventHandler,
        consume_commands: Callable[[str, CommandKind], Sequence[CommandRecord]],
        load_loop: Callable[[str], AgentLoop],
        stream: bool,
    ) -> None: ...

    async def run(
        self,
        binding: RunBinding,
        executable: AgicDecl | FlowDecl,
        *,
        parent: StepPath | None = None,
    ) -> Local: ...

    async def _execute_agic(
        self,
        binding: RunBinding,
        agic: AgicDecl,
        locals: dict[str, Local],
    ) -> Local: ...

    async def _execute_flow(
        self,
        binding: RunBinding,
        flow: FlowDecl,
        locals: dict[str, Local],
    ) -> Local: ...

    async def _execute_stmt(
        self,
        binding: RunBinding,
        locals: dict[str, Local],
        *,
        parent: StepPath,
        index: int,
        stmt: FlowStmt,
        placement: Mapping[str, object] | None = None,
    ) -> Local: ...
```

`execute_run()` remains the outer orchestration entry point. It resolves the
accepted executable, creates sinks and `Executor`, calls `Executor.run()`, and
converts the returned `Local` into the caller-facing outcome.


## Run Execution

```text
Executor.run(binding, executable, parent=None)
  locals = {"_": Local()}
  initialize `_` and named locals from the accepted command
  emit run_begin input=InputRef(cmd=0)

  try
    if executable is AgicDecl
      result = _execute_agic(binding, executable, locals)
      locals["_"] = result
    else
      result = _execute_flow(binding, executable, locals)
  on cancellation
    emit run_end[canceled] with the consumed stop InputRef when present
    re-raise
  on error
    emit run_end[failed] from the current primary local
    re-raise
  otherwise
    emit run_end[finished] from the current primary local

  return locals["_"]
```

The top-level executable is not wrapped in a synthetic step. A child
executable runs recursively inside the statement step that called it, and the
child run's `parent` is that `StepPath`.


## Agic Execution

`_execute_agic()` reuses the model/tool loop. It builds model input from the
run's immutable snapshot and locals, then lets the loop emit dynamic `model`
and `tool` steps.

The final assistant parts are decoded at the agic boundary:

```text
assistant Message.parts
  -> decode according to AgicDecl.output
  -> Local(value, shape=item)
```

Without a declared output type, the agic returns its normal assistant content.
Structured output types select and validate the model's structured response
mode. Flow statements never parse model text independently.


## Flow Execution

```text
_execute_flow(binding, flow, locals)
  for index, stmt in enumerate(flow.stmts)  # index starts at 0
    result = _execute_stmt(
      binding,
      dict(locals),
      parent=binding.run_id,
      index=index,
      stmt=stmt,
    )
    update locals in place according to stmt.binding

  return locals["_"]
```

The update after `_execute_stmt()` is intentionally visible. It is the only
point where one flow statement changes the locals observed by the next
statement.


## Statement Execution

`_execute_stmt()` owns common tracing and dispatch:

```text
path  = child path(parent, index)
basis = locals.get("_", Local())

emit step_begin(
  path,
  kind,
  input=explicit inputs and consumed command refs,
  context={binding, reads, placement, source},
)

try
  mid = execute the concrete statement kind
  result = reshape(stmt, basis, mid)
except cancellation
  emit step_end[canceled]
  re-raise
except error
  emit step_end[failed]
  re-raise
else
  emit step_end[finished] with result and detail
  return result
```

The three values have distinct roles:

- `basis` is the primary local before the statement.
- `mid` is the direct operation result.
- `result` is the value after the statement-specific reshape.

The wrapper does not update locals. Its caller applies the returned result only
after the terminal step event has been emitted successfully.


## Statement Mapping

```text
AST node       step kind  operation                            reshape
RunStmt        run        one child agic or flow               none
SeekStmt       agent      one child run on another agent       none
AskStmt        human      wait for human input                 none
ScatterStmt    run        one child run                        unfold
StormStmt      par        N independent child runs             list
GatherStmt     run        one child run                        fold
SettleStmt     loop       sequential reducer child runs        item
MapStmt        par        one child run per item                list
KeepStmt       par/system predicates or positional selection   filter keep
DropStmt       par/system predicates or positional selection   filter drop
RankStmt       par        one score per item                    stable rank
RepeatStmt     loop       repeated nested statements           last result
LetStmt        system     authored value                       none
```

The step kinds are:

```text
run | agent | human | model | tool | par | loop | system
```

`unfold`, `fold`, filtering, and ranking are executor reshapes, not step kinds.
A flow body is already sequential and does not require a synthetic `seq` step.


## Reshapes

```text
none
  result = mid

unfold
  require one produced array-like value
  scatter additionally requires exactly its declared item count
  result = Local(items, shape=list)

fold
  require one produced value
  result = Local(value, shape=item)

filter keep/drop
  require basis.shape=list
  select basis items using positional rules or mid predicates
  result = Local(selected, shape=list)

rank
  require basis.shape=list
  stable-sort basis items by mid scores, highest first
  optionally retain top N or bottom N
  result = Local(selected, shape=list)
```

Keep, drop, and rank use both `basis` and `mid`: the basis holds the items,
while the mid value holds predicates or scores.


## Child Runs

Runnable statements share one helper:

```python
async def _run_runnable(
    self,
    parent: RunBinding,
    locals: Mapping[str, Local],
    step: StepPath,
    runnable: str,
    placement: Mapping[str, object] | None,
) -> Local: ...
```

It resolves the runnable from `parent.live.program`, allocates a child run id,
emits `run_starting` with `parent=step`, builds a child `RunBinding` with
`live=parent.live`, and calls `Executor.run()` recursively. The event stream,
not this helper, causes the child records to be persisted.

The child owns a new locals dictionary. Its `_` is initialized from the call
input, and matching named parameters are copied as values. Child updates never
write into parent locals.


## Parallel Statements

`StormStmt`, `MapStmt`, predicate `KeepStmt`/`DropStmt`, and `RankStmt` use one
bounded ordered execution mechanism.

The outer `par` step has one deterministic path. Every spawned child run points
back to that path:

```text
outer step:         run_abc123/2
child run parent:   run_abc123/2
child run steps:    run_child1/0, run_child2/0, ...
```

Each worker receives a copy of the same parent snapshot with `_` replaced by
its item. Completion order does not affect paths or result order. Placement is
trace context rather than executor state:

```text
item: {index, count}
lane: {index, count}
```

If one branch fails, the executor cancels and awaits every unfinished sibling
before emitting the outer `step_end`. A parent step or run therefore never
ends while one of its visible descendants is still live.


## Repeat And Settle

`RepeatStmt` and `SettleStmt` each emit one `loop` step. Their child statements
run against a private working copy of parent locals. Successful children update
that copy in order; the parent sees only the loop's final returned `Local`.

Repeat child context records the iteration and body position. It returns the
last completed child result, or `Local()` if no child ran. An `until` agic runs
after each completed body iteration.

Settle requires `shape=list`. It invokes its reducer once per item in order.
Each child receives the item as `_` and the previous result as `accumulator`;
the final accumulator is the loop result.


## Commands And Cancellation

The executor consumes accepted commands at agic-loop and flow-statement
checkpoints. Command acceptance and application are separate trace facts, as
defined in [run-step-records.md](./run-step-records.md).

Command application is expressed through existing event inputs: `run_begin`
references start, `step_begin` references steer, and `run_end` references the
stop command that caused cancellation. A terminal `run_end` causes any
unapplied commands to be projected as canceled.

Stopping cancels the active task. During unwinding, the executor emits terminal
child-step and child-run events before the parent `run_end[canceled]`. The
executor is the sole owner of terminal `run_end` events.

`now` and `next_step` commands are consumed at the next statement or dynamic
model/tool step. `next_call` waits until the next statement that can call a
runnable, agent, human, model, or tool. The consuming `step_begin` includes a
steer command's `InputRef`; a consumed stop command is referenced by
`run_end.input`. Without that edge the command remains pending and cannot
affect locals or model input.


## Persistence Projection

`PersistSink` owns record and lineage state derived from trace events. In
particular, it maps run locals to their latest `InputRef` or `OutputRef` using
step context such as `binding` and `reads`.

```text
start command input -> `_` provenance is InputRef(cmd=0)
successful step     -> selected binding provenance is OutputRef(step=path)
discarded result    -> no binding provenance changes
run end             -> RunRecord.output uses `_` provenance when it is an OutputRef
```

This projection is deliberately separate from executor locals. A new reply or
persistence sink can consume the same semantic trace without changing step
execution.


## Deferred Statements

Target-agent resolution for `SeekStmt` and the suspension protocol for
`AskStmt` remain outside this executor version.

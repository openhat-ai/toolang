# Execution Presentation

Script and Chat share one projection of execution progress. Presentation adds
no execution events, records, identities, or lifecycle states. Historical
inspection reads durable records rather than reconstructing live progress.

## Projection model

```text
RunEvent -> ProgressProjector -> ProgressUpdate -> surface presenter
```

The projector normalizes Flow statements and runtime Tool Steps into a common
operation: action, target, Run scope, and tool identity. Headers, lanes, and
terminal rows consume this projection. Tool identity and status remain separate
from saved summary text; renderers never infer identity from prose or invoke
plugins to recreate summaries.

`ProgressUpdate.committed` contains append-only fragments in event order.
`ProgressUpdate.live` is the complete replaceable tail. A Step may commit its
header and completed output while retaining unfinished activity in live state.
Script and Chat share content, ownership, aggregates, and errors; their presenters
own wrapping, scrollback, and live-area replacement. [Terminal UI](terminal-ui.md)
owns geometry, palettes, Markdown, and TTY mechanics.

The root contains projected Steps and one terminal footer. Child Runs normally
appear through their Steps; dynamic calls add a caller-owned divider around
that content. Script adds its root context header; Chat uses its control bar.

## Invocation and ownership

Run, exec, async run, spawn, and await use one action/target vocabulary while
preserving their distinct execution boundaries:

| Operation | Presentation boundary |
| --- | --- |
| Synchronous run | Caller Step remains open through child completion. |
| Async run | Launch Step ends at admission; background activity retains a separate scope. |
| Await | Blocking Step reports the result without replaying or reparenting the target. |
| Spawn | Admission yields an independent root and its own progress. |
| Exec | Handoff boundary precedes the replacement runnable's first Step in the same Run. |

Background metrics aggregate once into their owning Run, including after the
launch Step closes. Await adds no second charge. Later caller Steps never adopt
the background tree. Independent spawned roots aggregate separately.

## Root context

Script commits one dim context header to stderr at root `RunBegin`, identifying
the resolved runnable and initial model/reasoning setting. Unnamed entries appear
as `agic:` or `flow:`. Child Runs may select other models; they do not repeat
the header. Failures before admission produce no execution header.

The header remains in scrollback. Non-TTY output retains its content without
ANSI; quiet mode suppresses it. [Scripts](scripts.md) owns result/output selection,
and [Chat](chat.md#control-bars-and-status) owns session and run controls.

## Step content

Headers describe intent, live rows describe activity, and terminal rows describe
actual outcomes. Flow uses an authored doc comment when present, otherwise a
short description derived from the operation, target, bindings, and lane limit.
Those bindings describe Flow locals, not persistence. A direct single-Run Step
retains the child's trace and output without a duplicate success wrapper;
direct values render as values rather than invented shape summaries.

Model text streams through the shared Markdown renderer. Concatenated deltas
must be an exact prefix of the authoritative Part closure, and successful Step
closure must contain that completed Part. Started Parts, Steps, and Runs close
before their owner. Contract violations become presentation errors rather than
being repaired by the renderer.

Reasoning and native tool-call Parts do not become human output. The following
Tool Step owns visible call activity; a successful tool-call-only Model Step
commits no terminal row. Other displayable Parts remain visible. Tool results,
including JSON, stdout, and stderr, stay in records and model messages. Tool
progress uses persisted masked summaries with a separately derived toolset
prefix; older records fall back to the recorded tool name.

Markers distinguish Model/Flow (`•`), ordinary tools (`›`), runtime guidance
helpers (`✧`), dynamic Run boundaries (`┌`/`└`), and the root footer (`▪︎`).
Failures use red, cancellation yellow, and headers/facts dim text. Model and
Flow results use the default foreground; finished Tool summaries are dim.
Model/Tool rows omit per-Step model, exit-code, usage, and cost inventories.
Compact additionally reports its running elapsed time and final duration.

## Dynamic calls and handoffs

A model-requested Run opens its divider at the child's `RunBegin`, accumulates
child metrics at `RunEnd`, and closes at the enclosing `StepEnd`. The header
identifies the resolved runnable; the footer reports the enclosing operation's
status, complete direct child Run ID, and child-tree facts. Failure before
acceptance has no invented child identity or metrics.

Dynamic boundaries stay flat while the child retains its own Agic or numbered
Flow grammar. Calls inside parallel lanes remain lane activity. A confirmed
exec instead commits one handoff boundary using its saved summary; native Flow
and older records use the target caption. Exec creates no child Run boundary.

## Parallel work and loops

Parallel Steps retain an aggregate live row and the latest activity for each
observed lane. Lane rows truncate rather than wrap. Counts span lane reuse:
while running they distinguish failed, active, and succeeded items; terminal
counts also distinguish canceled and never-started items. Canceling children
remain active until they terminate. Only the success count carries the known
total; no total is invented before work starts.

Successful completion clears lanes and leaves the operation's actual result.
Failure preserves causal errors in failed lanes and the parallel Step's distinct
boundary error. Cancellation uses its own counts without inventing a failure or
labeling unstarted work as canceled.

Repeat and Reduce expose iteration boundaries and child activity in authored
order. Repeat conditions belong to the owning loop and execute as child Runs,
not synthetic tool Steps. Terminal summaries report completed passes and the
actual cause: exhaustion, condition, interruption, cancellation, or failure.
A condition may terminate with zero completed passes while retaining prefix
work. Historical Human and JSON trees preserve the same order.

## Errors and terminal facts

Each causal error appears once at its owner: the Step, failed lane, or root Run
when no Step owns it. A parallel boundary may add its distinct local error.
Propagated parent pointers remain silent. A malformed presentation stream clears
live state and commits one root-owned error.

A Flow Step owning child execution may append a footer with subtree facts and
its complete StepPath. Model, Tool, and direct-value Steps define no such facts.
Dynamic calls identify their direct child Run instead of a StepPath. Undefined
facts are omitted, and identity alone does not create a footer.

Script and Chat finish each root with one footer containing its ID, terminal
status, duration, and whole-tree facts. Errors before `RunBegin` belong to command
submission; later failures belong to execution progress and its footer.
Invocation counts and usage include owned children once; independent roots stay
separate. [Statistics](execution-statistics.md) owns accounting meaning,
including the distinction between unknown quantities and zero.

Token facts distinguish input/cache and output/reasoning; output already includes
reasoning. Cost preserves exact versus estimated attribution. Facts and complete
identities wrap when needed rather than being silently truncated.

## Operational progress

Preparation, setup discovery, and server lifecycle work use `ProgressEvent`,
separately from `RunEvent`. One segment lasts until terminal control passes to
Run output, logs, a prompt, or a command result; later cleanup opens a new segment.

A TTY shows a transient dim activity row and clears successful work. Non-TTY
stderr records material actions and outcomes immediately as append-only text.
Failures retain the stage, reason, and applicable recovery/log information.
Stable command results own agent identity; operational rows describe the work.

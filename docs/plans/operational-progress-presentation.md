# Define Operational Progress Presentation

## Status

Feature definition proposed for human confirmation. This document refines the
presentation and terminal-handoff sections of
[Unify Operational Progress](operational-progress-lifecycle.md). It does not
change the approved `ProgressEvent` contract or the Run grammar. Pull request
#375 is the current implementation under review and must be reconciled with this
definition before the operational-progress feature is complete.

## Verified Current Behavior

- Run presentation has a terminal-independent projector, exact row and marker
  grammar, committed-versus-live ownership, display-cell width rules, spacing,
  error ownership, and matching Script and Chat tests.
- Pull request #375 gives `prepare` a legacy item list and summary, while
  `setup` and `runtime` use a separate current-activity line. Selection also
  depends on ID and label prefixes such as `cap:` and `Prepare `.
- Runtime commands close source progress and construct another presenter for
  launch. Temporary execution constructs a third presenter for cleanup. This
  resets elapsed time and can make a contiguous operation flicker.
- Initial guest `setup.load` and `setup.discover` are currently observed before
  `runtime.create` closes, although setup occurs after the AgentServer process
  starts and belongs inside the approved `runtime.start` boundary.
- Operational output does not use Run's 120-cell maximum, display-cell
  measurement, or wrapping rules. Details are bounded by Python character
  count instead.
- Non-TTY cleanup can follow a root Run footer without a defined section gap.
  Failure progress and the outer CLI diagnostic can also report the same cause
  twice.
- Current tests cover isolated activity lines, but not complete
  prepare/setup/runtime transcripts, narrow output, segment handoffs, or the
  equivalence of agent-first and command-first forms.

## Goal

Give `prepare`, `setup`, and `runtime` one exact operational presentation that
is visually compatible with Run output without pretending that environment
work is execution. A complete command transcript must remain calm, ordered, and
unambiguous when operational work precedes a Run, hands off to foreground logs,
or resumes for cleanup.

Success means:

- all three kinds use one row grammar and one presenter state model;
- TTY and non-TTY output preserve the same material activity ordering, subject
  to the defined suppression of sub-threshold TTY work;
- successful operational work leaves no redundant summary before the command's
  actual result;
- Run rows, operational rows, warnings, diagnostics, and logs never compete for
  terminal ownership; and
- full-transcript tests specify every seam.

## Relationship to Run Progress

Operational and Run progress share presentation principles, not events or
projectors:

| Concern | Operational progress | Run progress |
| --- | --- | --- |
| Source | `ProgressEvent` | `RunEvent` |
| Meaning | environment work | agent execution |
| Active marker | animated spinner | `•` activity rows |
| Stable running marker | `…` | committed `•` content |
| Terminal closure | command result or one failure block | `∎` root Run footer |
| TTY mechanics | one transient Rich live region | committed blocks plus one live region |
| Non-TTY mechanics | append-only activity starts | append-only committed blocks |

There is no operational Run header or footer and no adapter between the two
event families. `CliProgress` remains the only public operational presenter.
Implementation may use private state, but must not add public presenter or
projector concepts per kind.

## One Operational Segment

One `CliProgress` instance owns each uninterrupted interval of operational
terminal use. Prepare, setup, and launch runtime events before execution belong
to the same segment even when their kinds change. The presenter closes only
when control passes to a Run presenter, prompt UI, foreground logs, a stable
command result, or an error diagnostic.

Cleanup after a Run or foreground session is a new segment because another
owner used the terminal in between. A segment has one elapsed clock, one Rich
live region, and one ordered non-TTY stream. Call sites must not close and
recreate a presenter merely because the event kind changes.

Events retain the approved lifecycle rules:

- `pending` contributes only to aggregate facts and is never a visible row;
- `running` starts or materially updates the visible activity;
- `ok` and `skipped` close the activity without committing a success row;
- `failed` closes live state and transfers stable output to the single failure
  block; and
- cached work that emits only `ok` or `skipped` remains silent.

For concurrent prepare items, the most recently changed running item is the
visible activity. When it closes, the most recently changed remaining running
item becomes visible. Aggregate facts carry overall progress, so concurrency
does not grow the live region vertically.

## Row Grammar

The TTY live row is:

```text
SPINNER SUBJECT [· SANDBOX] · ACTIVITY [· DETAIL] [· FACTS] · ELAPSED
```

Examples are:

```text
⠋ agent eve · Fetching skill · browser · 2/5 caps · 1.2s
⠙ agent eve · Loading setup · 180ms
⠹ agent eve · Discovering models · 2.1s
⠼ agent eve · docker:python:3.13-slim · Installing Toolang · package index · 8.4s
⠴ agent eve · docker:python:3.13-slim · Waiting for agent API · http://localhost:7001 · 9.1s
```

The non-TTY committed activity row is the same semantic row without animation
or elapsed time:

```text
… SUBJECT [· SANDBOX] · ACTIVITY [· DETAIL] [· FACTS]
```

For example:

```text
… agent eve · Fetching skill · browser · 2/5 caps
… agent eve · docker:python:3.13-slim · Installing Toolang · package index
```

Exact duplicate running events produce no output. A change to activity, bounded
detail, selected concurrent item, or aggregate facts is material and appends a
new non-TTY row. Terminal success is not material output because the following
command result, inspection view, Run, or foreground session proves completion.

`SUBJECT` is `agent NAME` for agent-scoped commands. Before a remote agent name
is known it is `agent SOURCE`; root-only operations may use `Toolang`. Raw
progress IDs are never displayed. `SANDBOX` is shown when the segment owns a
selected runtime, including guest setup nested inside launch. It is omitted for
embedded host setup and unrelated preparation.

`ACTIVITY` is a short present-participle phrase. Producers may specialize the
object, but use this vocabulary and tone:

| Kind and stage | Canonical activities |
| --- | --- |
| `prepare.resolve` | `Resolving agent`, `Resolving KIND` |
| `prepare.fetch` | `Fetching agent`, `Fetching KIND` |
| `prepare.materialize` | `Preparing agent`, `Updating KIND`, `Preparing caps` |
| `setup.load` | `Loading setup` |
| `setup.discover` | `Discovering models` |
| `runtime.create` | `Preparing sandbox`, `Fetching image`, `Installing Toolang`, `Checking Toolang` |
| `runtime.start` | `Starting agent`, `Waiting for agent API` |
| `runtime.stop` | `Stopping agent` |
| `runtime.destroy` | `Removing runtime` |

Labels do not contain status words, trailing punctuation, agent identity, or
sandbox identity. They may identify a third-party implementation action when
the phrase remains concise and follows the same active-verb form. Installer
commands, environment values, secrets, and raw logs are never labels or
details.

Prepare shows `N/T caps` only when a total is known and greater than one. `N`
counts `ok` and `skipped` items. Setup and runtime add no invented counts.
Expected Ollama or llama.cpp absence, connection refusal, or timeout produces
an offline provider snapshot and a successful aggregate discovery; it is not a
red progress failure. An unexpected catalog exception fails
`setup.discover`. The following inspection output remains authoritative for
provider status and model counts.

## Width, Wrapping, and Style

Operational output uses the same available width as Run output:

```text
min(attached terminal width, TOOLANG_PROGRESS_MAX_WIDTH)
```

The configured maximum is used for non-TTY output. Measurement and wrapping use
display cells. Logical fields wrap at ` · ` boundaries with a two-cell hanging
indent; a single overlong field folds by display cells. Details are whitespace
normalized and bounded to 80 display cells before layout. Subject, activity,
status, and aggregate facts are not silently dropped.

The TTY live row is dim so it remains secondary to execution and command
results. A stable failure marker and its activity use normal-intensity red;
continuation labels are dim and the reason uses normal intensity. Success green
is not used. Non-TTY output contains UTF-8 text but no ANSI, cursor movement, or
carriage-return rewriting.

TTY live presentation is delayed for 150 milliseconds to avoid flashing for
cache hits and fast local work. It never delays the operation itself and has no
minimum hold time. Non-TTY rows are emitted immediately. Spinner refresh is
presentation-only and never causes new semantic rows.

## Runtime and Setup Ordering

The normal guest launch sequence is:

```text
runtime.create: prepare sandbox, fetch image, install and check Toolang -> ok
runtime.start:  start AgentServer -> running
  setup.load:       load initial setup -> terminal
  setup.discover:   discover initial models -> terminal
runtime.start:  wait for API and identity -> ok
```

Initial guest setup is the only permitted cross-kind nesting. While a setup
stage is running, it is the visible leaf activity. When it closes, the still
running `runtime.start` activity resumes. Setup does not open a second live
region. Stages for one `(kind, id)` never overlap, and `runtime.create` must be
terminal before `runtime.start` begins.

Embedded execution has `prepare -> setup -> Run` and no synthetic host runtime
stages. Attaching to an already running AgentServer has no operational segment.
Stop and destroy are sequential stages in a cleanup segment after foreground
or Run ownership ends.

## Stable Failure Grammar

One operational failure produces one block:

```text
× agent eve · docker:python:3.13-slim · Installing Toolang failed
  Stage: runtime.create
  Reason: Could not install Toolang from the package index.
  Fix: Check network access or use --dev PATH with a compatible wheel.
  Log: ~/.toolang/agents/eve/.runtime/agent.log
```

The first line follows the operational row order and replaces the running
marker with `×`. Continuations use exactly the applicable fields above. `Stage`
and `Reason` are mandatory; `Fix` and `Log` are conditional. The responsible
command and presenter compose this block once. Typer, Click, sandbox logs, and
outer exception handlers must not repeat the same cause as another top-level
error. An error before any operational event continues through the normal CLI
error path.

Interruption is not a failed activity. It clears live state, transfers terminal
ownership to cleanup, and preserves the command's existing exit-130 message
policy. A cleanup failure uses the normal failure block and retains recovery
identity.

## Segment Handoffs

Spacing is semantic and independent of TTY capability:

| Handoff | Rule |
| --- | --- |
| prepare -> setup -> runtime | same segment; no blank row or presenter reset |
| operational -> warning -> operational | warning is committed once; live state is suspended and redrawn; no surrounding blank row |
| operational -> command result, table, or ready line | live clears; result follows immediately with no added blank row |
| operational -> Run | live clears; the Run presenter owns exactly one leading blank row before its first committed block |
| Run footer -> cleanup | cleanup starts only after the footer commits; non-TTY adds exactly one leading blank row, while successful TTY cleanup remains transient |
| operational -> foreground logs | live clears, the ready line commits, then log following starts; no log may appear above or inside live progress |
| operational -> Chat TUI | live clears before prompt_toolkit starts; no operational live state exists while the TUI owns the terminal |

An operational presenter never prints a trailing blank row. The later owner is
responsible for a required section gap. A stable warning or failure temporarily
clears the live region before committing and never relies on Rich stdio
redirection.

### Complete Non-TTY Temporary Run

```text
… agent eve · Fetching skill · browser · 2/5 caps
… agent eve · docker:python:3.13-slim · Preparing sandbox
… agent eve · docker:python:3.13-slim · Installing Toolang · package index
… agent eve · docker:python:3.13-slim · Loading setup
… agent eve · docker:python:3.13-slim · Discovering models
… agent eve · docker:python:3.13-slim · Waiting for agent API · http://localhost:7001

• Thinking...
• Finished the report.

∎ run_abc123 succeeded                                      9.4s · 1 model call

… agent eve · docker:python:3.13-slim · Stopping agent
… agent eve · docker:python:3.13-slim · Removing runtime
```

TTY displays the same activity sequence in one transient row, then leaves only
the Run transcript. Successful cleanup is transient, so the Run footer remains
the final stable line.

## Command Results

Operational progress does not replace stable command outcomes. Existing concise
outcomes remain the closure for lifecycle commands:

```text
Running agent eve: http://localhost:7001 (Ctrl+C to stop)
Started agent eve: http://localhost:7001
Stopped agent eve
```

Clone, cap mutation, and inspection commands likewise keep their existing
result line or view. They do not gain an additional `Prepared`, `Loaded`, or
`Completed` progress summary.

## Implementation Touchpoints

- `src/toolang/cli/common/progress.py`: replace the prepare-specific and
  operational-specific render branches with one segment state model; use
  shared display-cell width, wrapping, delayed live reveal, activity selection,
  and failure rendering.
- Runtime, Script, Chat, clone, cap, and inspection command orchestration: pass
  one presenter through contiguous pre-execution work, close it at the defined
  handoff, and request a new segment only for cleanup.
- `src/toolang/up/sandbox.py` and launch-token observation: close
  `runtime.create` before starting AgentServer, open `runtime.start` before
  initial setup, and resume its readiness activity after setup.
- Prepare producers: use stable IDs and stage semantics rather than label
  prefixes to drive aggregation; normalize running labels to the activity
  vocabulary.
- Setup and sandbox plugins: retain semantic events and bounded details; do not
  preformat rows or failures.
- Operational, command, integration, PTY, and live Docker tests: assert complete
  transcripts and ownership seams rather than private renderer methods alone.
- `docs/execution-presentation.md` and plugin documentation: add the normative
  operational grammar and plugin label/detail constraints.

No `RunEvent`, `ProgressProjector`, Script/Chat Run grammar, sandbox selection,
readiness authority, environment exposure, or recovery-record change is in
scope. Dotted progress phases and the removed presenter names receive no
compatibility path.

## Acceptance Tests

1. Prepare, setup, and runtime events render through the same field order,
   width logic, style rules, and presenter instance within one segment.
2. TTY cache hits and operations completing before 150 milliseconds leave no
   live residue or artificial delay; slower work becomes visible by the
   threshold.
3. Non-TTY output is ordered, append-only, ANSI-free, deduplicates exact
   activities, and emits no success rows.
4. Concurrent prepare events select the most recently changed active item and
   maintain accurate `N/T caps` facts without growing multiple live rows.
5. Cached and skipped prepare work leaves no pending or running residue.
6. Setup load and discover use the same grammar; expected Ollama and llama.cpp
   offline or timeout results remain successful discovery, while unexpected
   exceptions produce one `setup.discover` failure block.
7. Runtime create closes before start; initial guest setup temporarily owns the
   live leaf inside start; readiness resumes after setup without a second live
   region.
8. Wide, narrow, and Unicode subjects/details wrap by display cells within the
   same configured maximum as Run output.
9. One failure block contains the qualified stage and reason, conditional fix
   and log, and no duplicated outer or workload error.
10. Prepare-to-setup-to-runtime changes add no blank line, reset, duplicate
    activity, or elapsed-clock restart.
11. Operational-to-Run handoff has exactly one leading Run gap and no residual
    live row; Run projection and footer output remain byte-for-byte unchanged.
12. A root Run footer precedes cleanup. Non-TTY has exactly one intervening
    blank row; successful TTY cleanup is transient and leaves the footer as the
    final stable line.
13. Ready lines precede all foreground logs, Ctrl+C stops log following before
    cleanup presentation, and no log corrupts a live row.
14. Command results and inspection views follow successful operational work
    directly and gain no redundant success summary.
15. Agent-first and command-first forms, embedded host execution, attached
    execution, temporary guest execution, Linux/macOS terminals, and Docker
    controlled from Linux/macOS/WSL2 produce the defined semantics.
16. Default verification and opt-in Docker transcript tests pass.

## Risks and Open Questions

- Delayed TTY reveal needs an event-driven timer that cannot outlive a closed
  presenter or delay command completion.
- Cross-kind setup nesting requires explicit active-state restoration; choosing
  only the last received event will leave a terminal setup row visible or hide
  readiness.
- Full transcript tests must normalize elapsed values and spinner frames without
  weakening ordering, spacing, or ownership assertions.

There are no open product questions. Human confirmation is required before
implementation.

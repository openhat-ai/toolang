# Execution Presentation

Script and Chat share one presentation over canonical [Run events](events.md).
It adds no
execution identities, records or lifecycle states. Durable inspection may reuse
its vocabulary but cannot reconstruct live event interleaving.

## Projection and ownership

```text
ordered RunEvents -> shared projector -> committed fragments + live snapshot
                                           |                    |
                                      append once          replace in place
```

`ProgressUpdate.committed` contains stable fragments in event order; `live` is
the complete replaceable tail. A Step may commit its header and stable Markdown
while retaining an unfinished block. Part closure commits the remaining tail;
Step closure never repeats output. Presenters own terminal mechanics, not another
semantic projection. Script uses Rich Live; prompt_toolkit owns Chat's live area.

A root has Steps and one footer. Child Runs show their Steps without synthetic
Run headers, except for the enclosing dynamic-call boundary described below.
Parallel children are summarized by their owning operation rather than appended
in completion order. Chat clips only the live viewport; committed scrollback
retains complete content. Restored history comes from records, not fabricated
events. [Chat](chat.md) owns submission, queue and recovery state transitions.

## Operational progress

Preparation/discovery/server work uses `ProgressEvent`, separate from RunEvents.
One `CliProgress` segment ends when control passes to run output, logs, a prompt
or a result. Cleanup opens another segment. Producers supply complete verb-first
sentences; active work ends in `...`, success uses simple past.

A TTY delays a dim transient row by 150 ms, adds elapsed time after one second,
and clears successful work without a summary. Non-TTY writes material stages and
outcomes immediately as plain append-only stderr, without elapsed time. Failures
retain the qualified stage/reason and relevant fix/log information. Progress is
presentation-only; it cannot decide readiness or lifecycle ownership.

## Geometry and styles

Both surfaces default to at most 120 display cells, bounded by TTY width.
`TOOLANG_PROGRESS_MAX_WIDTH` accepts a positive integer; non-TTY uses that width.
Markers start at column zero and continuations align after the marker. Wrapping
uses display cells, including wide characters. Escape terminal controls in
untrusted captions. Consecutive structural gaps coalesce rather than doubling.

| Marker | Meaning |
| --- | --- |
| `•` | Model and flow activity/output |
| `›` | Ordinary tool |
| `✧` | Runtime pick/compact/honor helper |
| `┌` / `└` | Dynamic child-call opening/closing boundary |
| `-` | Confirmed execute transfer |
| `∎` | Complete root Run footer |
| `·` | Separator within facts, never a Step marker |

Model/flow output uses normal foreground; headers/facts are dim. Active tool
summaries are normal, completed ones dim. Failure is red, cancellation yellow;
success is not green. Binding effects and control decisions are not extra
progress rows. Model/tool Steps do not display per-Step usage or IDs; compact
alone shows its elapsed time and duration.

## Run and flow boundaries

Script emits one persistent dim context header at root RunBegin, with `‣`, the
resolved runnable and initial model/reasoning. Missing model reads `model
unspecified`. Narrow layouts wrap without losing identity. Pre-acceptance failure
has no Run header; child Runs never repeat it. Chat uses its root input bar instead.

A flow header uses its authored doc comment, otherwise a sentence generated from
the typed AST. Names and inline `<adhoc:LINE>` identities remain exact. Lane and
binding suffixes describe concurrency, named save or discard. Direct values show
the value; single-child Steps preserve the child's leaf trace without a synthetic
success row. Headers describe intent; terminal summaries describe actual results.

Dynamic model calls use flat `┌ Run KIND:NAME` / `└ FACTS STATUS CHILD_RUN_ID`
boundaries. For scheduled `_toolang/run`, retain the receipt's child identity
past Tool Step closure and open at child RunBegin. Parent references express
causality, not overlapping lifetimes. Failure before acceptance invents no child
ID or metrics; cancellation before dispatch does not invent RunBegin.

The closing identity is the complete direct child Run, not a StepPath. Nested
calls do not add indentation. Rules shorten before fields wrap; identities never
truncate. A confirmed execute transfer prints `- Execute KIND:NAME` before the
target's first Step without inventing a child Run. Parallel lanes reduce these
boundaries to their single physical lane row.

## Leaf output and errors

A model starts with `• Thinking`; its text replaces that live row and progressively
commits as Markdown. Tool-call Parts themselves are hidden: the following Tool
Step owns activity. Tool-call-only model output commits no terminal row, while
retaining execution status and metrics. Reasoning/native metadata is not ordinary
human output. Tool results, stdout/stderr and JSON remain inspectable records,
not progress blocks.

Tools use persisted begin/end summaries. The optional plugin summary hook receives
isolated data with sensitive arguments masked; it performs no I/O or styling.
Missing/empty/failed hooks use generic wording. Presenters never call a plugin to
reconstruct past summaries. Running/succeeded/canceled tools take one physical
line; failed tools add an indented diagnostic. Long tool lines truncate; model
Markdown wraps, including tables, lists and fenced code.

```text
› Searching for “Toolang plugin protocol”
› Searched for “Toolang plugin protocol”
✧ Loaded rules: repo://src/AGENTS.md
```

These illustrate alternative live/final states, not duplicate committed rows.
A streamed text delta sequence must be an exact prefix of Part closure, and a
successful Step must contain that completed Part. Started Parts/children must
close before their owner. Violations become presentation contract errors rather
than silently repaired text.

Display each causal error once at its owning Step/lane. Parallel Steps may add a
distinct boundary failure; parent pointer errors remain silent. A concrete Run
error outside a Step gets one root row. Malformed streams clear live state and
report one root-owned error.

## Parallel and loop work

Parallel work keeps one aggregate and one physical row per observed lane. A lane
retains its latest activity until reuse/closure; it truncates rather than wraps.
Running counts are failed, active, succeeded; terminal counts are failed,
canceled, not started, succeeded. Only succeeded includes the known total.
Unstarted work is never labeled canceled, and totals are not invented before
known. A canceling child still counts as active until terminal.

```text
• Running · 1 failed · 2 active · 2/6 succeeded
  0 | #4 | • Thinking
  1 | #5 | › Searching
```

On success, remove lanes and retain the operation result (`Mapped 6 items`,
`Kept 5 of 7 items`, `Sorted 6 items descending`). On failure retain causal failed
lanes and the distinct boundary error, not successful/canceled lane details.

Repeat/settle iterations use centered count dividers, with a left-aligned wrapped
fallback on narrow terminals. Unknown totals show only the current count.
Conditions remain real child Runs; generated names display `<?> Check whether
to break`. Terminal summaries distinguish count exhaustion, condition success,
failure/interruption and cancellation without repeating causal errors.
Automatic compaction hides its internal summary text/children in default progress
while retaining their usage and errors; outer completion waits for publication
and adoption, not merely child success.

## Facts and root completion

Child-owning flow Steps may display a dim footer: duration, counts, usage and
complete StepPath. Facts wrap at group boundaries; the path moves to a separate
right-aligned line rather than truncating. Undefined facts and path-only footers
are omitted. Counts omit zero categories. Direct values and model/tool Steps
have no such footer.

```text
[2] Search the web for each query

• Mapped 6 items
  31s · 6 runs 12 models 8 tools · ↑18.4k ↓5.2k(3.1k) ≈$0.01    run_root.2

∎ run_root succeeded    1m16s · 26 runs 32 models 8 tools · ↑43.8k ↓17.6k(9.2k) ≈$0.01
```

This is schematic output; IDs and metrics are illustrative. Root facts aggregate
the complete tree and appear once. Retry/rerun identify their operation in the
same root footer, without another result line. Duration formatting is shared:
`250ms`, `1s`, `1m0s`, `1h1m1s`; nonpositive values are `0s`. Positive subsecond
values keep milliseconds; other durations round, while live clocks floor first.

Usage is `↑INPUT(CACHE%) ↓OUTPUT(REASONING)`. Output already includes reasoning;
never add it again. Complete cache ratios show a percentage; explicit zero
reasoning shows `(0)`, partial known reasoning adds `+`, unknown reasoning omits
it. Exact zero cost is hidden. Cost uses two decimals, then four when needed;
smaller exact/estimated values use `<$0.0001` / `≲$0.0001`, ordinary estimates `≈$`.

## Surface output and palette

Script progress goes to stderr for TTY and non-TTY. It does not copy root output
to stdout by default. `--out -` emits the result there; `--out PATH` atomically
writes a file. Failure/cancellation writes neither destination. `-q` suppresses
operational/execution progress but leaves actionable errors. Non-TTY is stable
newline-delimited output with no ANSI, cursor motion or partial delta rows.

Chat and Script preserve normal terminal foreground and named ANSI semantic
colors. Code blocks form rectangular Code surfaces; inline code uses a derived
background. Chat retains ANSI identity in both live and committed rendering.
The palette is resolved before keyboard reading/output, never during a live run.

`TOOLANG_COLOR_SCHEME` accepts case-insensitive `dark`/`light` or three explicit
`#RRGGBB` colors in Input, Queue, Code order. Explicit colors bypass discovery;
the third also fills inline code. Otherwise bounded OSC 10/11 queries run only
on the same input/output TTY with no pending input; incomplete/failed queries
fall back to dark. Quiet/non-TTY Script and noninteractive Chat never probe.
`COLORFGBG` is not used. RGB fills do not preserve terminal transparency.

| Scheme | Input | Queue | Code | Inline |
| --- | --- | --- | --- | --- |
| Dark | `#1f1f1f` | `#121212` | `#0b0b0b` | `#151515` |
| Light | `#e3e3e3` | `#f2f2f2` | `#f9f9f9` | `#efefef` |

Detected block backgrounds target contrast 1.05 (1.07 near black); inline uses
1.15 against the terminal background in linear RGB. These are surface contrast
values, not text readability claims. Input/Queue use a foreground-based cap.

## Chat layout

Run/Steer/Quick Command bars preserve complete authored text, with one padding
row above/below and two cells at either side. Root inputs fill terminal width;
Steer and Quick Command bars use bounded output width. Input background covers
controls; cyan marks start/Input and magenta marks Steer/Queue. Root annotations
show submitted runnable/model/reasoning, updated with resolved RunBegin runnable;
later session changes cannot rewrite the snapshot.

Queue adjoins Input with no separator. Expanded Queue shows a summary, gap, up
to eight one-line previews and a trailing gap; collapsed Queue shows only its
summary. Viewport pressure reduces previews. Focus uses selection background
only; entry action hints stay dim and right-aligned. Input cursor hides while
Queue owns focus. Narrow layouts truncate previews before losing the count.
Interaction/state rules belong in [Chat](chat.md#queue-and-controls).

Pending steers retain magenta bars plus one aggregate `• N steer(s) pending`
row. A receipt alone is not adoption: matched Step `preceded_by` or durable
applied status commits it. At termination only confirmed unapplied steers show
`not applied`; uncertain evidence stays unlabeled and uses recovery diagnostics.
Late callbacks cannot alter completed scrollback.

The session status line shows session runnable left, model/effort right and
`agent@workspace` at absolute center. While running, center uses the active root
workspace; root chdir updates it, child chdir does not. `/cd` changes later-session
workdir. Edge labels elide toward the fixed center and disappear before center
truncation. Model effort shows explicit level/budget or applicable `auto`.

A separate two-row run bar above Queue/Input shows elapsed `Working` text; it
clears only on settlement, not a cancel request. Idle rows stay blank to stabilize
Input; idle Ctrl+L collapses them until the next run. Short viewports yield these
rows before Input or steer feedback. Status/error state never enters execution
scrollback.

Slash results use two-space indentation and one final separation row. Resource
tables retain columns, elide flexible cells and protect current-model ` *`.
Reopened `/output` uses a quiet dim divider and recorded result, without replay.

## Implementation and verification

[Shared projection/rendering](../src/toolang/cli/common/execution_progress/),
[script presenter](../src/toolang/cli/common/script_progress/) and
[Chat presenter](../src/toolang/cli/toolang/commands/chat/presenter.py) own the paths.
[Projector](../tests/unit/cli/test_execution_progress_projector.py),
[dynamic calls](../tests/unit/cli/test_agic_run_progress.py),
[facts](../tests/unit/cli/test_execution_progress_facts.py),
[Markdown](../tests/unit/cli/test_markdown_rendering.py),
[tool rendering](../tests/unit/cli/test_tool_progress_rendering.py) and
[TUI tests](../tests/unit/cli/test_chat_tui.py) are the detailed scenario matrix.
[Deterministic terminal tests](../tests/system/cli/test_chat_tui_e2e.py) verify the
real terminal boundary; live-provider cases remain separately opt-in.

# Execution Presentation

This document defines the execution progress language shared by script mode and
the Chat TUI. It is a presentation contract only: it does not add execution
events, records, identities, or lifecycle states.

Inspection reads durable state and may reuse the same vocabulary, but it does
not reconstruct live progress from stored records.

## Projection Model

Ordered native events pass through one terminal-independent pipeline:

```text
RunEvent -> ProgressProjector -> ProgressUpdate -> surface presenter
```

`ProgressUpdate.committed` contains stable, append-only fragments in event
order. `ProgressUpdate.live` is the complete replaceable snapshot. A Step can
progressively commit a header and output while retaining only its unfinished
tail in live state.

Script and Chat use the same projected ownership, content, aggregates, facts,
and errors. Their presenters own terminal mechanics such as wrapping, lane
truncation, scrollback, and live-area replacement.

A Run has no standalone progress header. A root Run contains projected Steps
followed by one footer. Child Runs are represented through the Steps they
execute rather than separate Run headers or closure rows. A model-produced
dynamic Run Step owns its own opening and closing divider around that child
content.

## Width and Alignment

Progress is at most 120 terminal cells wide by default and never wider than an
attached TTY. Set `TOOLANG_PROGRESS_MAX_WIDTH` to a positive integer to change
the maximum for both surfaces. Non-TTY script output uses that configured
maximum as its available width, equivalent to a TTY with no narrower physical
width.

Execution markers start in column zero. Wrapped content and unmarked
continuations align with the text after the marker:

```text
• Alpha beta gamma delta epsilon
  zeta eta theta
```

Flow headers also start in column zero and are followed by one blank line.
Iteration and condition headers create the same kind of stable boundary.

## Markers and Style

`•` is the only Step execution-row marker. `---  ` opens and closes a dynamic
Run Step, and `∎` marks the root Run footer. There is no separate marker for
errors, control decisions, or parallel work. The centered dot `·` is only an
inline facts separator.

- Model activity and output use `•` and normal text.
- Tool activity uses `•` and normal text. Its successful terminal marker,
  action, and output are dim.
- Flow activity and terminal output use `•` and normal text.
- Errors use `•` with error styling; cancellation uses warning styling.
- Parallel lanes place `•` after the lane columns.
- Headers and facts are dim.

Terminal markers use the same color and intensity as their following content.
Successful Model and Flow outputs use the terminal's default foreground;
successful Tool terminal output uses that foreground dimmed. Failure uses red
and cancellation uses yellow. Green is not a terminal status color.

Step paths appear only at the right edge of facts-bearing Flow Step footers. A
dynamic Run Step footer instead identifies its direct child Run. Binding
effects, result pointers, and control decisions are not displayed. Model and
Tool Steps also omit duration, model name, exit code, usage, cost, and other
per-Step facts.

## Agic Dynamic Run Steps

A Run Step owned by an Agic Run uses a flat divider scope. Ownership is
identified by the enclosing `RunBegin.runnable`, while the existing `RunStmt`
provides the target label. Its header begins in column zero and uses the
canonical resolved runnable ref:

```text
---  run agic:summarize -----------------------------------------------

• Summary text from the child.

---  2.0s · 1 run · 1 model call ---------------- succeeded run_abc123
```

The fixed prefix is three ASCII hyphens followed by two spaces. The caption,
facts, elastic hyphen leader, and child Run ID are dim. A successful status has
normal intensity and the terminal's default foreground; failed and canceled
statuses use normal-intensity red and yellow. The status style does not affect
the surrounding fields.

The header contains no Run ID. It displays the resolved `agic:NAME` or
`flow:NAME`; a failure before resolution displays bounded terminal-safe request
text, or `request` when no text is available. The footer's right field is
`STATUS CHILD_RUN_ID`, using the complete direct child identity rather than the
owning StepPath. Its facts aggregate that child's complete Run tree in the
normal order. A failure before child acceptance has no facts or invented
identity:

```text
• Runnable not found: missing

---  -------------------------------------------------------------- failed
```

Every structural marker remains in column zero; nested dynamic calls do not
introduce indentation. An agic child retains normal Model and Tool traces. A
flow child retains its numbered Flow headers and StepPath footers, so the
caller-owned dynamic boundary and callee-owned grammar stay distinct. Dynamic
calls inside compact parallel lanes remain one physical lane row.

At narrow widths the renderer shortens the leader first. Facts then wrap at
fact boundaries under a five-cell hanging indent, followed by a final leader
and complete status-plus-ID field. Long captions and identities fold by display
cells without truncation. Exactly one blank row follows the header, precedes the
footer, and follows the footer; adjacent child-owned gaps coalesce.

## Flow Headers

A Flow Step uses its non-empty authored doc comment as the header. Without a
doc comment, the presenter generates a short sentence from the typed AST. It
preserves runnable names exactly, hides generated inline runnable names, and
includes authored concurrency or binding behavior only when useful.

Examples include:

| Statement | Generated header |
| --- | --- |
| `let` | `Set NAME` |
| `run` | `Run RUNNABLE` |
| `scatter` | `Expand into N items with RUNNABLE` |
| `storm` | `Run RUNNABLE N times` |
| `gather` | `Combine the items with RUNNABLE` |
| `settle` | `Reduce the items with RUNNABLE` |
| `map` | `Run RUNNABLE for each item` |
| positional `keep` or `drop` | `Keep/Drop the first/last N items` |
| predicate `keep` or `drop` | `Keep/Drop items selected by RUNNABLE` |
| `rank` | `Rank items with RUNNABLE` |
| fixed `repeat` | `Repeat N times` |
| conditional `repeat` | `Repeat up to N times` or `Repeat until complete` |

A direct single-Run Flow Step preserves that Run's leaf trace and emits no
synthetic success row. Absence of an error means success. Direct values are
displayed as values; output shapes such as `1 item` or `6-item list` are not
displayed.

## Model and Tool Trace

Outside parallel work, every leaf Step leaves a complete trace. Model text is
incrementally projected as Markdown. The initial live row:

```text
• Thinking...
```

is replaced once text arrives, while stable Markdown progressively enters
scrollback:

```text
• # Deep Research Brief
  ## Executive summary

  The evidence supports explicit ownership
```

Complete Markdown blocks move to scrollback as soon as later input makes them
stable. Only the unfinished block remains live. The transition does not visibly
change already-rendered text. At Part closure the remaining tail is committed,
and Step closure does not repeat the final output.

Tool activity and terminal output use this form:

```text
• Executing search “Toolang plugin protocol” ...

• Executed search “Toolang plugin protocol”

  [                                                            ] background
  [ {"results":[{"url":"https://example.com"}]}          ]
  [                                                            ]
```

The brackets above label colored cells; they are not emitted as terminal
glyphs.

Every Step begins after one unpainted blank line, including model output that
follows a Tool Step. A preceding statement, iteration, or condition header can
own that same separator through its trailing blank row; the following Step
does not add a second one. Continuation rows from the same Step do not add
another separator. A standalone live Tool summary is dim, including its marker;
terminal Tool summaries remain ordinary progress rows. After another unpainted
blank line, a succeeded result or failed diagnostic follows on a borderless,
background-filled detail surface. The detail content has one empty row above
and below it and one empty column on each side, matching code-block padding. The
detail surface wraps like a code block and fills the available progress width
up to `TOOLANG_PROGRESS_MAX_WIDTH`. Non-TTY output preserves the same gaps,
padding, and width while omitting ANSI sequences. The surface shares ANSI
palette slot 8 with the Chat control bar and input box, so terminal themes
retain ownership of the actual color.

The executor records a human-readable `summary` when the Tool Step begins and
another when it ends. Summary generation receives the tool `family`, leaf
`name`, and supplied `args` in tool-schema declaration order. The default
summary uses only `name` and the first argument; it does not repeat `family`.
The default running form is `Executing NAME ARG ...`; the succeeded and failed
forms are `Executed NAME ARG` and `Failed NAME ARG`; the canceled form is
`Canceled NAME ARG`. Argument previews are single-line, bounded, and redact
sensitive fields. A failed or canceled Tool Step records the corresponding
terminal summary; its concrete error remains a separate diagnostic
continuation.

Historical Steps without summaries retain the compatibility forms `executing
TOOL`, `executed TOOL`, `failed TOOL`, and `canceled TOOL`.

Structured Tool results use compact single-line JSON. Textual `stdout` and
`stderr` preserve their original lines. Model `ToolCallPart` values are not
displayed; the following Tool Step owns the visible call activity. Mixed Model
output retains its text and other displayable Parts. A successful Model Step
containing only tool-call Parts commits no terminal row and leaves its live
position to the following Tool Step. This presentation suppression does not
change execution status, records, metrics, or footer facts.

For a streamed Model Text Part, concatenated deltas must be an exact prefix of
the authoritative Part closure. A successful Step result must contain that
same completed Part. Started Parts, child Steps, and child Runs must close
before their owning Step closes. Violations are reported as execution contract
errors rather than repaired by the presenter.

## Flow Step Footer

A Flow Step that owns child execution may append one dim footer:

```text
[2] Search the web for each query

• Mapped all 6 items in parallel
  31.0s · 6 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00        run_root.2
```

Facts retain their two-cell indentation at the left, and the complete canonical
StepPath is right-aligned to the available progress width. At least two cells
separate the fields. When they do not fit together, facts wrap under the same
indent and the untruncated StepPath follows on a right-aligned continuation
line. Undefined facts are omitted, and a StepPath is not displayed by itself.
Usage and optional cost form one `↑INPUT ↓OUTPUT $COST` fact, with cost rounded
to cents. Model, Tool, and direct-value Steps define no footer facts. Run facts
appear only in the root footer and aggregate the complete Run tree.

The footer immediately follows the owning Step's last visible output. A direct
single-Run Flow Step therefore places its footer directly after its child Model
output without opening another visual section. The normal trailing blank row
still separates the completed Flow Step from the next Step or root footer.

## Parallel Work

Parallel work keeps one aggregate live row and one physical row per active
lane. Lane rows are truncated rather than wrapped:

```text
• running · 4/18 succeeded · 3 active
  0 | #4 | • Thinking...
  1 | #5 | • executing web.search
  2 | #6 | • Source summary prepared
```

On success, the live lanes are cleared and one natural-language result remains:

```text
• Mapped all 7 items in parallel
• Brainstormed 7 items in parallel
• Evaluated 7 items in parallel, kept 5
• Evaluated 7 items in parallel, dropped 2, leaving 5
• Scored 10 items in parallel, kept the top 8
```

On failure, successful, active, and canceled lanes are cleared. Each failed
lane retains its causal error, followed by the parallel Step's distinct
boundary error:

```text
• Parallel execution stopped: 4/18 succeeded, 1 failed, and 2 were canceled
  1 | #5 | • failed fetch_page
             provider returned status 429

• parallel step stopped because lane 1 (#5) failed
  31.0s · 7 runs · 12 model calls · 8 tool calls · ↑18.4k ↓5.2k $0.00
```

## Repeat and Settle

Repeat and Settle use the same loop presentation. Each iteration follows the
normal trace-or-lane rule for its child statement:

```text
--- iteration 1 of 3 ---

<?> completion_check

• Thinking...
• true
```

The condition is a child Run, not a synthetic `executed completion_check`
Step. Terminal loop output identifies the actual cause:

```text
• Completed all 3 iterations
• Condition met after 2 of 3 iterations
• Completed all 3 iterations without meeting the condition
• Interrupted after completing 2 of 3 iterations
• Canceled after completing 2 of 3 iterations
• Settled all 6 items in 6 iterations
```

The causal child error remains at the child Step or lane. The loop row describes
termination without repeating it.

## Error Ownership

Each causal error is displayed once, as close as possible to its owner:

- a Step error occupies that Step's normal output position;
- a failed lane preserves its concrete error;
- a parallel Step adds only its distinct local-failure boundary error;
- parent Step and Run pointer errors remain silent; and
- a concrete Run error without a Step owner becomes a final `• MESSAGE` row.

A malformed presentation stream also clears live state and emits one
root-owned error row. There is no separate diagnostic marker.

## Root Run Footer

Script and Chat end a root Run with the same footer:

```text
∎ run_nrqpt0mf succeeded        1m 16s · 26 runs · 32 model calls · 8 tool calls · ↑43.8k ↓17.6k $0.01
∎ run_nrqpt0mf failed           1m 16s · 26 runs · 32 model calls · 8 tool calls · ↑43.8k ↓17.6k $0.01
∎ run_nrqpt0mf canceled         1m 16s · 26 runs · 32 model calls · 8 tool calls · ↑43.8k ↓17.6k $0.01
```

A CLI retry or rerun identifies the operation in the same footer instead of
appending a separate result line:

```text
∎ run_zvczap2h: retry succeeded        2.0s · 1 model call
```

The U+220E END OF PROOF character marks the complete root Run; square brackets
do not frame the footer. The Run caption stays at the left while facts align to
the available width's right edge, separated by at least two spaces and no
centered dot before the first fact. When both fields do not fit, the caption is
followed by facts wrapped on two-cell-indented continuation lines.

The marker, Run identity, operation, and status use normal intensity. A
successful caption uses the terminal's default color; failed and canceled
captions use red and yellow respectively. Facts always use dim intensity and
the terminal's default color, independent of status.

The footer owns total duration and whole-tree Run facts. Script does not append
a separate `Run: RUN_ID` line. Errors before `RunBegin` are reported outside
execution progress; later terminal errors belong to progress and its footer.

## Reopened Chat Result Divider

The Chat TUI `:show` command introduces a durable result with a quiet divider:

```text
• run_ma8hccd9 result ────────────────────

• Result body rendered as Markdown.
```

The `•` marker, caption, and rule are dim, while the result body retains
normal intensity. The divider uses a fixed 42-cell width and shortens only when
the available width requires caption truncation. Exactly one blank line
separates the divider from the result body.

## Surface Behavior

Script writes progress to stderr. It does not copy the durable root result to
stdout by default. `--save -` writes the result to stdout and `--save PATH`
atomically writes it to a file. Failed and canceled Runs do not write the
selected destination. Progress is enabled by default for both TTY and non-TTY
stderr; `-q` or `--quiet` suppresses prepare and execution progress, including
the root Run footer. Actionable errors remain visible in quiet mode. Non-TTY
output contains stable newline-delimited content without color, cursor
movement, or partial delta lines. Its semantic rows and block geometry match
TTY output; only live replacement, ANSI emission, and width-dependent wrapping
differ.

TTY script output uses one event-driven Rich `Live` area per root Run. Chat
does not create a Rich `Live` because prompt_toolkit owns its terminal; it uses
the same Rich Markdown renderables while moving committed fragments into
scrollback and retaining only replaceable fragments in its live container.

Model and result Markdown leaves ordinary text on the terminal's default
foreground and background. Semantic styles use named ANSI colors, so the
terminal theme owns their actual RGB values. Inline code uses bold ANSI cyan
text on the terminal's default background, distinguishing it from ordinary bold
text without isolated background spans around short identifiers.
Fenced code uses ANSI slot 15 text on an ANSI slot 8 background with Rich's
`ansi_dark` token palette. Its top and bottom padding, trailing background
cells, and authored blank lines remain one rectangular surface. This is a
conventional dark code surface rather than a guarantee of contrast for
arbitrarily redefined ANSI slots.

Chat preserves terminal-default and 16-color ANSI identities through both its
live prompt_toolkit path and stable scrollback path. Script uses the same
identities on a TTY and continues to emit no color for non-TTY output. Toolang
does not probe terminal colors or infer whether the surrounding theme is light
or dark.

Chat submission and steer controls contain only their authored message. Their
left background-filled accent cells distinguish start from steer without
displaying Run IDs or execution state. The start accent uses the same ANSI
bright cyan as the banner logo and wordmark. Quick-command bars use the same
background-cell treatment with their own accent, and the prompt uses the start
accent. Control bars and the input box share the fenced-code surface's ANSI
slot 8 background, leaving its actual RGB value to the terminal theme. Control
bar messages use the terminal's default foreground and explicitly clear dim
styling in both stable and live output. An empty prompt shows the muted
placeholder `Ask anything`; the
placeholder disappears as soon as the buffer contains text and is never part of
the submitted message. The status bar does not paint a base background and
therefore inherits the terminal background. Its left side begins in column zero
with an idle marker or running spinner, followed by one space and a runnable
rendered as `agic:name` or `flow:name`. While idle, this is the current default
runnable. While running, it is the active root runnable. The dim label `running`
follows the runnable below one elapsed second and is replaced by the whole-second
duration at one second; `0s` is never shown. If the current default runnable
differs, it appears on the right as
`DEFAULT_RUNNABLE · MODEL`; otherwise the right side contains only `MODEL`.
`MODEL` is always the current default model, not an active model step, and
remains right-aligned against the terminal edge as defaults change. Setting
commands remain available while running and update these default values
immediately without changing the active run. Hotkey hints are omitted. Runnable
and model text inherit the terminal's default foreground without dim styling.
The idle marker uses the terminal's dim attribute without painting a status
background. The running spinner inherits the terminal's normal foreground
without dim styling. The activity label uses the terminal's dim attribute. The
default `circles` style uses `■` while idle and rotates
through `◐`, `◓`, `◑`, and `◒` every 300 milliseconds during a Run. The retained
`squares`, `triangles`, `quadrants`, `hatch`, and `dots` styles remain available
through an internal named style switch.
Short Runs retain the running state long enough to avoid flashing. This
transient UI state is never committed to execution scrollback.

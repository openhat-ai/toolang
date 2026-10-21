# Chat

Chat is an interactive client over normal Thread/Run execution. It owns mutable
session settings, draft input, queue and steer interactions, and presentation
recovery. [Execution](execution.md) owns durable lifecycle, [API](api.md) the HTTP
boundary, and [Execution presentation](execution-presentation.md) shared rendering.

## Session and submissions

Chat connects through RemoteRunClient to a compatible AgentServer for resident,
visiting, and roaming placements. It creates a `chat_` thread on first execution,
not merely when showing help or exiting an empty session. Subsequent turns reuse
the thread. Startup `--thread`/`-t` accepts an explicit Thread or Run ID; bare
`--thread` selects the most recently updated recorded thread and fails when none
exists. Omission starts a new session. An empty explicit value is invalid.

The client keeps model parameters, runnable selection, workdir, resource ceilings,
and limits as session defaults. Every submission snapshots them with its input-
local overrides into one self-contained RunRequest. Queued requests retain their
snapshots when visible defaults change. The server captures Setup/State and
validates the request before admission. It owns provider/environment values,
workspace authorization, input evaluation, and compatible handler resolution.

[Call Input](call-input.md#requests-and-overrides) owns one-run parsing.
Standalone commands inspect or change this session without changing accepted
or queued work.

A nullable runnable ref with `driver: "chat"` requests automatic handler
selection. A session choice or `/NAME` supplies an explicit selection. The server
selects the user Chat handler or unnamed entry; no runtime Chat handler is
packaged. A failed or invalid selected handler does not fall back to another.
[Drivers](drivers.md#handler-selection) owns this selection.

## Session commands

The command parser consumes the complete normalized submission. Colon commands
change defaults for subsequent requests; slash commands inspect or control the
client. One-run slash directives belong to [Call Input](call-input.md#requests-and-overrides).

The complete command registry is exposed by `/help`, with `/keys` for focused
keyboard controls and `:?` for session values. Bare setting commands show
focused usage. These paths never submit a Run.

Resource commands inspect the session's allowed base. Their query intersects
that base; `-a` instead starts from all available resources and reports whether
rows are allowed. Changing visibility does not change ceilings. Runnable lists
use visible language declarations rather than resource queries.

When the session's model ceiling changes, Chat preserves the complete current
model request if it remains available. Otherwise it selects the configured
default when present in the ordered result, then the first result. Only an empty
model collection leaves the session model unset.

`/help` and `/?` describe commands and run directives. `/keys` lists interactive
shortcuts. Known command help and errors enter scrollback and clear the input.
Unknown colon commands and rejected run submissions retain the draft and show
an error in the status bar. Unreserved slash names are resolved as runnables.

`:runnable`, `:agic`, and `:flow` select the session entry without arguments.
Only `:runnable default` restores automatic handler selection; kind-specific
commands treat `default` as a name. Startup `--default runnable=REF` initializes
this session only. Other sessions and drivers keep their own selections.

`:model` accepts an optional identity and `effort`/`max_output` assignments.
Parameter-only changes preserve identity; selecting an identity clears omitted
explicit parameters. `auto` clears one parameter. `:model default` selects the
effective default within session `allow.models`; `:model unset` and `:model none`
are invalid. [Models](models.md#runtime-configuration) owns expression validation.

`:allow` replaces the named session field; a one-run `/allow` adds a ceiling.
Repeated queries union within a field, while `all`/`none` cannot mix with queries.
`:limit` changes subsequent-run limits. `:workdir` queries when bare; `default`
or `unset` resets it. Relative paths use the current session directory, and
selection never grants a new workspace. [Workspaces](workspaces.md) owns that
authority boundary.

Chat trims leading/trailing blank lines and horizontal whitespace at the end of
the final line, preserving indentation and internal input whitespace. Completion
only edits the draft. A root `/NAME` resolves a runnable after reserved built-in
names; `/agic:NAME` or `/flow:NAME` accepts an explicit kind constraint. Runnable
identity and display labels follow [runnable names](program.md#runnable-names).
`/runnable NAME`, `/agic NAME`, and `/flow NAME` are not selector forms.

## Queue and steering

Enter submits while idle and queues runnable input while a Run is starting or
active. Meta+Enter sends literal draft text as steer input to the active Run;
it does not interpret slash, colon, prompt, or include syntax. A rejected steer
keeps the draft. Local acceptance records input history and clears an unchanged
draft. Ctrl+J inserts a newline; Shift+Enter does so when the terminal reports it.

A nonempty Queue opens expanded above Input without taking focus. Tab and
Shift+Tab switch focus, yielding to Input completion. Space toggles a focused
Queue. Expanded entries support Up/Down or Ctrl+P/Ctrl+N selection, `e` editing,
Meta+Enter steering, and `d`/Delete removal. Editing does not overwrite an existing
draft. Rejected steering retains the queued item. Completion dispatches requests
FIFO and preserves valid selection and expansion state. An emptied Queue resets
its next nonempty appearance to expanded.

Esc Esc, Ctrl+C, and Ctrl+D apply their cancellation/draft/exit behavior only
while Input is focused. They do not act from Queue. Esc dismisses transient
status without moving focus; Ctrl+L clears the idle display while preserving
scrollback; Ctrl+Q explicitly exits from either area. The former `/queue`, `/q`,
`/steer`, and `/s` commands are unregistered.

Steer acceptance and adoption are distinct. Pending bars count sending controls
and accepted controls awaiting a checkpoint. Only consuming Step references or
durable applied status confirm adoption. A terminal Run labels only confirmed
unapplied steers `not applied`; uncertain transport does not assert non-adoption.
Receipt/event reordering cannot change completed transcript history.

## Server acquisition and recovery

An active server is reused with its current settings; caller environment and local
startup defaults are not compared. Required drivers and explicit requirements,
including sandbox and workspace bindings, must match it. Without a server, hosting
resolves CLI, configuration, remembered selector, then host. Concurrent starters
converge on one generation. Fresh implicit resident/visiting Chat servers enable
chat only: residents persist,
visiting servers automatically reclaim after leases and accepted work finish.
Explicit start retains either placement. See [Hosting](hosting.md) and
[server lifetime](hosting.md#server-lifetime-and-client-leases) for concrete timing and cleanup.

RemoteRunClient records the accepted Run ID and observes its canonical root
stream. After interruption it reconnects through GET with the committed cursor;
it never repeats the submission. Structural replacement clears unfinished Parts.
After bounded stream recovery fails, Chat pauses queue dispatch and polls durable
Run detail after 500 ms, 1 s, 2 s, then every 5 s. Terminal truth finalizes the Run
without inventing missed events; `/output RUN_ID` retrieves the complete result.
Ambiguous pre-acceptance failure, missing accepted work, or invalid recovery
identity blocks new submissions until Chat restarts. Inspection and exit remain
available. Transport loss never requests cancellation by itself.

## History and projections

Canonical model messages contain `role` and ordered `parts`, including reasoning
where the role allows it. Runtime provenance stays separate from public message
JSON. There is no separately persisted Chat message table. Authored input and
prompt provenance live in controls; recall uses resolved Parts. Step outputs and
steers project into model history. [Records](records.md) owns the durable edges;
[Agic](agic.md) owns message assembly and guidance visibility.

HTTP RunDetail returns resolved Output envelopes, controls, and Step projections.
ThreadInfo derives title, head, active/latest Run, and counts; it does
not copy a separate parent field into canonical ThreadRecord. Fork provenance
belongs to its create/fork control. A fork inherits through its anchor without
copying physical Run/Step rows. Rewind hides the anchor and suffix, retaining
records for explicit inspection. All Thread kinds can fork/rewind under the
same state checks; those operations do not start follow-up execution.

Input recall history belongs to the runtime layout, separate from execution
records and model context; [Layout](layout.md#runtime-room) owns its paths.
Unsent drafts remain in memory; no legacy history or drafts are imported.
Terminal titles and pane metadata are specified in [CLI](cli.md#terminal-titles-and-pane-marks).
They are display/navigation hints and do not establish liveness.

## Terminal presentation

The banner caption shows `Chat <client-version>` and rows `runtime`, `sandbox`,
then available `workspaces`. Runtime shows its version; sandbox shows driver and
image/host description. Workspace inspection failure displays `unavailable`
without blocking Chat. The banner omits endpoint, container ID, and agent-home
paths; inspection/profile APIs retain their appropriate diagnostic fields.

`TOOLANG_PROGRESS_MAX_WIDTH` controls execution/command output (default 120
cells). `TOOLANG_INPUTBOX_MAX_WIDTH` separately limits Input, Queue, and status
bars, falling back to the progress limit. Invalid or empty width settings fail.
The input placeholder is `Describe your task`. Chat requests a blinking block
cursor and restores the terminal default on handled exit.

Input and Queue have distinct backgrounds and cyan/magenta accent cells. Queue
shows a right-aligned dim `N queued` summary and up to eight one-line previews,
with fewer on short terminals. Focus is shown by selected background and entry
hints, not a separate marker or changed summary style. The focused entry's hints
are `(m-enter)steer (e)dit (d)elete`. Input's cursor hides during Queue focus.

Stable blocks progressively enter native scrollback; only replaceable blocks
stay in the live area. Resize preserves terminal scrollback while repainting
live input. Execution bars, errors, metric footers, Markdown, colors, and status
geometry follow [Execution presentation](execution-presentation.md) and
[Terminal UI](terminal-ui.md).

## Control bars and status

Submitted input, steering, and command results enter stable scrollback as
separate interactions. Input bars carry the submitted runnable/model/reasoning
snapshot, so later session settings cannot relabel accepted work. Root RunBegin
can update the resolved runnable. Steering keeps its authored text and original
accent; pending feedback counts sending requests and accepted-but-unconsumed
controls. Only confirmed terminal non-adoption adds `not applied`; uncertain
transport never rewrites completed history.

Session and Run status have separate lifetimes. The session bar shows the
current session runnable/model at the edges and agent/workspace at the center.
During a Run the center follows the root's effective workspace, including root
`runtime.chdir`, then returns to the latest session workdir on settlement. Child
changes do not move it. Session-setting changes update the edges immediately
without changing the accepted request.

The Run status bar owns elapsed time and survives a cancellation request until
the Run settles. It clears between queued Runs; session errors do not stop its
timer. Status remains replaceable and is never committed to execution history.
Narrow layouts prioritize the center context and explicit reasoning choice,
eliding names before dropping context. Short terminals preserve minimum Input,
Queue, and steer feedback space before decorative status rows.

Command tables keep structured columns in scrollback and elide flexible cells
instead of wrapping physical rows. Their allowed/all distinction and model
marker survive projection. Command prose, help, tables, and reopened outputs
share the execution-output width. Chat adds the interaction separation after
the complete result. [Terminal UI](terminal-ui.md) owns display-cell geometry,
palette resolution, and terminal ownership.

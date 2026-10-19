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

[Input syntax](input-syntax.md) owns parsing: standalone Chat commands inspect or
change the session; one-run directives and CallInput produce a submission.
`/help` and `/?` show the full syntax, `:?` session settings, and `/keys` keyboard
controls. `/models`, `/tools`, `/caps`, `/agics`, and `/flows` inspect the session's
available resources. `/output` reopens durable output; `/show` remains its alias.
`:model`, `:runnable`, `:allow`, `:limit`, and `:workdir` change session values.
They do not silently change accepted or queued work.

A nullable runnable ref with `driver: "chat"` requests automatic handler
selection. A session choice or `/NAME` supplies an explicit selection. The server
selects the user Chat handler or unnamed entry; no runtime Chat handler is
packaged. A failed or invalid selected handler does not fall back to another.
[Drivers](drivers.md) and [Program](program.md#surface-rules) own this selection.

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

An active server is checked for compatibility and reused. An explicit sandbox
must match it. Without a server, hosting resolves CLI, configuration, remembered
selector, then host. Concurrent starters converge on one generation. Fresh
implicit resident/visiting Chat servers enable chat only: residents persist,
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
ThreadInfo derives title, head, origin, active/latest Run, and counts; it does
not copy a separate parent field into canonical ThreadRecord. Fork provenance
belongs to its create/fork control. A fork inherits through its anchor without
copying physical Run/Step rows. Rewind hides the anchor and suffix, retaining
records for explicit inspection. Only Chat threads can fork/rewind; those
operations do not start follow-up execution.

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
geometry belong to [Execution presentation](execution-presentation.md).

## Implementation evidence

- [Chat client](../src/toolang/cli/toolang/commands/chat/), [RemoteRunClient](../src/toolang/execution/remote.py)
- [Chat input](../tests/unit/cli/test_chat_input.py), [command behavior](../tests/unit/cli/test_chat_commands.py), [TUI](../tests/unit/cli/test_chat_tui.py)
- [Remote recovery](../tests/unit/cli/test_chat_remote.py), [remote transport](../tests/unit/execution/test_remote_run_client.py)

# Chat Orchestration

Chat is a caller over durable Threads/Runs and canonical events. It owns input
classification, mutable session defaults, queued submissions, terminal interaction
and local/remote recovery. It does not define another execution or message store.
[Execution](execution.md) owns lifecycle; [HTTP](api.md) owns the transport;
[presentation](execution-presentation.md) owns rendering and scrollback geometry.

## Session and thread ownership

Direct Chat creates its terminal thread lazily on first accepted input. Help or
exit alone creates none. `--thread` without a value resumes the most recently
updated thread, including run activity; an explicit thread or Run ID resumes
that thread. Missing/empty selections fail instead of silently creating another.
Job threads can be inspected or continued, but Chat does not implicitly reopen
a task or create a manual chore occurrence. Tmux launchers may allocate a thread
early to identify their target window, as described below.

Chat adopts concrete model, runnable, allow, limit and workdir defaults, then
owns them in `SessionSetting`. Model reasoning/budget/output parameters belong
to its `ModelRequest`. Every accepted runnable submission snapshots the session
plus sparse overrides into a self-contained request. Queue entries keep that
snapshot when later slash commands change the visible session.

Root input history retains authored source, including `$prompt` calls. Runtime
recall uses resolved inputs and canonical model/tool content. Steers are durable
Controls, not a separate message database. Reopened conversations use record
projections and recorded output, not replayed deltas. Thread fork/rewind behavior
and branchability belong in [execution](execution.md#threads).

## Input classification

A normalized submission is a complete slash command, the read-only `:?` help
interaction, or shared [run overrides and CallInput](call-input.md). Slash
commands cannot combine with colon overrides or runnable input. Normalization
removes leading/trailing blank lines and horizontal whitespace at the end of
the last line, preserving internal Content whitespace and initial indentation.
Double a marker where necessary for literal input; Content owns `$`/`@` expansion.

| Interaction | Effect |
| --- | --- |
| `/model`, `/runnable`, `/agic`, `/flow`, `/allow`, `/limit` | Parse a required setting body and atomically update the session. |
| `/cd` | Resolve a session workdir for subsequent Runs; report resolution failure without changing it. |
| `/models`, `/tools`, `/caps` | Inspect session-allowed resources; optional query intersects that base. `-a` uses all available resources and adds allowed status. |
| `/agics`, `/flows` | List runnable definitions of that kind; no query argument. |
| `/output`, `/show` | Read durable output; `/show` aliases `/output`. |
| `/help`, `/?`, `/keys`, `:?` | Read-only guide/shortcut/override help. |
| `/exit`, `/quit` | Exit the client. |

A bare setting command shows focused help, not a picker. Completion can insert
text but never submits or changes session state. Slash runnable selections do
not accept named call arguments; colon runnable overrides do.

`/model default` chooses the effective default within the session allow result;
`/model unset` and `none` are invalid. On a changed model ceiling, preserve the
current complete request if still allowed, otherwise select the configured
default or first available model. Only an empty result leaves no selected model.
Shared one-run resets and ceilings follow [execution policy](execution.md#policy-resolution).

Known slash outcomes, usage and errors become immutable scrollback and clear the
submitted draft. Bare/unknown slash input and rejected runnable submissions stay
editable with a status diagnostic. Successful settings refresh the status bar.
Transient diagnostics clear on edits, Esc or accepted interaction; persistent
connection/safety errors survive until recovery or restart. Empty Enter is a no-op.

## Queue and controls

Enter dispatches when idle and queues runnable input while starting/running.
Immediate slash interactions still execute during a Run. Meta+Enter sends the
normalized draft as literal steer text: it does not parse slash, colon, prompt
or include namespaces. Rejected steering retains the draft; local acceptance
records input history and clears only the unchanged draft.

The nonempty Queue appears above Input without taking focus. Tab/Shift+Tab switch
areas unless input completion owns the keys. Space toggles Queue expansion;
collapsed queues disable entry actions. Expanded entries support arrows or
Ctrl+P/Ctrl+N selection, `e` edit into an empty draft, Meta+Enter steer, and `d`/Del
delete. An edit/steer removes its item only after local acceptance. Losing focus
preserves selection; emptying resets the next Queue to expanded. Completion
submits queued requests FIFO without changing the chosen expansion state.

Ctrl+J inserts a newline; Shift+Enter does so where supported. Esc Esc, Ctrl+C
and Ctrl+D apply only in Input focus. Esc never changes focus or cancels from
Queue. Ctrl+L clears display while idle and Ctrl+Q exits from either area.
There are no `/queue` or `/steer` commands. `/keys` is generated from the same
shortcut metadata used for bindings.

Control receipts plus consuming Step references determine steer adoption.
Pending delivery/application stays visible; a confirmed unapplied steer is
marked after termination. Uncertain delivery is not reported as confirmed
non-adoption. Root cancellation and control timing belong to execution.

## Local and remote execution

[CLI acquisition](cli.md#runtime-acquisition-and-lifecycle) chooses embedded host,
an attached server or a temporary non-host server for any materialized placement.
An unhealthy existing runtime fails without competing embedding. Chat never
reconfigures or stops an attached runtime; it cleans up only the temporary
workload it owns. Both paths render the same RunEvents through RunClient.

Remote Chat creates/selects a thread, adopts defaults, and sends materialized
authored requests. Server Setup/State own resource validation and resolution;
client session mutations never cross as unresolved fallback instructions.
Client attachments and workspaces are separate authorities. Closing HTTP readers
detaches without canceling accepted Runs.

The banner distinguishes TUI source version, executor endpoint/version, sandbox
identity and host-side home. Remote version is omitted only for a confirmed clean
match. Optional sandbox description cannot prevent connection; additive unknown
profile fields are tolerated. Embedded execution uses the local host description,
while Docker displays its selector and abbreviated instance identity.

## Recovery

Remote acceptance records a root ID before the first event. If its stream breaks,
Chat pauses the queue and polls durable detail after 500 ms, 1 s, 2 s, then every
5 s. Terminal truth finalizes without inventing missing events and points to
`/output RUN_ID` for complete output. It does not reconnect expecting event replay.

Ambiguous pre-acceptance failure, missing accepted Run or invalid recovery
identity blocks further submissions until restart; read-only commands and exit
remain usable. The client never retries an ambiguous submission or falls back
to embedded execution after selecting remote transport.

## Terminal Titles and Tmux Metadata

Interactive chat publishes the thread title with OSC 0. In iTerm2 this sets the
session name and window title; the tab title follows the session name by default.
In tmux it sets `pane_title`, independently of `window_name`.
The title has no role prefix. Before the title is available, chat shows
`[new chat]`, even when its thread ID is already known. Titles are single-line,
limited to 60 display columns, and stripped of terminal control characters.

A resumed chat reads its title at startup. A new thread reads its title once the
first run is accepted, with run end as a retry opportunity. Queries run outside
the UI event loop; title output is flushed through the TUI's terminal output.
Normal exit clears the title. Abrupt termination can leave the last title behind.

OSC output requires TTY stdin and stdout. Files, pipes, scripted chat, and JSON
receive no title sequences. No iTerm2 user variables or tmux passthrough are sent;
nested terminal configurations are outside this feature's scope.

In an interactive tmux pane, chat also publishes identity metadata:

| option | scope | value | written |
| --- | --- | --- | --- |
| `@toolang_agent` | session | agent name | when placement determines the agent's session |
| `@toolang_thread` | window | full thread id, e.g. `term_6xp42qxg` | when the launcher creates the thread window |
| `@toolang_pad` | pane | `chat` | when the launcher prepares the chat pane |

Each option has one scope. Session and window metadata survive chat exit and
remain the source of identity, so renaming a session or window does not break
lookup. Default names remain the agent name for sessions and the thread id for
windows. A retained failed pane keeps its pad mark; discovery checks pane
liveness as well as the mark.

Tmux placement, naming, and metadata require TTY stdin and stdout plus `TMUX` and
`TMUX_PANE`. `TOOLANG_TMUX=0` disables all three; `false`, `no`, and `off` also
disable them, case-insensitively. OSC titles remain enabled on a TTY independently
of that switch. The launcher writes target metadata and starts the child with
`TOOLANG_TMUX=0`, so the child neither places itself again nor republishes marks.
A chat started in the current thread window publishes on a background worker
and clears its pad mark when returning to the shell. Existing marked windows
retain user-assigned names. Placement failures are reported in the invoking
terminal; `TOOLANG_TMUX_DEBUG=1` reports best-effort publication diagnostics.

## Tmux Agent Sessions

Inside tmux, `too <agent> chat` locates or creates the target in the agent's
session and enters its chat pane. Metadata determines identity; renaming a
session or window does not break reuse.

| situation | behaviour |
| --- | --- |
| non-TTY, outside tmux, or `TOOLANG_TMUX=0` | run directly; no placement or metadata when disabled/outside tmux |
| no `--thread` | create an empty thread first, then its own window |
| existing live chat for `--thread` | select that pane without starting another chat |
| invoking pane is unmarked in the exact thread window | run in that pane |
| retained failed chat for `--thread` | explicitly retry in that pane |
| missing target | create the missing session, window, or chat pane |

New chats placed by the launcher have a persistent thread ID before the child
starts. They leave an empty thread if closed without submitting a message.
Direct execution keeps lazy creation on first submission. A missing or invalid
explicit thread fails before tmux target creation.

The launcher starts the normal Chat command with `TOOLANG_TMUX=0` and the resolved
`--thread`. It preserves the prepared agent/root, chat options, and working
directory. The child does not enter placement or publish metadata. OSC 0 title
updates and normal-exit clearing remain enabled on an interactive terminal.

| target location | navigation |
| --- | --- |
| current pane | none |
| same window, different pane | select the pane |
| same session, different window | select the window and pane |
| different session | select the window and pane, then `switch-client` |

This applies equally to ordinary clients and iTerm2 Control Mode. Same-session
navigation never issues `switch-client`, avoiding unnecessary iTerm window
rebuilding. A real cross-session switch can rebuild iTerm's mapped windows.
Tmux chooses one current client using its normal rules; if multiple clients
share a session, the pane process cannot reliably identify which supplied the
input. Window and pane selections themselves are shared tmux state.

Targets are created detached and selected after preparation. The invoking
terminal prints `located chat pane %6 in b:term_emrcwvjn` for an existing chat,
`created chat pane %6 in b:term_emrcwvjn` for a new pane, or
`reused chat pane %6 in b:term_emrcwvjn` for a retry. Names reflect the current
session and window; `%6` can be passed to a pane command such as
`tmux select-pane -t %6`. Starting Chat in the current pane prints no notice.
A navigation failure appends `; failed to switch: <reason>` to the notice.
Creation failures use `failed to create chat pane: <reason>`.
Lookup, creation, and navigation failures are reported here with a nonzero exit
status. A failed lookup never means the target is absent.
A navigation failure keeps the target and does not start a duplicate local chat.
Creation success confirms that the pane exists, not that Chat startup finished.

Use tmux 3.6 or newer on Linux for reliable exit handling. Older builds with
utempter can lose child exit notifications, leaving exit status empty or keeping
a successful pane open ([upstream fix](https://github.com/tmux/tmux/issues/4559)).

Each new pane's command sets its own `remain-on-exit failed` before executing
Chat. Startup and later nonzero exits retain the pane, error output, and exit
status, even when it is the session's last pane. If retention setup itself fails,
the pane displays that error and waits for Enter without starting Chat. Retrying
a known thread restarts its failed chat pane; there is no automatic restart loop.
Normal successful exit closes the created pane. Sessions created by the launcher
use `detach-on-destroy off`, allowing an attached client to fall back to another
session when the last chat closes. No global tmux options are changed.

The agent session is found by `@toolang_agent` first and its derived name second.
Names use lowercase `[a-z0-9-]`; a name already owned by another agent is suffixed
(`eve-2`). New windows are named after their thread ID. Existing marked windows
keep their user-assigned names.

Created panes use tmux's environment, with `TOOLANG_TMUX=0` explicitly set for
Chat. Variables exported only in the invoking shell are not generally inherited.
If the initial tmux lookup is unavailable, Chat runs in the current terminal;
once target preparation begins, failures are surfaced instead of falling back.


## Implementation and verification

The [Chat package](../src/toolang/cli/toolang/commands/chat/) separates input,
policy, slash commands, widgets, history and local/remote clients.
[Input](../tests/unit/cli/test_chat_input.py),
[policy](../tests/unit/cli/test_chat_policy.py),
[TUI](../tests/unit/cli/test_chat_tui.py) and
[remote execution](../tests/integration/cli/test_chat_remote_execution.py) tests
cover interaction and submission safety. [Tmux placement](../tests/integration/cli/test_tmux_chat_placement.py)
and [terminal titles](../tests/unit/cli/test_chat_terminal_title.py) cover terminal
integration without making titles into durable identity.

# Pane-local tmux integration and OSC titles

Status: proposed definition for review; no implementation changes.

## Scope

Chat and interactive Talk stay in the invoking terminal. Toolang only publishes
OSC titles and pane-local marks: no automatic placement, reuse, splitting,
focus/layout changes, session/window naming, or tmux server startup. Users can
build their own tmux scripts from the marks.

Marks cover Chat, Talk, and continuous Top. Titles also cover foreground Serve,
Script Run, Retry, and Rerun. Concurrent draft/submission behavior, job-control
handoff, pickers, and terminal configuration remain outside scope.

This replaces the relevant placement, naming, metadata, and title rules in
[Tmux Chat routing](tmux-control-mode.md), [Talk status bar](talk-status-bar.md),
and [OSC Chat titles](osc-chat-title.md).

## Pane marks

All options are strings named `@toolang_<field>`. Use `convo`, not `conversation`.

| Field | Value |
| --- | --- |
| `kind` | `chat`, `talk`, or `top` |
| `root` | Resolved absolute Toolang root |
| `agent` | Bare agent name for Chat and single-agent Top |
| `thread` | Full Chat thread ID once created |
| `convo` | Full canonical Talk conversation ID, including pending DMs |
| `context` | Existing opaque Talk connection identity, shared by team Top |

Resolve values from `AgentLayout` or the selected root and `HubConnection`.
Reuse Talk's existing context hash over root, backend identity, viewer, and
endpoint without changing its draft key. Inapplicable or unknown fields are
empty strings. Top describes its scope, not its selected row. Titles are never
identity fields; several panes may describe the same content.

- The foreground UI captures its tmux server and pane ID and uses libtmux.
  Require TTY stdin/stdout and a reachable pane. Retain `TOOLANG_TMUX` opt-out
  spellings and `TOOLANG_TMUX_DEBUG`; opt-out affects marks, not OSC titles.
- Clear `kind`, write every other field, then publish `kind` last. A required
  write failure leaves the pane unpublished. New Chat starts with an empty
  thread field and updates it at normal lazy creation; never create a thread
  just for routing or marking.
- Deduplicate and order writes off the UI loop. On normal/error exit, cancel
  queued writes, remove `kind` first, then the other owned fields with bounded
  cleanup. Failures are nonfatal; do not restore stale marks. One foreground
  publisher per pane is supported.
- Consumers require a recognized `kind` and its required fields, reading only
  pane-local options with `show-options -p` **without `-A`**. Format expansion
  can inherit old session/window values. Ignore unknown kinds/fields and legacy
  marks; keep old session/window options untouched and perform no dual writes.

```sh
tmux show-options -p -t "$TMUX_PANE"
```

Retire `@toolang_pad` and `@toolang_talk`; do not revive `_id` or title aliases.
See the [tmux option reference](https://man.openbsd.org/tmux#show-options).

## Titles

| View | Title |
| --- | --- |
| Chat | `alice: thread title`; only `alice` until a title is available |
| Talk | `@alice`, `@alice,bob`, `#dev` |
| Top | `top alice` or `top team` |
| Serve | `serve alice` or `serve hub` |
| Script Run | `run file.too` or `run file.too runnable` |
| Rerun | `rerun run_id` |
| Retry | `retry run_id` |

Script titles use only the basename, retaining `.too`; omit unnamed entry IDs.
Direct file dispatch and `too run` agree. Retry/Rerun use the resolved source run
ID throughout the operation, including when Rerun creates a new run.

Talk reuses `conversation_label`, including its unnamed-GC ID fallback. After
sanitization, empty agent names use `agent`; bare `@`/`#` labels use `@?`/`#?`.
Add no extra IDs, status, counts, or decorations.

## Prefix configuration

Each type defaults to `""`: `chat`, `talk`, `top`, `serve`, `run`, `rerun`, `retry`.
Top's two scopes share one setting, as do Serve's. There is no global prefix or
cross-type fallback.

Configure root or agent-home `config.toml`:

```toml
[terminal.title.prefix]
chat = "💬 "
top = "[too] "
run = ""
```

Precedence, highest first: **CLI `--title-prefix TEXT` > env
`TOOLANG_TITLE_PREFIX_<TYPE>` > home config > root config > empty default**.
Environment suffixes are uppercase, for example `TOOLANG_TITLE_PREFIX_CHAT`.
Merge per type by presence: omission inherits, an explicit `""` clears, and
strings replace rather than append. Follow existing
[configuration precedence](../api.md#runtime-commands).

- Resolve once at CLI entry and pass a concrete string to the publisher.
  Reuse `AgentLayout`/`config_sources`; home means the agent's home. Root-only
  Talk, team Top, and Hub Serve skip home. Roaming/script commands retain
  existing outer-to-inner project `toolang.toml` discovery and isolation.
- Reuse root `.env` < home `.env` < process environment ordering; roaming/script
  commands retain process-only environment behavior. No state creation or
  live config watcher is needed for prefix resolution.
- Add the option through existing command factories. Its absent value is
  `None`; explicit empty values survive Script parent/runnable option handling
  and both invocation forms. Reject malformed tables, unknown prefix keys,
  and non-string values with source/key errors; leave unrelated settings alone.

```sh
too alice chat --title-prefix '[too] '
TOOLANG_TITLE_PREFIX_CHAT='' too alice chat
```

Compose `<prefix><label>` directly. Preserve printable spaces exactly, including
leading, trailing, repeated, and space-only prefixes: `'💬'` gives `💬alice`,
while `'💬 '` gives `💬 alice`. Do not trim or insert a separator. Prefixes affect
neither marks nor stored titles; a prefix sanitized to empty stays empty.

## Publication

- Emit and flush only `ESC ] 0 ; <title> BEL` through the foreground UI/status
  stream. Require TTY input, output, and presentation stream. Scripted Chat,
  Talk directory/send, Top snapshots, quiet execution, files/pipes/JSON,
  background commands, and internal service children emit neither title nor
  cleanup sequences. The outer CLI owns publication.
- Use common safety filtering: strip terminal escapes, C0/DEL/C1 controls,
  Unicode line separators, and invalid surrogates. Normalize generated label
  whitespace separately; never normalize prefix spaces after concatenation.
- Limit the complete title to **32 display columns**, including the prefix.
  Limit Chat's agent component to 12 columns. Clip from the right with `…`,
  preserving retained spaces and whole grapheme clusters, including joined
  emoji. A long prefix may consume the label budget; it has no separate limit.
- Publish on entry after resolving identity/config. Chat retains existing
  asynchronous lookup opportunities, ignores obsolete results, and keeps the
  agent fallback on failure. Talk follows existing label refreshes; other
  titles stay fixed. Add no polling. Deduplicate final payloads, retry failed
  writes on the next normal update, and keep output failures nonfatal.
- On normal/handled-error exit, stop late updates and clear once with
  `ESC ] 0 ; BEL` if any title write succeeded. Never include a prefix in cleanup
  or query/restore an unknown previous title.

OSC 0 updates tmux `pane_title` when allowed. Do not add `select-pane -T`,
window/session renaming, terminal-specific protocols, or configuration changes.
Respect tmux's `allow-set-title`, automatic naming, and user display formats;
iTerm2 `-CC` uses the same application output contract.

## Implementation

- `cli/common/tmux.py`: pane-only writer and lifecycle; remove launcher,
  placement/reuse/naming helpers, session/window writers, child environment
  forwarding, and failed-pane retention. Delete their unused helpers/protocols
  and obsolete tests; add no compatibility branches for removed behavior.
- New `cli/common/terminal_title.py`: shared filtering, clipping, emission,
  deduplication, and cleanup. Command owners construct their labels.
- `cli/config.py`, `cli/common/{context,parameters}.py`: prefix parsing,
  source layering, and shared option; keep presentation out of execution schemas.
- `cli/toolang/commands/chat/{main,marks,title,tui}.py` and
  `talk/{__init__,tui}.py`: remove placement; preserve lazy thread creation and
  conversation selection; connect marks/titles. Keep Talk labels in `talk/status.py`.
- `cli/toolang/commands/{top,runtime,hub,script,thread}.py` and
  `cli/common/activity.py`: title lifetimes/options. Public agent Serve is
  `runtime.run`; internal `runtime.serve` does not publish.
- Paths above are under `src/toolang/`. Update relevant CLI/config/title tests,
  `docs/{chat,messaging,api}.md`, and the implementation changelog entry.

## Acceptance and migration

1. Chat/Talk launched in a second pane stay there, even with matching content
   elsewhere. No container/name/focus changes or eager thread creation occur;
   outside tmux no server starts, and normal shell exit status is preserved.
2. Sibling/moved panes retain independent marks and titles. Legacy inherited
   options cannot supply identity. Normal/error/queued-write cleanup, opt-out,
   unreachable tmux, and publication failures follow the lifecycle above.
3. Every title row, Chat lookup state, Talk label change, and excluded command
   mode produces the expected bytes. An isolated tmux test verifies `pane_title`
   updates without changing explicit container names or user title settings.
4. Prefix precedence, partial tables, explicit empty values, whitespace, invalid
   config, and resident/visiting/roaming/root-only sources resolve consistently.
   Direct Script and `too run` options agree; one type never affects another.
5. Unicode, controls, joined emoji, long prefixes, and long labels produce safe
   payloads within 32 columns. Failed writes can later succeed and still clear;
   closed publishers ignore late updates. Run the repository's default checks
   for implementation changes; tmux tests must use an isolated server.

Users relying on automatic placement must open panes themselves or script from
pane-local marks; existing metadata consumers must migrate off session/window
options. Title formats/limits change as specified above. SIGKILL can leave stale
marks/titles, so marks are not a liveness or uniqueness guarantee. Concurrent
draft/submission semantics remain unchanged. No open design questions remain;
implementation requires approval of this definition.

Verified against current CLI/configuration code and isolated tmux 3.8: OSC 0
updates `pane_title` without changing explicit session/window names, and
`show-options -p` without `-A` reads pane-local marks only.

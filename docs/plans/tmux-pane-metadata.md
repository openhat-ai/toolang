# Pane-local tmux metadata and OSC titles

Status: proposed definition for review. No implementation changes.

## Goal and scope

Give each interactive Toolang pane a small, consistent description of what it
hosts, with short titles recognizable in tmux pickers and iTerm2 tabs. Multiple agents,
conversations, and monitors may share a window without overwriting each other's
identity. Moving a pane must preserve its metadata.

This defines pane metadata for Chat, interactive Talk, and continuously
refreshing Top, plus OSC titles and configurable prefixes for those commands,
foreground Serve, Script Run, Retry, and Rerun. Serve and execution commands
publish titles only; this definition adds no metadata kinds for them.
Chat and interactive Talk run in the invoking terminal, including a manually
created second pane. Remove automatic placement, container naming, and
identity-based reuse: do not create, split, attach, or switch tmux containers,
rename sessions/windows, or change layout. Outside tmux, run normally without
starting a tmux server. Marks never route commands or prevent duplicate views.
The tmux integration only publishes OSC titles and pane-local marks, leaving
users free to implement their own tmux scripts. No picker, layout manager, or
new concurrent-execution/draft-storage behavior is included; `--title-prefix`
is the only new CLI option.

When implemented, this supersedes the placement, naming, metadata, and title
rules in [Tmux Chat routing](tmux-control-mode.md), the metadata and terminal title
rules in [Talk status bar](talk-status-bar.md), and the title rules in
[OSC Chat titles](osc-chat-title.md). Other behavior remains outside this scope.

## Fields

All fields are string-valued pane user options named `@toolang_<field>`.
Use the established short name `convo` for conversation IDs and `kind` in place
of `pad`. Do not add a `conversation` spelling or parallel `_id` aliases.

| Option | Value | Required for |
| --- | --- | --- |
| `@toolang_kind` | `chat`, `talk`, or `top` | Every published pane |
| `@toolang_root` | Canonical absolute Toolang root, expanded and resolved by the CLI | Every published pane |
| `@toolang_agent` | Canonical agent name, without the `agent:` selector prefix | Chat and single-agent Top |
| `@toolang_thread` | Full thread ID | Chat once a thread exists |
| `@toolang_convo` | Full canonical conversation ID, including for a pending DM | Interactive Talk |
| `@toolang_context` | Opaque Hub connection context described below | Talk and Hub-wide Top |

Write every field on startup. Unknown or inapplicable values are the empty
string, never a placeholder such as `new`, `none`, or a display title. This masks
legacy inherited values while the pane is published. For example, Talk writes
empty agent and thread fields even if its parent session/window carries them.

Chat uses `AgentLayout.root` and `AgentLayout.name`, including visiting and
roaming layouts. Single-agent Top uses the same values. Talk and Hub-wide Top
use the resolved CLI root and the selected `HubConnection`.

Hub context preserves Talk's existing connection identity calculation, expressed
as `H(value) = sha256(value.encode("utf-8")).hexdigest()[:20]`:

```text
dataset = H(root + "\0" + connection.identity + "\0" + connection.human)
context = H(dataset + "\0" + connection.endpoint)
```

Use the same calculation for Hub-wide Top. Treat the result as opaque; do not
publish endpoint credentials or use the hash as authorization. Chat and
single-agent Top leave context empty. Do not change Talk's draft storage key.

Chat identity is `(root, agent, thread)`; Talk identity is
`(root, context, convo)`. These describe content, not a unique UI: several
panes may carry the same identity. Top marks describe its agent or Hub scope,
not a selected row, filter, or currently running thread.

Native `pane_title` remains the display surface. Do not duplicate it in a
Toolang option. The metadata publisher must not rename sessions/windows or
change layout, focus, or tmux configuration. OSC publication follows the separate
display contract below and never supplies authoritative identity.

## Publication and lifecycle

- The process hosting the UI owns publication to its captured tmux server and
  `%pane_id`; the launcher does not publish on a child's behalf. Continue using
  libtmux, resolving environment and application context in CLI orchestration.
- Publish only with TTY stdin/stdout and a resolvable current tmux pane, with
  integration enabled. Talk's directory and one-shot sends, Top's `--once` and
  redirected output, and scripted Chat publish nothing.
- Keep `TOOLANG_TMUX=0` and its existing false-value spellings as the opt-out;
  it disables metadata, independently of OSC titles. Retain
  `TOOLANG_TMUX_DEBUG` diagnostics. Add no metadata environment switches.
- On entry, clear any pane-local `kind`, replace all other contract fields, and
  publish `kind` last. A failed required-field write leaves the pane unpublished.
  Publication is best-effort and must not prevent the UI from starting.
- A new Chat initially has an empty thread field. Publish its actual ID when
  the runtime creates it; never allocate a thread to populate a mark. Other
  identity fields remain fixed for that UI invocation.
- Keep writes ordered and off the UI event loop, skipping unchanged values.
  Before normal/error cleanup, cancel queued writes; remove `kind` first and
  then all other contract fields owned by the invocation, with a bounded wait.
  Do not restore stale metadata from the preceding application.
- Support one foreground metadata publisher per pane. Concurrent publishers in
  one pane and job-control handoff are outside this contract. SIGKILL or a lost
  tmux connection can leave stale values; the next enabled publisher replaces
  them. Marks alone are not proof of process liveness.

## Reading and migration

Authoritative consumers read pane-local options, without inheritance, and
require a recognized nonempty `kind` plus its required fields. Missing and empty
optional values mean no value. Do not infer identity from session/window names,
OSC titles, inherited options, or legacy markers. Ignore unknown fields; ignore
unrecognized kinds rather than interpreting them as Chat.

Read the current pane's own options with:

```sh
tmux show-options -p -t "$TMUX_PANE"
tmux show-options -p -q -v -t "$TMUX_PANE" @toolang_kind
```

Do not add `-A`: it includes inherited values. Bare format expansion such as
`#{@toolang_agent}` can inherit old session/window values, so it is suitable for
display only, not authoritative discovery. See the
[tmux option reference](https://man.openbsd.org/tmux#show-options).

Stop publishing `@toolang_pad` and `@toolang_talk`; do not revive
`@toolang_thread_id` or `@toolang_thread_title`. Keep `@toolang_convo` as the
conversation field. The shared agent, thread, convo, and context names now exist
only at pane scope. No compatibility dual writes.
Leave old session/window options and other panes untouched. Updated consumers
ignore them; existing scripts must migrate from window/session lookup to the
pane-local contract. An old running process has no new `kind` and is excluded
until restarted under the new publisher.

## OSC title contract

Put the distinguishing name first for Chat/Talk. Omit redundant `chat`/`talk`
words; Talk's `@` and `#` identify its view. Keep short readable role words where
the name alone would be ambiguous. Sanitize components before joining; titles
are display text and are never parsed as IDs. Add no automatic decorative icons,
version strings, or tmux indexes. Users may opt into the prefix below.

| View | Format or fallback | Example |
| --- | --- | --- |
| Chat with a title | `<agent>: <thread title>` | `alice: thread title`, `bob: thread title` |
| Chat without an available title, including a new Chat | `<agent>` | `alice` |
| Talk | `<convo label>` | `@alice`, `@alice,bob`, `#dev` |
| Single-agent Top | `top <agent>` | `top alice` |
| Hub-wide Top | `top team` | `top team` |

Foreground service and execution commands use these title forms:

| Command | Format | Example |
| --- | --- | --- |
| `too <agent> serve` | `serve <agent>` | `serve alice` |
| `too hub serve` | `serve hub` | `serve hub` |
| `too file.too [runnable]` | `run <file.too> [runnable]` | `run file.too`, `run file.too build` |
| `too <agent> rerun <run_id>` | `rerun <run_id>` | `rerun run_id` |
| `too <agent> retry <run_id>` | `retry <run_id>` | `retry run_id` |

Use the full word `serve`, without abbreviation. The `team` in `top team` and
the `hub` in `serve hub` are fixed scope labels, not agent names or metadata IDs.
Run uses the source file's basename, including `.too`, followed by the selected
named runnable when present. Brackets denote an optional component and are not
literal title characters. Omit the runnable for an unnamed entry; do not display
an internal entry ID or full path. The explicit `too run` form uses the same title.
Retry/rerun titles use the source run ID resolved from the command argument;
a thread argument resolves to its selected run first. Keep that label stable
throughout the operation, including when rerun creates a new run.

Agent names come from the same resolved layout as the marks, without `agent:`.
Talk reuses `conversation_label`, including named DMs, stable participant order,
viewer exclusion, and the existing `#<convo ID>` fallback for an unnamed GC.
A label reduced to only `@` or `#` after sanitizing uses `@?` or
`#?`; an empty sanitized agent uses `agent`. Do not append extra IDs, root,
context, member counts, progress, connection status, or unread counts. The
existing unnamed-GC fallback is the only automatic ID fallback.

### Optional prefixes by command type

Every type defaults to the empty string. Configure types independently in root
or agent-home `config.toml`, environment variables, or a command option:

| Config key | Environment variable | Applies to |
| --- | --- | --- |
| `chat` | `TOOLANG_TITLE_PREFIX_CHAT` | Chat with or without a thread title |
| `talk` | `TOOLANG_TITLE_PREFIX_TALK` | DM and GC views |
| `top` | `TOOLANG_TITLE_PREFIX_TOP` | Single-agent and team monitoring |
| `serve` | `TOOLANG_TITLE_PREFIX_SERVE` | Agent and Hub foreground services |
| `run` | `TOOLANG_TITLE_PREFIX_RUN` | Script execution |
| `rerun` | `TOOLANG_TITLE_PREFIX_RERUN` | Rerun execution |
| `retry` | `TOOLANG_TITLE_PREFIX_RETRY` | Retry execution |

The new TOML table has string values only; omitted keys inherit independently:

```toml
[terminal.title.prefix]
chat = "💬 "
top = "[too] "
run = ""
```

Merge low to high: **empty defaults < root config < agent-home config <
environment < explicit CLI option**. Follow the existing
[configuration precedence](../api.md#runtime-commands) convention, verified in
`cli/common/policy.py`; prefix strings replace a field rather than append.
Presence, not truthiness, selects an override: `""` explicitly clears an
inherited prefix, and whitespace-only strings remain intentional values.

Resolve configuration at CLI orchestration, once per invocation:

- For an agent, reuse its `AgentLayout` and ordered `config_sources`; `home`
  means the selected agent's home, not the user's shell home directory. Visiting
  layouts retain their existing sources. Roaming/script commands use existing
  project `toolang.toml` source discovery and its outer-to-inner order instead
  of importing resident root/home settings or creating state to read a prefix.
- Talk, team Top, and Hub Serve have no agent-home layer; use the selected root's
  `config.toml`. Reuse environment layering: root `.env`, then agent-home `.env`
  when applicable, then the invoking process environment. Roaming/script
  commands retain their existing process-only environment behavior.
- Add command-level `--title-prefix TEXT` to each title-producing command,
  through its existing factory/entry point. It replaces only the current type's
  effective prefix; absence is `None`, distinct from an explicit empty string.
  Script's parent and runnable option handling must preserve an explicitly
  empty value and behave identically through `too run` and direct file dispatch.
- Reject malformed prefix tables, unknown prefix keys, and non-string values;
  report the source/key instead of coercing values. Leave unrelated
  configuration tables alone. Do not strip strings during parsing or merging.
  Pass the final string to the title publisher; no publisher reads config/env,
  no runtime request field is added, and no live config watcher is needed.

Examples of the proposed option and environment override:

```sh
too alice chat --title-prefix '[too] '
TOOLANG_TITLE_PREFIX_CHAT='' too alice chat
too file.too build --title-prefix '▶ '
```

Types describe title presentation independently of tmux metadata kinds. There
is no global prefix or cross-type fallback; Run's setting does not affect Retry
or Rerun. Treat values as Unicode strings, with no built-in icon or emoji-only
restriction. A value reduced to empty by safety filtering publishes no prefix
and does not restore a lower-priority value.

Compose `<prefix><label>` directly. Do not insert a separator, trim the prefix,
or collapse its printable whitespace: leading, trailing, repeated, and
space-only prefixes are intentional. For example, Chat prefixes `'💬'`, `'💬 '`,
and `'[too] '` produce `💬alice`, `💬 alice`, and `[too] alice` for an untitled
Chat. Talk remains `@alice` unless its own prefix is configured.

Remove terminal escape sequences, control characters, Unicode line separators,
and invalid surrogates from the prefix using the shared safety filtering.
Normalize label whitespace separately; never trim or collapse whitespace after
concatenation. The prefix has no separate length limit; the final 32-column
title limit can clip the label or an overlong prefix at a grapheme boundary.
Prefixes are display-only: they change neither pane metadata nor stored titles.
Cleanup always emits an empty title without the prefix.

### Output and lifecycle

- Send exactly `ESC ] 0 ; <title> BEL`, then flush, through the UI-owned output.
  Use the same bytes inside/outside tmux; no title query, OSC 2 alternative,
  proprietary user variables, or DCS passthrough. See the
  [xterm OSC reference](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html).
- Enable only while the foreground command owns TTY input and output; its
  presentation stream must also be a TTY. Use the UI's output for Chat/Talk/Top
  and the existing foreground status/progress stream for Serve/Run/Retry/Rerun.
  Scripted Chat, Talk directory/send, Top snapshots, quiet execution, files,
  pipes, JSON, and background `start` commands emit no title or clear sequence.
  `TOOLANG_TMUX=0` does not disable OSC titles.
- Use shared safety filtering and a **32-display-column** limit for the entire
  composed title, including the prefix and any user-supplied spaces, independent
  of pane width and input/progress width settings. Limit a Chat agent component
  to 12 columns, including any ellipsis. With no topic, its label is just that
  bounded agent component. Without a prefix, at least 18 columns remain for a
  Chat topic after `: `; a configured prefix consumes that budget.
  For generated labels, remove embedded terminal escape sequences, collapse
  whitespace, remove remaining C0/DEL/C1 controls and invalid surrogates, and
  trim. Apply the prefix-specific whitespace rules above before concatenating.
  Clip the composed title from the right with `…` inside the limit, preserving
  whitespace in the retained text and never splitting grapheme clusters,
  including joined emoji and variation selectors. Reuse terminal-cell width
  helpers with grapheme-aware measurement and clipping. This does not shorten
  metadata or alter stored source titles.
- Publish the initial label on UI entry. Chat uses existing resume, accepted-run,
  and run-end retry opportunities until a title is available; empty/failed
  lookups keep the agent-only fallback. Thread creation alone does not change
  the title. Keep lookups off the UI loop and reject obsolete results.
  No new polling or external rename watcher is introduced.
- Talk updates when its existing conversation refresh changes the label. Top's
  scope label stays constant across refreshes, filters, rows, and connectivity
  changes. Serve/Run/Retry/Rerun publish once after their label and prefix are
  resolved, before foreground work begins, and keep the title through cleanup.
  The outer CLI owns publication; internal service children publish nothing.
  Deduplicate all emissions by the final sanitized, clipped string.
  A failed write is not cached as successful; retry on the next normal update.
- Write and flush on the owning UI loop, in order with terminal rendering;
  output failures remain nonfatal. On normal or handled-error exit, close the
  publisher and clear once with `ESC ] 0 ; BEL` if any title was successfully
  published, including a later success after startup failure. Ignore late
  updates and repeated cleanup. Do not query/restore an unknown previous title.

Terminal configuration decides how titles appear. In tmux the sequence updates
`pane_title` when allowed; it does not issue a window rename. Respect
[`allow-set-title`](https://man.openbsd.org/tmux#allow-set-title), automatic rename,
and user status formats without changing them. Multiple panes retain independent
titles; which title appears in an outer tab/window is terminal configuration.
Do not add `select-pane -T`, `rename-window`, or `rename-session` as a second
title path. Remove existing naming calls from marks and launcher orchestration;
session/window names remain owned by the user and tmux's own configuration.

The same OSC title contract applies to direct terminals, ordinary tmux, and
iTerm2 tmux Control Mode (`-CC`). Use existing terminal display settings;
picker bindings, tab formatting, and mode-specific integration or verification
are outside this scope. Keep full identity in pane metadata when display labels
are equal or truncated.

## Implementation touchpoints

- `src/toolang/cli/common/tmux.py`: shared field vocabulary, pane-only writer,
  opt-out, and lifecycle. Remove the launcher, placement/naming helpers, and
  session/window writers from this integration.
- New `src/toolang/cli/common/terminal_title.py`: shared sanitization, width
  limit, optional prefix, output, deduplication, and cleanup; accept concrete
  values/output from callers. Keep label construction with the owning command.
- `src/toolang/cli/config.py`, `src/toolang/cli/common/context.py`, and
  `src/toolang/cli/common/parameters.py`: parse/merge the CLI-owned prefix table,
  reuse existing config and environment sources, and define the shared option.
  Keep presentation settings out of Setup policy and execution schemas.
- `src/toolang/cli/toolang/commands/chat/{main,marks,title,tui}.py`: pass resolved
  identity, compose the label, retain asynchronous title lookup, and update the
  thread field at existing lifecycle hooks. Remove launcher placement and eager
  thread creation for routing; retain lazy creation at the normal first-run hook.
- `src/toolang/cli/toolang/commands/talk/{__init__,tui}.py`: publish conversation
  identity for the interactive lifetime; share the existing context derivation
  with Top through a CLI-owned helper without changing draft paths. Keep label
  rules in `talk/status.py` and delegate title output to the shared publisher.
  Remove launcher placement and retain the existing conversation selection.
- `src/toolang/cli/toolang/commands/top.py` and `src/toolang/cli/common/activity.py`:
  pass a resolved scope label into the existing continuous-display lifecycle.
- `src/toolang/cli/toolang/commands/{runtime,hub,script,thread}.py`: foreground
  title lifetimes and shared option integration. Public agent Serve is
  `runtime.run`; internal `runtime.serve` must not publish a second title.
- Existing CLI/config, tmux, and title tests plus isolated tmux integration tests;
  `docs/chat.md`, `docs/messaging.md`, `docs/api.md`, and the implementation
  changelog entry.

## Acceptance and risks

1. Chat, Talk, and Top in sibling panes expose exactly their own applicable
   values; one pane's start, updates, and exit do not change another's options.
2. A new Chat starts with no thread ID; creation updates only its pane. Opening
   an existing thread publishes the full ID without allocating another thread.
3. Single-agent Top publishes the unprefixed agent name; Hub-wide Top and Talk
   share context for the same connection. Different roots/backends/viewers or
   endpoints produce the expected distinct contexts.
4. Legacy session/window values cannot supply absent pane fields, both while a
   new publisher is active and after it exits. Old processes are not detected
   as new-contract panes; old options are not removed from other scopes.
5. Renaming containers or moving a pane preserves identity and later cleanup
   still targets that pane. Metadata publication never changes names or focus.
6. Normal exit, handled startup failure, and queued writes exercise ordered,
   bounded cleanup. Simulated tmux failures leave the application usable.
7. Opt-out, missing/unreachable tmux, non-TTY commands, Talk directory/send, and
   Top snapshots make no metadata writes. Interactive OSC remains independent
   of tmux availability and metadata opt-out.
8. Repeated values cause no redundant writes, and native titles introduce no
   additional metadata fields. Default code verification remains mandatory for
   the implementation; isolated tmux checks must not use the user's server.
9. Every title-table row emits the exact OSC 0 payload; Chat covers agent-only,
   resolved, failed lookup, and obsolete result states. Talk covers
   DM/GC labels and label changes; Top/Serve cover both fixed scopes. Script
   titles omit paths and unnamed entry IDs; Retry/Rerun retain the source ID.
10. Titles containing CJK, combining characters, emoji, multiline text, ESC/BEL,
    CSI/OSC sequences, C1 controls, or invalid surrogates remain a single safe
    payload of at most 32 display columns. Long agent names leave room for the
    Chat topic when space remains; unprefixed Talk labels retain their `@`/`#`.
    All commands share safety filtering and width handling.
11. Duplicate final values emit nothing; a failed write can later succeed and
    still clears on exit. Closed publishers ignore pending work. Non-TTY and
    excluded command modes emit no title or clear sequence.
12. An isolated tmux test confirms sibling `pane_title` values change/clear
    independently while a manually named window stays named. With title changes
    disabled in tmux, application operation is unaffected and the setting stays
    unchanged. No terminal mode adds a separate title protocol or modifies user
    display configuration.
13. With no configuration every type has an empty prefix; an effective empty or
    sanitized-empty value disables it. Setting one type never
    changes another; Top's two scopes share its setting, as do Serve's two scopes.
    Prefixes with no trailing space, one or several spaces, leading spaces, or
    only spaces concatenate exactly as configured, without inserted separators.
    Emoji, joined emoji, short text, controls, and overlong prefixes exercise
    sanitization and whole-grapheme clipping; final titles stay within 32 columns.
    Prefixes never enter metadata or stored titles, and cleanup stays empty.
14. Distinct root/home/env/CLI values select the highest present layer per type;
    partial home tables preserve unrelated root keys. Empty values at each
    higher layer clear lower values, while omission inherits and spaces survive.
    Root-only commands skip home; visiting/roaming discovery follows its layout.
    Dotenv/process ordering, malformed tables, non-string values, and unknown
    prefix keys have deterministic outcomes.
15. Each supported command exposes `--title-prefix`; direct Script dispatch and
    `too run` agree, including parent/runnable placement and explicit empty
    values. Foreground startup errors and interruptions clear a published title;
    internal children and background commands never publish one.
16. Launching Chat/Talk in a second pane stays there even when matching content
    is open elsewhere. No tmux container is created, renamed, split, or selected;
    shell exit status returns normally. Outside tmux no tmux server is started.
    A new Chat creates no thread solely for placement or marking. Users can
    discover the current content with pane-local options and `pane_title`.

Compatibility risks are consumer migration and stale marks after uncatchable
termination. Hash collisions remain theoretical as with existing Talk context;
metadata never enforces exclusivity. Concurrent Talk drafts and Chat submissions
are separate application concerns, not resolved by this schema.

Chat/Talk no longer arrange or reuse tmux containers, retain failed child panes,
or allocate a thread for routing. Users who relied on automatic placement must
open their desired pane themselves or script placement using pane-local marks.
Session/window names receive no Toolang-specific treatment.

Title changes are visible: Chat gains its agent and replaces `[new chat]` with
the agent-only fallback; title limits become 32 columns for Chat/Talk, and Top,
Serve, Run, Retry, and Rerun gain OSC titles. Prefixes default to empty; a new
option/config/env override affects presentation only. Existing `pane_title`
display configurations keep working; scripts parsing title text must use the
metadata fields where available. SIGKILL can leave a
title behind, and terminal settings may suppress or override it.

No unresolved contract choices remain in this proposal. Implementation requires
approval of this definition; concurrent draft/submission semantics remain
outside scope.

Definition verification: inspected the current CLI, title, and metadata code;
an isolated tmux 3.8 server confirmed that format expansion inherits legacy
options while `show-options -p` without `-A` reads only pane-local options.
An OSC 0 emission updated `pane_title` while explicit session/window names and
an independently written pane mark remained intact on an isolated tmux 3.8 server.
Configuration precedence was checked against CLI policy override parsing,
root/home source discovery, and runtime environment layering.

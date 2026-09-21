# OSC chat titles with tmux identity metadata

Status: approved for implementation in the accompanying pull request.

## Goal

Publish chat titles through OSC 0 for direct iTerm2 use and ordinary tmux use.
Keep existing tmux identity metadata and placement so user-renamed sessions and
windows remain discoverable. Replace title metadata with OSC output to simplify
display configuration. Title publication must not block TUI event processing.

This replaces only the title publication design in
`tmux-pane-marks.md` and `tmux-agent-sessions.md` where they conflict below.

## Decisions

### Terminal title

- Send only OSC 0 (`ESC ] 0 ; title BEL`), through the terminal output owned by
  the TUI, followed by a flush. Do not use patched `print()` or open `/dev/tty`.
- Enable only for interactive chat with a TTY output. Scripted output, files,
  pipes, and JSON receive no OSC bytes. No terminal-brand detection is needed.
- OSC 0 updates the iTerm2 session name (the default tab title) and window
  title, and the tmux pane title. OSC 2 updates only the iTerm2 window title.
- Emit the thread title alone, without a `[chat]` prefix or appended metadata.
  Until the title is available, use the thread id, or `new_chat` before a thread
  exists. The pane role is represented separately by `@toolang_pad=chat`.
- Preserve the current single-line, 60-display-column title limit. Remove C0,
  DEL, and C1 control characters before emission so user text cannot terminate
  or inject terminal sequences. Collapse whitespace before removing controls.
- Publish the fallback at startup and when a thread is created. Read the title
  at resume or first run acceptance, with run end as a retry opportunity when
  the lookup failed. Deduplicate successful emissions per thread and value.
- Perform history/HTTP lookups outside the UI loop. Deliver results through the
  existing UI event queue; ignore results for an obsolete thread or closed UI.
  Keep terminal writes on the UI loop. Do not add polling or per-message reads.
- Clear the terminal title on normal exit; do not query or restore an unknown
  previous title. Abrupt termination may leave the last title behind.
- Do not send OSC 1337, user variables, DCS passthrough, or ids as OSC metadata.
  Nested tmux/iTerm2 and iTerm2 tmux control mode are outside this scope.

### Tmux organization

- Retain libtmux and best-effort placement in the user's existing tmux server.
  Outside tmux, do not create sessions or windows.
- Keep the existing scopes and use these option names:

  | Option | Scope | Value |
  | --- | --- | --- |
  | `@toolang_agent` | session | agent name |
  | `@toolang_thread` | window | full thread id |
  | `@toolang_pad` | pane | `chat` |

  Rename the thread marker from `@toolang_thread_id` to `@toolang_thread`.
  Publish and discover only the new key. Do not read, migrate, or dual-write
  the old key; compatibility with old metadata is explicitly out of scope.
- Preserve metadata-first session discovery, unowned-name fallback, ownership
  checks, sanitization, and collision suffixes. User-renamed sessions are found
  through `@toolang_agent`, not their current display names.
- Preserve window discovery through `@toolang_thread`, including the existing
  newest-in-tmux-order selection rule. User-renamed windows remain discoverable.
- Retain pane-level `@toolang_pad=chat`: set it when chat starts and unset it on
  normal exit. Publish metadata only when stdin and stdout are TTYs and the
  process is inside tmux. Scripted/redirected chat performs no tmux placement,
  naming, or metadata publication. Do not include the marker in OSC.
- Preserve `--thread` routing: inspect the matching window's panes for the pad
  marker and focus the chat pane when present, including an inactive pane.
  Otherwise reopen a chat pane in that container using the existing launch path.
- Without a matching thread window, preserve current placement: run in place
  when already in the agent session, otherwise ensure that session and launch
  chat in a new window. Use `new_chat` until the thread id exists, then rename.
- During interactive chat, dispatch metadata writes and window renaming through
  ordered background work so they do not block submission or response rendering.
  Preserve ordering of pad marking, thread marking, naming, and exit cleanup;
  pending work must not restore a pad marker after cleanup. Launcher work
  before the TUI starts may remain synchronous. Exit must bound cleanup and
  prevent stale queued operations from changing another thread's window.
- Keep existing names after exit. Preserve existing command forwarding,
  placement notice, failure fallback, and `detach-on-destroy` behavior.
- Stop publishing `@toolang_thread_title`; use OSC 0 and native `pane_title` for
  display instead. Leave legacy title options untouched and unused. Preserve
  agent/thread metadata after chat exits; clear only the pane role marker.
- Use `TOOLANG_TMUX=0` as the switch for tmux placement,
  naming, and metadata publication, and retain `TOOLANG_TMUX_DEBUG`. OSC titles are
  independent of both.
  Do not recognize the old `TOOLANG_TMUX_MARKS` switch. No new CLI flags.

## Implementation touchpoints

- `src/toolang/cli/common/tmux.py`: rename the thread option without compatibility
  and remove title-option publication; retain metadata discovery,
  naming, and the agent/pad lifecycle.
- `src/toolang/cli/toolang/commands/chat/marks.py`: separate terminal title
  publication into a chat-owned module; retain tmux identity and pad coordination.
- Chat `main.py`, `tui.py`, and `base.py`: factories, lifecycle, background work,
  and typed UI events. Keep existing local/remote title semantics.
- `docs/chat.md`: document the three retained metadata options and use
  `pane_title` for display instead of `@toolang_thread_title`. Explain that
  window display uses its active pane's title, OSC 0 does not rename windows,
  and requires tmux `allow-set-title`; do not change user tmux configuration.
- Existing tmux, title, TUI, and PTY tests: replace obsolete mark expectations
  and add the acceptance coverage below.

## Acceptance

1. Direct iTerm2 and ordinary tmux receive the same OSC 0 payload. A tmux PTY
   smoke check reads the new `pane_title` while `window_name` stays the thread id.
   Assert the unprefixed thread title, thread id, and `new_chat` exactly;
   normal-exit clearing emits an empty title.
2. Non-TTY/scripted output contains no OSC. Unicode, multiline input, ESC, BEL,
   and C1 characters cannot break the sequence or its display-width limit.
3. Startup fallback, resume, first acceptance, failed-lookup retry, deduplication,
   and normal-exit clearing work; a closed UI ignores pending results.
4. Gated title lookup, metadata writes, and tmux renaming do not prevent UI input
   or run events from being processed. Use synchronization barriers, not sleeps.
   Pending work cannot restore a pad marker after exit cleanup.
5. Metadata-based placement still finds user-renamed sessions and windows.
   Preserve coverage for ownership collisions, duplicate thread windows, missing
   sessions, first-submission naming, disabled placement, and failure fallback.
6. Retain agent/thread/pad publication and remove title-option publication.
   Cover startup marking, exit cleanup, disabled marking, an inactive marked
   pane, and reopening chat when a matching window has no marked pane.
   Assert reads and writes use only `@toolang_thread`; legacy-only windows
   are not matched. Cover all TTY/tmux combinations and `TOOLANG_TMUX=0`.
7. Documentation display examples use native `pane_title`; identity lookup uses
   the retained metadata. Default lint, format, type checks, and the offline test
   suite pass; no live-provider tests are needed.

## Risks and limits

- The pad marker retains existing chat-pane discovery, but abrupt termination
  can leave a stale marker if its pane survives. OSC title is display state and
  must never become the source of container identity or chat liveness.
- `pane_title` belongs to a pane, so a window display follows its active pane.
  Another pane or program can show a different title without changing thread
  identity. This is intentional and avoids duplicating titles in window metadata.
- Terminal settings may reject or hide OSC titles. Toolang does not override
  those settings, and title failure never affects chat.
- This removes metadata work from the UI path; it does not promise to eliminate
  model latency or synchronous request-building and model-catalog work.

## Open questions

None. The user approved implementation and explicitly excluded compatibility.

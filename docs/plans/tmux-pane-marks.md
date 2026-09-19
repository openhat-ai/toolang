# Tmux Pane Marks

Status: Approved for implementation on 2026-09-16; amended 2026-09-19 so the thread
title is published as soon as its founding run is accepted, not only when that run
finishes.

## Goal

`too <agent> chat` running inside a tmux pane publishes three pane user options, so any
tmux-side consumer (status line today, a picker later) can see what a pane is hosting.
Outside tmux it is a silent no-op and standalone behaviour is unchanged.

| option | value | first available |
| --- | --- | --- |
| `@toolang_agent` | agent name (`AgentLayout.name`) | chat start |
| `@toolang_thread_id` | full thread id, e.g. `term_6xp42qxg` | chat start with `--thread`, otherwise when the thread is created |
| `@toolang_thread_title` | thread title, single line, at most 60 display columns | chat start for a thread that already has runs, otherwise once the run that created the thread is accepted |

## Success Criteria

1. Each mark is written as soon as its value exists. Delayed values (thread id and title
   of a brand-new conversation) are written when they become known, without polling.
2. All three marks are cleared when chat exits, including Ctrl-Q, Ctrl-C, and exceptions.
3. With tmux absent, unreachable, or killed mid-session, chat is unaffected: no visible
   error and no delay beyond the three write points.
4. The scripted (non-tty) chat path marks exactly like the interactive TUI.
5. Default verification stays offline and deterministic.

## Current Behaviour

- `chat/tui.py`: `ChatTuiApp.run(thread_id=...)`; `thread_id` may be `None` and
  `ensure_thread_id()` calls `client.create_thread()` on the first submission, so the
  thread id is deliberately late.
- `chat/main.py`: agent identity is `context_layout(ctx).name`; the non-tty path
  (`_chat_interactive_scripted_local`) has its own `ensure_thread_id()`.
- Thread titles have exactly one definition: `ThreadInfo.from_records`
  (`execution/schemas.py`) sets `title = message_summary(first run input parts) or origin`,
  with `message_summary` in `base/types/message.py`. Titles are read, never recomputed.
- No tmux code exists in the repository; `cli/common/terminal_surfaces.py` is the local
  precedent for an environment-driven, injectable helper that performs no I/O until called.
- `libtmux` exposes `Server/Session/Window/Pane.from_env()`, which read `TMUX` and
  `TMUX_PANE` from the environment, plus `server.cmd(...)` for arbitrary tmux commands.

## Scope

In: the three marks, pane target resolution, write/clear lifecycle, the title lookup those
marks need, tests, and a short section in `docs/chat.md`.

Out: everything that *consumes* the marks (picker, key bindings, dedicated tmux server) —
that is the follow-up plan. Also out: window or session marks, tmux server management, and
any change to execution records or history.

Session names are deliberately not part of this plan: the marks are pane-scoped, so chat
works in any session under any name. Verified while preparing this plan: `tmux list-panes -a
-F '#{pane_id} #{@toolang_thread_id}'` discovers marked panes without touching session names,
and `session_id` (`$N`) survives `rename-session`, so a user renaming a session breaks
nothing. Naming rules for sessions that toolang itself creates belong to the follow-up plan.

## Design

1. **Target resolution**: `libtmux.Pane.from_env()`. It parses `TMUX` and `TMUX_PANE`
   itself, so this plan contains no socket or protocol code. Resolution returning nothing,
   or raising, means "not in a pane" and disables marks for this process. Popups receive a
   synthetic `$TMUX_PANE` that is not a real pane, so a failed lookup there is expected and
   must stay silent.
2. **Writes target the pane only**: `set-option -p -t <pane id> @name value`, issued through
   `server.cmd(...)` (or `Pane.set_option` if the pinned version provides it — confirm in
   the first implementation commit). One write per value change; never in a render path.
3. **Title source**: add `thread_title(thread_id) -> str | None` to the `ChatClient`
   protocol (`chat/base.py`), implemented as
   - `local.py`: `LocalChatSession` already holds `self.store` and `self.history`, so read
     `ThreadInfo.title` there;
   - `remote.py`: one `GET /api/v1/threads` following the existing request pattern.
   A missing or empty title writes nothing; an option is never written as an empty value.
4. **Title text** is normalised before writing: whitespace collapsed to single spaces and
   clipped to **60 display columns** using `wcwidth` (already a dependency).
5. **`@toolang_agent` carries the agent name** (not a home path), so consumers match the
   user-facing identity.
6. **Lifecycle** — every write is idempotent and compared against the last written value:
   - start: `agent`; `thread_id` when `--thread` was given; `title` only when that thread
     already has runs;
   - first submit: `thread_id` immediately after `create_thread()`; `title` as soon as the
     run that created the thread is accepted (its input supplies the title), with the
     end-of-run read as a fallback;
   - later runs: nothing, because a thread's title is stable;
   - a changed thread identity (for example chat opened on a forked thread): rewrite
     `thread_id` and `title`;
   - exit (normal, Ctrl-Q, Ctrl-C, exception): clear all three in a `finally`.
7. **Failure policy**: every call is best-effort and wrapped. Diagnostics appear only under
   `TOOLANG_TMUX_DEBUG=1`. **`TOOLANG_TMUX_MARKS=0`** disables the feature entirely; there is
   no additional command-line flag. A failing tmux call never reaches the UI and never
   aborts the chat.
8. **Module API** (`cli/common/tmux.py`), narrow enough to swap the library later:

   ```python
   MARK_AGENT = "@toolang_agent"
   MARK_THREAD = "@toolang_thread_id"
   MARK_TITLE = "@toolang_thread_title"

   def clip_title(text: str, width: int = 60) -> str: ...
   def resolve_pane_marks(*, environment=..., pane_factory=...) -> PaneMarks | None: ...

   @dataclass(frozen=True, slots=True)
   class PaneMarks:
       target: str                      # pane id, e.g. "%3"
       def set(self, name: str, value: str) -> None: ...
       def clear(self, *names: str) -> None: ...
   ```

9. **Dependency**: add `libtmux` to `pyproject.toml` with a pinned version (it is pre-1.0)
   and update `uv.lock`. Accepted consequence: libtmux drives tmux by invoking the tmux
   binary, which happens at most three times per chat session.

## Files

- new `src/toolang/cli/common/tmux.py`
- `src/toolang/cli/toolang/commands/chat/base.py` — `thread_title` on `ChatClient`
- `src/toolang/cli/toolang/commands/chat/local.py`, `.../remote.py` — `thread_title`
- `src/toolang/cli/toolang/commands/chat/tui.py` — start, thread-created, run-finished, exit
- `src/toolang/cli/toolang/commands/chat/main.py` — wire marks into interactive and scripted paths
- `pyproject.toml`, `uv.lock`
- new `tests/cli/test_tmux_pane_marks.py`, `tests/cli/test_chat_thread_title.py`
- `docs/chat.md` — short "tmux pane marks" section

## Acceptance Tests

1. Environment without `TMUX`/`TMUX_PANE`: `resolve_pane_marks()` returns `None` and the
   injected pane factory records zero calls.
2. Fake environment plus fake pane: chat with `--thread term_x` writes `agent` then
   `thread_id`, and `title` when a title is available; each name exactly once.
3. New thread: no `thread_id` write before the first submit; `thread_id` written right after
   `create_thread()`; `title` written once that run is accepted, before it ends; no title
   lookup after it is published.
4. Exit paths (return and a raised exception inside the loop) clear all three names.
5. `clip_title` table test: newlines and tabs collapsed, CJK counted as two columns, clipped
   with a visible marker at 60 columns.
6. A scripted (non-tty) chat run writes and clears the same marks.
7. Gated integration test (`skipif` no tmux): inside a detached pane, assert
   `tmux show-options -p -t <pane> @toolang_thread_id` matches, then that it is gone after
   chat exits.
8. `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest`
   clean; live-provider tests untouched.

## Risks

- libtmux is pre-1.0: pin it, and keep every call behind `cli/common/tmux.py` so a swap or a
  hand-rolled client touches one file.
- Write points are synchronous subprocess calls (roughly 5–15 ms each): acceptable at start,
  first submit, and the title read at run acceptance and at run end; they must never enter a
  per-frame path.
- The founding run's input is the only title source and it is durable from acceptance on, so
  the mark is written then; the end-of-run read is a fallback for a lookup that failed or
  raced, not the primary write point.
- Pane ids are stable while the pane lives; writes to a dead pane fail harmlessly.

## Decisions

- Title clip width: 60 display columns.
- `@toolang_agent` value: agent name.
- Kill switch: `TOOLANG_TMUX_MARKS=0`; no extra flag.
- Pane options only; no window or session mirror.
- Marks are cleared on exit.
- The scripted chat path marks as well.
- Title timing: published when the founding run is accepted; the end-of-run read is a
  fallback.

## Follow-up Plan

Consume the marks: picker and key bindings, and optionally a dedicated tmux server with its
own configuration file. `/tmp/toomux` is a working prototype of that half (window options,
subprocess calls, hand-rolled server management); this plan supersedes it for the marks
themselves.

### Session naming rules to carry over

- Options are authoritative, names are cosmetic. Locate toolang panes by mark, and toolang
  sessions by a `@toolang_agent` session option, never by parsing a name; address targets by
  `session_id` / `pane_id`, which stay valid across renames.
- Only name what toolang creates: derive `toolang-<sanitized agent>` (lowercase, `[a-z0-9-]`,
  every other character replaced with `-`). If the derived name is already taken by a session
  toolang does not own, add a numeric suffix rather than renaming someone else's session.
- Respect tmux's own rewriting: session names silently turn `.` and `:` into `_` (verified:
  `-s x.y` creates `x_y`), so sanitise before using a derived name as a target too.
- With a dedicated server (`tmux -L <socket>`) the server is already toolang-scoped, so a plain
  name is enough and the mark remains the identifier.

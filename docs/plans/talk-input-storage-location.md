# Chat and Talk input history storage

Status: approved by the user's scope decisions on 2026-10-13.

## Goal and success criteria

Use canonical input-history filenames for Chat and Talk without importing history
from prior filenames. Preserve Chat's existing runtime location and Talk's
per-conversation location under `<root>/human/talk-input/`. Talk will no longer
persist or restore unsent composer drafts.

## Decisions and scope

- Interpret `human` as the literal directory beneath the selected Toolang root.
  Chat uses `context_layout(ctx).runtime`; Talk uses `context_root(ctx)`.
- Store Chat history at `<agent-runtime>/chat-input.jsonl`, replacing
  `<agent-runtime>/chat-history.jsonl` while preserving its per-agent and
  per-runtime-instance location and isolation.
- Store Talk history at
  `<root>/human/talk-input/<conversation-id>.jsonl`, keeping each conversation's
  accepted input history separate.
- Keep the existing JSONL record format, history limits, and recall behavior.
  Chat continues to recall accepted inputs with Ctrl+P/Ctrl+N. Talk continues to
  recall accepted inputs using its existing history bindings.
- Remove Talk's persisted draft support: do not save or restore the composer
  text across sessions and do not create `.draft.txt` files. Unsent input is
  intentionally lost when the Talk process exits.
- Do not import from or fall back to prior Chat or Talk paths/names, including
  Chat's `.runtime/chat-history.jsonl` and `.runtime/chat-input-history.jsonl`,
  Talk's `.runtime/talk-history/` files, or `.runtime/talk-v2/` data. Leave old
  files untouched; new canonical files start with no imported history.
- Chat and Talk input submission, message delivery, and other TUI behavior are
  unchanged. No Hub or message protocol changes are in scope.

## Design touchpoints

- `src/toolang/cli/toolang/commands/chat/main.py`: use the canonical Chat
  filename under the existing runtime directory and remove legacy import.
- `src/toolang/cli/toolang/commands/talk/__init__.py` and `tui.py`: use the
  canonical per-conversation path, remove legacy imports and draft persistence.
- `tests/unit/cli/test_chat_command.py`: verify the runtime-local Chat path,
  isolation, and no legacy imports.
- `tests/unit/cli/test_talk.py`, `test_talk_layout.py`, `test_talk_status.py`,
  and `test_talk_terminal_title.py`, plus
  `tests/system/cli/test_scrollback_resize.py`: verify Talk history paths, no
  legacy imports, no draft persistence, and unchanged in-session behavior.
- `CHANGELOG.md`: describe the new filenames/paths, lack of automatic history
  import, and removal of Talk draft restoration when implementing, using the
  repository's changelog workflow.

## Acceptance checks

- Chat writes and reloads accepted history at
  `<agent-runtime>/chat-input.jsonl`; the runtime location remains isolated by
  agent and runtime instance.
- Talk writes and reloads accepted history at
  `<root>/human/talk-input/<conversation-id>.jsonl`, isolated by conversation.
- No Chat or Talk data is imported from the previous paths or compatibility
  filenames, and those old files remain unchanged.
- Talk neither writes nor reads a draft file; an unsent composer value is not
  restored in a new Talk session.
- Existing send/recall, Chat command, and read-only Talk behavior remains intact.

## Risks

- Existing history is not automatically available at the new filenames; old
  files remain on disk but are ignored. Users who need recall must copy or rename
  them manually to the canonical path.
- Talk users lose recovery of unsent text when the process exits, as requested.

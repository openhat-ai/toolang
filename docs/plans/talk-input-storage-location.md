# Chat and Talk input history storage

Status: approved by the user's scope decisions on 2026-10-13.

## Goal and success criteria

Store accepted Chat and Talk input history under `<root>/human/` using the
canonical names `chat-input.jsonl` and `talk-input/<conversation-id>.jsonl`,
without importing history from previous paths or compatibility filenames. Talk
will no longer persist or restore unsent composer drafts.

## Decisions and scope

- Interpret `human` as the literal directory beneath the selected Toolang root.
  Chat uses `context_layout(ctx).root`; Talk uses `context_root(ctx)`.
- Store Chat history at `<root>/human/chat-input.jsonl`. This is one shared
  history file for Chat sessions using the same Toolang root, independent of
  agent or runtime instance.
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

- `src/toolang/cli/toolang/commands/chat/main.py`: use the root-level canonical
  Chat history path and remove legacy import behavior.
- `src/toolang/cli/toolang/commands/talk/__init__.py` and `tui.py`: use the
  canonical per-conversation path, remove legacy imports and draft persistence.
- `tests/unit/cli/test_chat_command.py`: verify the canonical Chat path and no
  legacy imports.
- `tests/unit/cli/test_talk.py`, `test_talk_layout.py`, `test_talk_status.py`,
  and `test_talk_terminal_title.py`, plus
  `tests/system/cli/test_scrollback_resize.py`: verify Talk history paths, no
  legacy imports, no draft persistence, and unchanged in-session behavior.
- `CHANGELOG.md`: describe the new paths, lack of automatic history import, and
  removal of Talk draft restoration when implementing, using the repository's
  changelog workflow.

## Acceptance checks

- Chat writes and reloads accepted input history at
  `<root>/human/chat-input.jsonl`; different agents and runtime instances under
  the same root use this same file.
- Talk writes and reloads accepted history at
  `<root>/human/talk-input/<conversation-id>.jsonl`, isolated by conversation.
- No Chat or Talk data is imported from the previous paths or compatibility
  filenames, and those old files remain unchanged.
- Talk neither writes nor reads a draft file; an unsent composer value is not
  restored in a new Talk session.
- Existing send/recall, Chat command, and read-only Talk behavior remains intact.

## Risks

- Existing history is not automatically available at the new paths; old files
  remain on disk but are ignored. Users who need old recall data must handle it
  manually.
- Talk users lose recovery of unsent text when the process exits, as requested.
- Chat history becomes shared across agents and runtime instances that use the
  same root, matching the requested canonical path.

# Simplified CLI input history layout

Status: Proposed; awaiting human approval. Definition only.

## Goal and scope

Use readable, stable input-history paths for the human Talk CLI and agent Chat
CLI. Talk's local input history is shared by all human viewers of a conversation
within a Toolang root; it does not use viewer or Hub dataset identity directories.
Chat retains its existing agent/runtime scope.

This covers input recall, Talk draft persistence, and migration of their existing
files. It does not move conversation messages or execution records, change Hub
sender/access semantics, change terminal pane context, or add file locks.
The separate [file lock definition](file-lock-layout.md) governs existing locks.

## Paths and behavior

| Purpose | Path |
| --- | --- |
| Talk input history | `<root>/.runtime/talk-history/<conversation-id>.jsonl` |
| Talk draft | `<root>/.runtime/talk-history/<conversation-id>.draft.txt` |
| Chat input history | `<agent-runtime>/chat-history.jsonl` |

Use the resolved canonical conversation ID directly, validated with the existing
`teaming.schemas.conversation_id` contract before constructing a filename.
Do not hash it. IDs currently contain only `dm_` or `gc_` and eight safe ID
characters. Different conversations and roots remain isolated; viewer and Hub
dataset changes within a root intentionally share files for the same ID.

Resolve paths at the CLI call sites. Pass concrete history and draft paths into
TalkTui instead of a directory from which it invents filenames. Retain JSONL
record format, recall/compaction limits, malformed-record handling, read-only
behavior, and existing draft restoration and saving. Chat still uses
`AgentLayout.runtime`, including the existing instance-specific runtime behavior.
Keep `talk_context` and its identity calculation for terminal pane coordination;
removing identity from persistence does not remove messaging identities.

## Migration

Before upgrading, close interactive Talk and Chat sessions sharing the affected
paths. On first access, if a new destination does not exist, copy its legacy
file atomically and leave the source untouched. An existing destination,
including an empty one, always wins; do not merge or repeatedly import.

For Chat, the source is the same runtime's `chat-input-history.jsonl`. For Talk,
use only the current connection/viewer's legacy
`talk-v2/<identity>/<conversation-hash>/input.jsonl` and `draft.txt`, computing
their old identity solely for migration. Migrate history and draft independently.
Do not scan or merge other viewer/dataset directories. If no source exists,
retain normal lazy creation. Migration failure must leave both source and any
existing destination intact and report the I/O failure.

## Touchpoints and verification

- `cli/toolang/commands/talk/__init__.py` and `talk/tui.py`: concrete paths and
  legacy import; `cli/common/messaging.py` retains pane-context behavior.
- `cli/toolang/commands/chat/main.py` and CLI-owned migration helpers: renamed
  Chat destination and legacy import using existing atomic file utilities.
- Talk persistence, Chat command/history, and messaging-context tests;
  `docs/layout.md` and the canonical changelog document the implemented change.

Acceptance scenarios:

- Talk restores/saves input and draft at readable canonical ID paths; two
  conversations and roots remain isolated, and changing viewers does not change
  persistence paths.
- Invalid IDs fail before filesystem access; pane context and Hub access checks
  retain their current behavior.
- Chat uses the new filename under the selected agent/instance runtime.
- Legacy history and drafts import independently once; existing destinations
  win, other identities are untouched, and failed imports preserve data.
- Recall, compaction, malformed lines, and read-only draft behavior remain intact.

Run the repository's default verification for implementation. Risks are intentional
sharing across humans/datasets, incompatible simultaneous old/new sessions, and
legacy history belonging to other identities remaining outside the new history.
No history-layout decisions remain open; human approval is required before
implementation. Talk command arguments remain unchanged.

# CLI input history paths

Status: Approved on 2026-10-12.

## Paths

| Purpose | Destination |
| --- | --- |
| Human Talk input history | `<root>/.runtime/talk-history/<conversation-id>.jsonl` |
| Talk draft | `<root>/.runtime/talk-history/<conversation-id>.draft.txt` |
| Agent Chat input history | `<agent-runtime>/chat-history.jsonl` |

Use resolved IDs validated by `teaming.schemas.conversation_id`, without hashing
or viewer/dataset directories. Root and conversation boundaries remain; humans
and datasets within one root intentionally share files for the same ID. Chat
retains `AgentLayout.runtime`, including instance scope.

CLI call sites resolve concrete history/draft paths. Preserve record formats,
recall/compaction limits, malformed-record handling, and read-only draft behavior.
Talk arguments, Hub sender/access semantics, terminal pane context, and execution
records stay unchanged. No new file locks or concurrent-input guarantees are
introduced; existing append/compaction and last-writer draft behavior remain.
[Lock decisions](file-lock-layout.md) are separate from input history.

## Migration

Close old interactive sessions before upgrading. On first access, import a
legacy file only if its destination is absent. Prepare a complete temporary copy
in the destination directory, then publish it without replacement (for example,
with an exclusive hard-link creation). If another process creates the destination
first, keep that file and discard the temporary copy. A check followed by
`os.replace` is insufficient. An existing empty file also wins.

- Chat source: the same runtime's `chat-input-history.jsonl`.
- Talk sources: the current connection/viewer's
  `talk-v2/<legacy-identity>/<conversation-hash>/{input.jsonl,draft.txt}`.
  Compute old identity only for import; do not scan or merge other identities.

Import history and draft independently. Leave legacy sources untouched; clean up
only the importer's temporary files. Missing sources retain lazy creation. Report
I/O errors without damaging either source or an existing destination.

## Implementation and acceptance

Touchpoints: `cli/toolang/commands/talk/{__init__,tui}.py`,
`cli/toolang/commands/chat/main.py`, a CLI-owned import helper using standard file
operations, and their persistence tests. Keep `cli/common/messaging.py` pane
context behavior. Update [layout](../layout.md) and the canonical changelog.

Acceptance: canonical ID paths and invalid-ID rejection; root/conversation and
agent/instance scope; unchanged recall/draft behavior; independent legacy imports;
existing/empty destinations win; simultaneous imports never overwrite a winner;
failures preserve source/destination and remove temporary copies. Run default
repository verification for implementation.

Risks: intentional cross-viewer/dataset sharing, concurrent input remaining best
effort, and other identities' old history remaining outside the new paths.
No open design questions remain; approval is required before implementation.

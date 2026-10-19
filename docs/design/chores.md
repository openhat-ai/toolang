# Chores

A chore defines one-off or repeated work in `chores/<name>.md` or an inline
`chore name:` declaration in `agent.too`. Files directly inside `chores/` are
current definitions. There are no draft, ready, or archive stages.

```markdown
---
title: Clean temporary files
---
Clean temporary files and summarize the result.
```

Without `schedule`, a chore runs once for each body revision. Add an explicit
RRULE for repeated work, for example `schedule: FREQ=HOURLY;INTERVAL=1`.
Inline definitions use the same rules:

```toolang
chore cleanup:
  schedule = FREQ=DAILY;INTERVAL=1
  Clean temporary files and summarize the result.
```

Names are case-sensitive declaration names or filename stems; refs use
`chore/name`, and threads use `chore_<8-character ID>`. Every name derives its ID
from the complete name and agent. Title/body/schedule/location edits preserve
identity; different-name collisions are rejected. Inline definitions shadow home
files, later inline definitions win, and root files are ignored. See [ID rules](ids.md).

Definitions hold no execution state. The `chores` table in `.runtime/runs.db`
records revisions, pending work, active runs, and schedule checkpoints. Removing
a definition stops future dispatch but preserves its checkpoint and history.
Restoring an unchanged completed, failed, or canceled one-off does not rerun it.
Deleting a definition does not cancel an already running occurrence.

## Activation and manual work

The Chore driver must be enabled to dispatch definitions; standalone scripts
have no persistent drivers. [Drivers](drivers.md#chore-scheduling) owns
scheduling, admission, and recovery.

Authoring commands edit home files; inline definitions are edited in their
`.too` source. A manual run requests another occurrence, including retrying a
finished one-off. Cancel removes pending work or cancels the active occurrence
without deleting its definition or future schedule. Copying a definition under
a different name creates independent progress and context.

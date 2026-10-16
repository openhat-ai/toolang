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

Names are case-sensitive declaration names or filename stems. The ref is
`chore/cleanup`; its ID is eight lowercase Base32 characters derived from the
exact name and agent. Its thread is `chore_<id>`. Renaming or changing agents
creates a new identity; editing the body, title, schedule, or source location
preserves identity. Inline declarations shadow home files; later inline
declarations shadow earlier ones. Root chore files are ignored. Different names
producing the same ID are rejected. See the [ID model](ids.md) for the codec.

Definitions hold no execution state. The `chores` table in `.runtime/runs.db`
records revisions, pending work, active runs, and schedule checkpoints. Removing
a definition stops future dispatch but preserves its checkpoint and history.
Restoring an unchanged completed, failed, or canceled one-off does not rerun it.
Deleting a definition does not cancel an already running occurrence.

## Commands

`too` is an alias for `toolang`.

```sh
too alice chore list
too alice chore new cleanup
too alice chore edit cleanup
too alice chore run chore/cleanup
too alice chore cancel cleanup
too alice chore delete cleanup
```

Selectors accept the name, ref, or ID. `new` generates `auto_<8 chars>` when the
name is omitted. That filename format is reserved for automatic names; all other
names use the manual identity domain. Both domains produce eight-character IDs.
To copy a definition, use `new` or copy a home file under a different name;
the new name creates independent progress and context. Authoring commands edit
home files; `list` also shows effective inline definitions. Edit inline
definitions in their `.too` source.
`run` requests a manual occurrence, including retrying a failed or completed
one-off. `cancel` cancels active or pending work without deleting the definition.
Repeated chores keep their future schedule.

The chore driver must be enabled to dispatch work:

```sh
too serve alice --enable chat,talk,chore
```

Standalone scripts enable no persistent drivers, even when their source contains
inline chores. Use `serve` to activate them. See [execution drivers](work.md).

## Transition from tasks and jobs

The `task` command/declaration, `/tasks`, `/jobs`, authored IDs, and stage commands
are removed. Use named chores in the flat directory and explicit schedules for
repeated work. No old directories, IDs, or `jobs.db` checkpoints are migrated or
imported. Existing files and databases are left untouched; a definition added
to the new model receives a checkpoint under its name-derived ID.

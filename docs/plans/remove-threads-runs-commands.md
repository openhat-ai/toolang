# Remove Threads And Runs Commands

## Status

Proposed. Human confirmation is required before implementation.

## Goal

Make `inspect` the only top-level CLI surface for listing durable Threads and
Runs by removing the redundant `threads` and `runs` commands.

## Success Criteria

- `threads` and `runs` are no longer registered top-level commands or routing
  keywords.
- `inspect threads`, `inspect runs`, and `inspect THREAD runs` remain the
  supported Thread and Run collection queries for resident, roaming, and
  visiting Agents.
- Normal help, target help, completion, and routing metadata no longer expose
  the removed commands.
- The removal has no compatibility aliases or deprecation-only hidden commands.
- Execution history, `inspect` semantics, control commands, and HTTP APIs remain
  unchanged.

## Current Behavior

The CLI has two overlapping history-list surfaces:

```text
too AGENT threads
too AGENT runs [--thread THREAD]
too AGENT inspect threads
too AGENT inspect runs
too AGENT inspect THREAD runs
```

The older commands are registered under `Inspection Commands`, have dedicated
resident, roaming, and visiting routing entries, and render human tables
through `RunHistory`. The newer `inspect` collection subjects use the unified
records projector and also support canonical JSON.

The surfaces are not option-for-option equivalent:

| Removed form | Replacement | Intentional difference |
| --- | --- | --- |
| `threads` | `inspect threads` | changes from at most 50 rows to an unbounded collection |
| `runs` | `inspect runs` | changes from at most 50 rows to an unbounded collection and adds the `STEPS` summary column |
| `runs --thread T` | `inspect T runs` | uses a scoped subject and adds the `STEPS` summary column |
| `threads --origin/--channel/--status` | none | filters are removed |
| `runs --status` | none | the filter is removed |

The old commands succeed with an empty table when `runs.db` does not exist.
Inspect collections instead report `execution history not found: AGENT`. The
replacement intentionally adopts the existing `inspect` behavior.

## Command Removal

Remove the public `threads` and `runs` commands directly. Do not retain aliases,
hidden commands, forwarding shims, warnings, or a deprecation period. After the
change, forms such as these are unsupported:

```text
too alice threads
too alice runs
too alice runs --thread term_ab12
too brice/alice threads
too ./agent.too runs
```

The supported replacements are:

```text
too alice inspect threads
too alice inspect runs
too alice inspect term_ab12 runs
too brice/alice inspect threads
too ./agent.too inspect runs
```

The removed filtering flags are not added to `inspect`. Consumers that require
filtered data can use the canonical array from `inspect ... --json` and filter
it outside Toolang. This feature does not add limits, filters, sorting options,
or a compatibility mode to the inspect grammar.

Remove `threads` and `runs` from:

- top-level Typer registration and the `Inspection Commands` display order;
- the placement-aware command routing registry;
- resident, roaming, and visiting target help and completion derived from those
  registries.

The words cease to be reserved top-level command names. Generic target and
runnable resolution therefore applies where appropriate. In particular, a
roaming source may invoke an authored runnable named `threads` or `runs`
without an `agic:`, `flow:`, or `runnable:` disambiguator. This does not change
the exact `threads` and `runs` tokens reserved inside the `inspect` subject
grammar.

## Presentation And Data Semantics

No inspect output changes are part of this feature. The replacements retain
their current behavior:

- `inspect threads` renders `THREAD`, `TITLE`, `RUNS`, `STATUS`, and `UPDATED`;
- `inspect runs` renders `RUN`, `THREAD`, `TITLE`, `STEPS`, `STATUS`, and
  `CREATED`;
- `inspect THREAD runs` renders `THREAD RUN`, `TITLE`, `STEPS`, `STATUS`, and
  `CREATED`;
- `--json` emits unbounded canonical record arrays;
- ordinary visibility and durable ordering remain unchanged.

Individual Thread, Run, Control, Step, and field Pointer inspection is
unaffected. `RunHistory.list_threads()` and `RunHistory.list_runs()` remain
internal APIs because other execution and control paths still use
`RunHistory`; only the two CLI adapters are removed.

## Scope And Compatibility

Included:

- direct removal of the public `threads` and `runs` command functions;
- removal of their command registration, help-panel ordering, and routing
  specifications;
- cleanup of imports and private helpers made unused solely by that removal;
- replacement examples and migration notes in current CLI documentation;
- regression tests for the reduced command surface and retained inspect routes.

Excluded:

- changes to any `inspect` subject, projector, output, option, visibility, or
  missing-store behavior;
- filters, limits, pagination, aliases, warnings, or deprecation machinery;
- changes to `RunStore`, `RunHistory`, persistence, schemas, or records;
- changes to Thread/Run control commands such as `chat`, `retry`, `rerun`,
  `rewind`, or `fork`;
- changes to execution or HTTP Thread and Run resources.

This is intentionally a breaking CLI change. Scripts must replace the removed
commands with inspect collections and must separately handle any former
filtering or 50-row window. No stored data or Python execution API is removed.

## Design Touchpoints

- `src/toolang/cli/toolang/main.py`: remove both command registrations and their
  names from the inspection-panel order.
- `src/toolang/cli/toolang/routing.py`: remove both placement-aware command
  specifications so selector routing and target help no longer recognize them.
- `src/toolang/cli/toolang/commands/thread.py`: remove `threads_command()` and
  `runs_command()` plus imports and helpers used only by those functions; retain
  the remaining Thread and Run control commands.
- `tests/unit/cli/test_cli_routing.py`: update the public command order and
  placement grammar, and cover that the removed words are no longer static
  roaming commands.
- `tests/integration/cli/test_local_core_commands.py`: replace legacy command
  coverage with supported inspect forms and assert that old invocations and
  flags are unavailable.
- `tests/system/cli/test_cli_entry_points.py`: update command-surface assertions
  if affected by the registration change.
- `docs/api.md`: remove legacy typical and roaming examples, document inspect as
  the sole list surface, and record the intentional filter, limit, and
  missing-store differences.

Approved historical plans are not rewritten. No execution, database, or HTTP
module requires a change.

## Acceptance Tests

1. Root and target help omit `threads` and `runs`, while `inspect` remains under
   `Inspection Commands` in the existing order.
2. The Typer registration set and routing registry contain neither removed
   command, including resident, roaming, and visiting placement routes.
3. Legacy resident and visiting invocations are rejected without forwarding,
   compatibility warnings, or execution-history reads.
4. A roaming source with an authored runnable named `threads` or `runs` routes
   to that runnable rather than to an Agent history command.
5. `inspect threads`, `inspect runs`, and `inspect THREAD runs` still work for
   every currently supported Agent placement and retain exact human and JSON
   behavior.
6. The former filter flags and 50-row window receive no inspect equivalent;
   inspect collections remain unbounded and reject unsupported options through
   normal Click usage errors.
7. An Agent without `runs.db` receives the existing inspect missing-history
   error rather than the removed commands' empty-table behavior.
8. `chat`, `steer`, `cancel`, `retry`, `rerun`, `rewind`, and `fork` registration
   and routing remain unchanged.
9. The complete default verification passes.

## Risks And Open Questions

Existing scripts that use the old names, filters, bounded output, or
missing-store success will break immediately. This is accepted by the direct
removal request and is documented with explicit replacements; preserving any
of those behaviors would require a separately approved inspect feature. There
are no open product questions in this definition.

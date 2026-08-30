# Reshape CLI Inspection Commands

## Status

Approved for implementation on 2026-08-30.

## Goal

Give the top-level `Inspection Commands` panel one clear vocabulary:

- `inspect` is the sole interface for inspecting durable execution records;
- list commands use the verb `List` in help;
- installed plugin families have direct plural inventory commands.

## Success Criteria

- `threads` and `runs` are no longer registered top-level commands or routing
  keywords.
- `inspect threads`, `inspect runs`, and `inspect THREAD runs` remain the
  supported Thread and Run collection queries for resident, roaming, and
  visiting Agents.
- New `catalogs` and `toolsets` commands list installed model-catalog and
  toolset plugins, respectively.
- The existing `tools` command and its filtering behavior remain public.
- List-oriented help uses `List`; `Inspect` is reserved for `inspect`.
- Normal help, target help, completion, and routing metadata expose the same
  final command surface.
- Removed commands receive no compatibility aliases or deprecation-only hidden
  commands, and new commands receive no singular or nested-list aliases.
- Execution history, inspect semantics, plugin loading, control commands, and
  HTTP APIs remain unchanged.

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

The same panel exposes installed model adapters and sandboxes but has no direct
inventory for model-catalog or toolset plugins. It lists leaf tools through
`tools`, although the command help currently describes those leaf resources as
installed. Most list-oriented entries use `Inspect` in their help text.

The two history surfaces are not option-for-option equivalent:

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

## Final Command Surface

After the change, `Inspection Commands` contains these commands in this exact
order and with these exact help descriptions:

```text
inspect      Inspect execution subjects.
caps         List caps.
models       List models.
providers    List model providers.
tools        List tools.
catalogs     List installed model catalogs.
adapters     List installed model adapters.
toolsets     List installed toolsets.
sandboxes    List installed sandboxes.
```

`installed` describes discoverable plugin entry points only. It therefore
applies to catalogs, adapters, toolsets, and sandboxes, but not to leaf tools
assembled from toolsets. `models` and `providers` describe the merged model
catalog view rather than installed plugins. `caps` retains its existing
agent-aware behavior; only its help description changes.

`inspect` remains in the panel even though it is not itself a list command. The
removed `threads` and `runs` names do not leave empty positions in the order.

## Thread And Run Command Removal

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

## Plugin Inventory Commands

`too catalogs` lists the `toolang.model_catalog` entry-point group, sorted by
entry-point name:

```text
CATALOG  SOURCE
```

`too toolsets` lists the `toolang.toolset` entry-point group, sorted by
entry-point name:

```text
TOOLSET  SOURCE
```

Both commands show the existing `built-in` or `external` source provenance.
They require no Agent target, options, configuration merge, plugin
instantiation, or setup refresh. Empty groups print `No catalogs found.` and
`No toolsets found.`, respectively, and exit successfully.

`too adapters`, `too tools`, and `too sandboxes` retain their current output
and options. In particular, adapters keep `--filter` and `--json`, tools keep
`--filter`, and sandboxes remain a direct two-column plugin listing.

Do not add `catalog`, `toolset`, `catalogs list`, or `toolsets list` aliases.

## Inspect Presentation And Data Semantics

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
- addition and no-target routing of `catalogs` and `toolsets`;
- plugin entry-point inventory output for the two new commands;
- the final inspection-panel ordering and exact help descriptions above;
- cleanup of imports and private helpers made unused solely by the removal;
- replacement examples and migration notes in current CLI documentation;
- regression tests for the final command surface, retained inspect routes, and
  plugin listings.

Excluded:

- changes to any `inspect` subject, projector, output, option, visibility, or
  missing-store behavior;
- filters, limits, pagination, aliases, warnings, or deprecation machinery;
- plugin installation, enablement, configuration, loading, or setup changes;
- new options for catalogs, toolsets, or sandboxes;
- changes to `RunStore`, `RunHistory`, persistence, schemas, or records;
- changes to Thread/Run control commands such as `chat`, `retry`, `rerun`,
  `rewind`, or `fork`;
- changes to execution or HTTP Thread and Run resources.

This is intentionally a breaking CLI change. Scripts must replace the removed
commands with inspect collections and must separately handle any former
filtering or 50-row window. No stored data or Python execution API is removed.

## Design Touchpoints

- `src/toolang/cli/toolang/main.py`: remove the Thread and Run list
  registrations, add both plugin inventory commands, and apply the final panel
  order and help text.
- `src/toolang/cli/toolang/routing.py`: remove the placement-aware Thread and
  Run list specifications and add no-target specifications for `catalogs` and
  `toolsets`.
- `src/toolang/cli/toolang/commands/thread.py`: remove `threads_command()` and
  `runs_command()` plus imports and helpers used only by those functions;
  retain the remaining Thread and Run control commands.
- `src/toolang/cli/toolang/commands/plugin.py`: reuse the plugin-info listing
  path for catalog and toolset entry-point groups.
- `tests/unit/cli/test_cli_routing.py`: cover the final public order, help text,
  placement grammar, and removal of the old static roaming commands.
- `tests/integration/cli/test_local_core_commands.py`: replace legacy command
  coverage with supported inspect forms, assert old invocations are
  unavailable, and cover both new plugin inventories.
- `tests/system/cli/test_cli_entry_points.py`: update command-surface assertions
  if affected by the registration change.
- `docs/api.md`: remove legacy typical and roaming examples, document inspect
  as the sole history-list surface, and include the final plugin inventory
  commands where the public command surface is enumerated.

Approved historical plans are not rewritten. No execution, database, or HTTP
module requires a change.

## Acceptance Tests

1. Root and target help omit `threads` and `runs`; root help renders the exact
   final `Inspection Commands` order and descriptions defined above.
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
8. `too catalogs` and `too toolsets` list the correct entry-point groups with
   source provenance, deterministic ordering, exact headers, and defined empty
   messages without loading plugin factories.
9. Existing adapter options, tool filtering, and sandbox listing behavior are
   unchanged, apart from the top-level help descriptions.
10. The new plural commands accept no Agent target and no singular or nested
    aliases are registered.
11. `chat`, `steer`, `cancel`, `retry`, `rerun`, `rewind`, and `fork`
    registration and routing remain unchanged.
12. The complete default verification passes.

## Risks And Open Questions

Existing scripts that use the old history names, filters, bounded output, or
missing-store success will break immediately. This is accepted by the direct
removal request and is documented with explicit replacements; preserving any
of those behaviors would require a separately approved inspect feature.

Catalog names identify installed catalog plugins, not the merged model catalog
snapshot used by `too models`. Toolset names identify installed plugin entry
points, while `too tools` lists the leaf tools assembled from them. The exact
help descriptions and table headers make both distinctions explicit. There are
no open product questions in this definition.

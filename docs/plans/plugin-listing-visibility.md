# Plugin listing visibility

Status: Approved by the user for implementation in PR #561.

## Goal and success criteria

Make `too tools` and `too toolsets` inspect installed plugins directly, with
internal toolsets hidden by default and available through `--all`. Module
names continue to match registered plugin names.

## Scope and decisions

- Add `--all` to both commands. Default output excludes internal toolsets
  using the existing `is_internal_toolset_name` rule; this hides `_toolang`
  and its leaf tools. `me` remains visible.
- `too toolsets` reads entry-point metadata without constructing toolsets.
- `too tools` loads registered toolsets once through the existing plugin
  loader and builds the existing tool query dataset from their leaf tools.
  It does not construct `AgentSetup`, use `load_setup_tools`, or recompute
  the agent's effective tool allow policy. This command describes installed
  tools; runtime policy and agent-info summaries keep their existing meaning.
- Preserve current command targeting and plugin configuration precedence.
  Resolve root/default-agent configuration at the CLI boundary and pass only
  merged toolset configuration to the loader. No model setup or model discovery.
  Both commands remain untargeted; no new agent-target syntax is introduced.
- Apply visibility and `--query` to the same loaded dataset. An explicit query
  cannot reveal internal tools without `--all`. Counts describe displayed
  tools and distinct displayed toolsets, never a second setup calculation.
- Preserve columns, sorting, empty-result wording, and query diagnostics.
  `--all` changes visibility only; it does not grant runtime tool access.

## Implementation layout

- Keep one implementation module or package per registered plugin identity.
  Rename `toolsets/filesystem.py` to `fs.py` and `service_use.py` to `service.py`.
- Use the three catalog implementations in `plugin/catalogs/models_dev/`,
  `plugin/catalogs/ollama.py`, and `plugin/catalogs/llama_cpp.py`, each exporting
  `create_model_catalog`. Keep the upstream `plugin/adapters/` implementations
  and setup-owned model routes. Shared parsing and local helpers stay separate.
- Group Docker implementation, CLI helpers, and guest bootstrap resources in
  `sandboxes/docker/`; preserve its factory entry point and guest filenames.
- Keep runtime-owned `_toolang` and `me` in `execution/tools`, with their names
  unchanged. Retain all 17 registrations and update imports and package resources.

## Loader organization

Consolidate the typed `create_channel`, `create_sandbox`, `load_model_adapters`,
and `load_model_catalogs` functions in `plugin/loading.py`. Remove the four
thin channel, sandbox, adapter, and catalog loading modules and update callers. Move shared plugin data types
to `plugin/types.py`. Keep toolset registration, identity validation, duplicate
checks, leaf-tool wrapping, and selection in `plugin/toolsets/loading.py`.
Preserve entry points, configuration copying, missing-module handling, ordering,
and catalog selection exactly; do not add another loader abstraction.

## Implementation touchpoints

- `src/toolang/cli/toolang/commands/plugin.py`: CLI options, explicit config
  inputs, plugin-backed dataset, visibility filtering, and summary counts.
- Reuse `plugin.loading`, `plugin.toolsets.loading`, `plugin.toolsets.collections`,
  and `base.utils.tools`; do not introduce another registry or query schema.
- `src/toolang/setup/tools.py` and `tests/unit/setup/test_tool_loading.py`:
  retire the unused one-shot setup calculation; move coverage to CLI inventory tests.
- `tests/integration/cli/test_local_core_commands.py`: listing and query checks.
- `tests/unit/cli/test_cli_routing.py` and query-discovery tests if option
  routing or help expectations require updates.
- `plugin/loading.py`, `plugin/types.py`, the four removed family loaders,
  and their callers/tests: structural loader consolidation.
- `docs/plugins.md`: describe inventory scope, loader ownership, and `--all` examples.

## Acceptance tests

1. Default toolsets include public built-ins and external plugins but exclude
   `_toolang`; `--all` includes it exactly once.
2. Default tools exclude `_toolang/*`; `--all` includes them. `me/*` remains
   visible in both modes. Displayed totals match visible rows in each mode.
3. An internal-only query returns no match by default and the selected tools
   with `--all`; malformed queries retain their existing diagnostic.
4. Toolset listing never invokes factories. Tool listing constructs each
   selected installed toolset only once, passes its merged config, and succeeds
   when setup construction and model discovery are made to fail in the test.
5. Agent tool allow restrictions do not change the installed inventory;
   runtime selection and agent-info summaries remain unchanged.
6. Help documents `--all` for both commands; default verification passes.

## Risks and open questions

The intentional compatibility change is that `too tools` reports installed
plugin tools instead of the agent's allow-filtered effective tools. Preserve
agent-info/runtime views for effective capability inspection. No open questions.

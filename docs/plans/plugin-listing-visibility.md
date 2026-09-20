# Plugin layout and inspection scopes

Status: Approved by the user for implementation in PR #561, including the
correction to installed-plugin versus effective-resource inspection and the
shared diagnostic meaning of resource `--all`.

## Goal and success criteria

Align implementation names with registered plugins and distinguish local plugin
inventory from capabilities available in a root or resident-agent context.
Inspection must reuse its owning publication or metadata source, not calculate
an independent effective setup.

## Command contracts

| Commands | Without an agent | With a resident agent | Source |
| --- | --- | --- | --- |
| `adapters`, `toolsets`, `catalogs`, `sandboxes`, `channel list` | Locally installed plugin identities and distribution sources | Rejected | Entry-point metadata; no factories, configuration, or setup |
| `caps` and kind-specific lists | Root-shared capabilities allowed by root policy | Root-shared plus agent-owned capabilities under effective allow and existing scope precedence | Capability state/source |
| `tools` | Tools effective under root configuration and allow policy | Tools effective under root configuration overlaid by agent configuration and allow policy | Published setup tool collection |
| `models`, `providers` | Ready, allowed resources under root configuration | Ready, allowed resources under the selected agent's effective configuration | Published setup model catalog |

Omitting an agent never selects `agents/default`. Root model caches live under
root `.setup/models`; agent caches live under home `.setup/models`, with
distinct scope identities in setup revisions. Agent resource inspection uses
`too [AGENT] <command>`; plugin commands reject both prefix and postfix agent
arguments. Existing visiting/roaming restrictions remain unchanged.

## Scope and precedence

- Plugin inventories include installed entries even when their dependencies or
  factories cannot load. Keep sorted identities, source labels, empty messages,
  and adapter JSON. Remove `AgentSetup.adapter_sources`; runtime adapter
  instances and provenance used for cache invalidation remain setup-owned.
- `tools` reads `setup.tools` and its existing query views. Do not reconstruct
  policy, factories, sources, or counts through a second loader. Root context
  uses root inputs only; agent context merges root then agent inputs. Preserve
  existing allow replacement semantics and runtime-internal tools.
- `tools` and `toolsets` hide internal toolsets such as `_toolang` by default;
  `--all` includes them. `me` is not internally hidden, but remains subject to
  tool allow policy in the default view. `tools --all` reads the complete
  pre-allow tool collection retained by that same setup, including internal
  and allow-excluded tools. An internal-only query needs `--all`; counts
  describe displayed rows.
- Resource `--all` consistently means the complete diagnostic view for the
  selected scope. `models/providers --all` include unready and allow-excluded
  catalog entries, and empty providers. `--all` does not change configuration
  scope, undo plugin configuration, combine overridden catalog files, or grant
  execution permissions. Querying and exporting read the same setup version.
  No extra factories, policy calculations, route or readiness inference.
  An empty allow list selects no entries; it must not fail query parsing or
  prevent inspection of the full collection.
- Tools currently have no independent readiness protocol. The complete tool
  view contains all leaves supplied by successfully loaded toolsets; it cannot
  invent leaf metadata for an unloadable plugin. Plugin inventory still lists
  that plugin's installed entry point.
- Select one complete static model catalog: command `--catalog`, effective
  `TOOLANG_MODEL_CATALOG`, selected agent's `catalog.json`, root `catalog.json`,
  then packaged catalog. Environment precedence remains process, agent dotenv,
  root dotenv. Omit agent inputs entirely for root inspection. Static files
  replace one another rather than merge; enabled additional catalog plugins
  retain their existing composition. Invalid selected sources fail rather
  than silently fall back.
- Caps apply existing cap-kind allow policy by default; `--all` includes
  allow-excluded resources in the same scope. Agent lists consume State's
  published main-module effective caps and full cap index. Root lists capture
  only root source and apply the shared State policy selector once. Aggregate
  and kind-specific lists, including standalone `caps`, share this behavior.
  Preserve home/here precedence: `--all` does not resurrect shadowed definitions
  or include other agents' resources. Runtime grants are unchanged.
- Tools/models/providers need a resident home, not a running or parsed agent
  program. Inspect setup with default/compact model validation disabled, as in
  model inspection. Keep existing options and query diagnostics.

## Output states

| Resource | Default columns | Additional `--all` columns |
| --- | --- | --- |
| Caps | Existing identity, description, scope, form, source | `ALLOWED` |
| Tools | Existing identity, description, source | `ALLOWED`, `INTERNAL` |
| Models | Existing identity, `AVAILABLE` readiness, capabilities and prices | `ALLOWED`, route `REASON` |
| Providers | Existing readiness counts, routes and environment | `ALLOWED MODELS` count alongside readiness counts; preserve `REASON` |
| Toolsets | Identity and distribution source | `INTERNAL` |

`ALLOWED` is independent of readiness. Setup resolves model allow membership
against the complete catalog before selecting ready runtime models, so unready
but allowed models remain distinguishable from excluded ones. Provider full-view
readiness and allow counts each use the full provider model count as denominator;
default counts describe only the ready, allowed rows. Runtime-internal tools are
allowed independently of user allow policy. Caps/tools have no separate readiness
protocol. Keep model/provider JSON as raw catalog exports, without inspection
columns or secrets. Query filters and totals operate on the selected view.
Provider reasons include unready models even when other models are ready.
Cap preparation progress identifies root and agent-home layers separately.

## Implementation layout

- Keep all 17 plugin registrations and one implementation module or package per
  identity. Rename `toolsets/filesystem.py` to `fs.py` and `service_use.py` to
  `service.py`; runtime-owned `_toolang` and `me` stay in `execution/tools`.
- Keep upstream `plugin/catalogs` and `plugin/adapters`, with each catalog
  exporting `create_model_catalog`. Preserve setup-owned model routes/cache.
- Group Docker implementation, CLI helpers, and packaged guest bootstrap files
  under `sandboxes/docker/`; preserve its factory and guest filenames.
- Consolidate typed channel, sandbox, adapter, and catalog factories/loaders in
  `plugin/loading.py`; remove four thin family loaders. Shared plugin identity
  and provenance records live in `plugin/types.py`.
- Keep toolset-specific validation, duplicate checks, and wrapping in
  `plugin/toolsets/loading.py`. Preserve configuration copying, missing-module
  handling, order, and catalog selection. Retire the redundant setup tool loader.

## Implementation touchpoints

- CLI plugin/model commands, registration, routing, and optional-agent help.
- Setup watcher/types: remove inventory-only adapter source publication; retain
  full tools and model allow membership alongside effective runtime resources.
- State cap selection and CLI cap commands: reuse policy, root-only source reads,
  complete/effective views, and status columns.
- Plugin modules, factories, callers, package resources, and their tests.
- CLI integration tests for metadata-only inventories and scoped resources;
  setup tests for effective publication; routing/help tests.
- `docs/plugins.md`, `docs/tools.md`, `docs/models.md`, `docs/caps.md`,
  `docs/api.md`, README, and conflicting model-plan statements.

## Acceptance tests

1. Every plugin command lists metadata without setup/factory calls even when
   root configuration/catalog files are invalid; agent targeting is rejected.
2. Default/`--all` tool and toolset visibility, queries, sources, ordering, and
   displayed counts remain correct, including an internal-only inventory.
3. Root tools ignore default-agent files. Selected-agent tools merge plugin
   config and honor effective allow by default; `--all` bypasses allow for
   inspection and includes internal tools. Empty allow yields no default rows
   and the complete tool set with `--all`; runtime `setup.tools` stays filtered.
4. Tool inspection reads published query views without rediscovering plugins.
5. Caps default/`--all` honor root/agent scope and policy, same-name precedence,
   and kind-specific allow fields; no other agent's resources leak. Aggregate,
   per-kind, and standalone lists share the behavior.
6. Models and providers share static catalog precedence, replacement semantics,
   root/agent isolation, and default/`--all` policy behavior. Root cache writes
   never create a default-agent home; root and agent versions remain distinct.
7. Resident tools use layout-only preparation; missing agents and unsupported
   target forms fail without creating agent homes. Help performs no loading.
8. Full-view columns distinguish allowed/excluded, ready/unready, and internal
   tools; default columns remain concise. Counts and queries use displayed rows.
9. Default lint, format, type, and offline test checks pass; packaged entry
   points and Docker resources remain valid.

## Risks and open questions

Tools now inspect a complete setup through the same publisher as model resources;
model catalog errors can therefore fail tool inspection. This preserves one
source of effective setup truth. Installed-plugin lists remain independent of
all setup inputs. No open scope questions.

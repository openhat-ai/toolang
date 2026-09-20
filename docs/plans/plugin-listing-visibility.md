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
  factories cannot load. Keep sorted identities, distribution source labels, collection summaries,
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
  published main-module effective caps and full cap index. Root lists prepare or
  reuse the shared root State layer and apply the shared State policy selector
  once against its resolved metadata and configuration, without reading or
  creating an agent home. Remote content uses the existing root cache and refresh
  behavior. Aggregate and kind-specific lists, including standalone `caps`, share
  this behavior.
  Preserve home/here precedence: `--all` does not resurrect shadowed definitions
  or include other agents' resources. Runtime grants are unchanged.
- Tools/models/providers need a resident home, not a running or parsed agent
  program. Inspect setup with default/compact model validation disabled, as in
  model inspection. Keep existing options and query diagnostics.

## Output and summaries

All existing `--all` options accept `-a`, including cap-kind and job lists.
Resource scope, policy, query semantics, and JSON catalog exports stay unchanged.

| Resource | Default view | Full view (`--all` / `-a`) |
| --- | --- | --- |
| Caps | Identity, description, scope, form, source | Add `STATUS` immediately after identity |
| Tools | Identity and description; no `SOURCE` | Add `STATUS` last |
| Models | Identity, context/output sizes, modalities, capabilities, price | Add `STATUS` last, with unready reasons in parentheses |
| Providers | `MODELS` counts effective models | `MODELS` shows `OK/ALL` counts; no REASON column in either view |
| Plugin inventories | Identity and distribution source | Toolsets additionally include internal entries; no extra state column |

Model `STATUS` is `ok`, `blocked`, `unready (reason)`, or
`blocked, unready (reason)`. Policy and readiness remain independent:
`ok` means ready AND allowed, not readiness alone. Tools and
caps have no independent readiness protocol, so their status is `ok` or `blocked`.
Internal tools remain recognizable by their `_toolang` identity; do not add an
`INTERNAL` label or column. Runtime-internal tools bypass user allow policy.
Model query field `available` retains its readiness meaning; it is not rendered
as a separate boolean table column. Provider OK counts read the effective setup
collection; they must exclude ready-but-blocked models. Models and providers
have no separate REASON column. Model unready reasons use `No adapter`,
`No API URL`, and `Missing env`, in that order, joined with `; ` inside STATUS
parentheses. Provider MODELS cells keep the full-view fraction but use the
same header as the default view.

All resource and plugin inventory tables have an unindented summary. Tools use
`N tools, M toolsets`, aggregate caps use `N caps, M kinds`, and models use
`N models, M providers`. Omit the second count when N is zero or one; otherwise
include it even when M is one. Other lists use their own noun, such as `N prompts`
or `N adapters`. Use English singular/plural forms. Count only displayed rows
and their distinct groups after scope, policy, visibility, and query filters.
Print one blank line between a nonempty table and its summary. Empty results
print only `0 <items>` without headers or an additional empty-result message.
JSON exports include neither summaries nor presentation states.

Format prices as `INPUT / OUTPUT`, with each amount right-aligned independently
to the widest formatted value among displayed rows. Amounts omit `$`; the
`PRICE ($/1M)` header supplies the unit. Align `/` and both numeric
columns, including zero and missing (`-`) values; filtering recomputes widths.
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
- State cap selection and CLI cap commands: reuse policy and prepared root layers,
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
   Remote descriptions participate in allow/query selection identically in root
   and agent views, with cold preparation and reuse of the same root cache.
6. Models and providers share static catalog precedence, replacement semantics,
   root/agent isolation, and default/`--all` policy behavior. Root cache writes
   never create a default-agent home; root and agent versions remain distinct.
7. Resident tools use layout-only preparation; missing agents and unsupported
   target forms fail without creating agent homes. Help performs no loading.
8. Full-view STATUS distinguishes all policy/readiness combinations; provider
   OK/ALL excludes ready-but-blocked models. Default columns omit constant
   states, and tools omit SOURCE. All summaries handle empty, single, multiple,
   and filtered rows. Prices align both amounts and their separator. Every
   --all option has an equivalent -a alias.
9. Default lint, format, type, and offline test checks pass; packaged entry
   points and Docker resources remain valid.

## Risks and open questions

Tools now inspect a complete setup through the same publisher as model resources;
model catalog errors can therefore fail tool inspection. This preserves one
source of effective setup truth. Installed-plugin lists remain independent of
all setup inputs. No open scope questions.

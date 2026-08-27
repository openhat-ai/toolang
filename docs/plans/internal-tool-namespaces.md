# Define Tool Names And Internal Namespaces

## Status

Approved for implementation on 2026-08-27.

## Goal

Give every model-facing tool one short, stable namespace and verb-first leaf
name, while reserving underscore-prefixed namespaces for Toolang-owned internal
actions.

This change renames the existing built-in toolsets and moves the current
`agent_state` tools under `_me`. It establishes the naming and registration
boundary needed by future `_too` runtime actions and `_hat` Human-Agent Teaming
actions without implementing those actions yet.

## Success Criteria

- Public toolset namespaces and all leaf tool names contain only ASCII letters
  and underscores, start with a letter, and never contain `__`.
- External toolset plugin identities follow the same public-name grammar.
- External toolset plugins cannot register an underscore-prefixed namespace,
  including through a namespaced tool key.
- Toolang-owned built-ins may register a namespace with exactly one leading
  underscore; the remaining component follows the public namespace rules.
- Model-facing names remain encoded as `<namespace>__<leaf>` for every provider.
- Selectors and human-readable identities remain `<namespace>/<leaf>`.
- Built-in toolset and leaf names use the mappings in this plan with no legacy
  aliases.
- Tool behavior, resource selection, execution records, and presentation
  behavior otherwise remain unchanged.
- The default offline verification suite passes.

## Current Behavior

Toolset plugins are loaded from the `toolang.toolset` entry-point group. A
toolset may return either a bare leaf key or a `<namespace>/<leaf>` key. The
loader converts that identity to `<namespace>__<leaf>` for model APIs.

Name validation currently accepts letters, digits, and underscores. It allows
leading underscores and embedded `__`. Plugin source is available during entry
point inspection but is discarded before tool registration, so an external
plugin can currently claim a future internal namespace through either its
toolset name or a namespaced key.

The built-in runtime names are currently `filesystem`, `web_search`, `shell`,
`service_use`, and `agent_state`. Several leaf names use noun-first forms such
as `task_create`, `tool_call`, and `resource_read`.

## Scope

This change includes:

- canonical tool namespace and leaf-name validation;
- built-in versus external namespace authority during tool registration;
- built-in toolset renames to `fs`, `web`, `shell`, `service`, and `_me`;
- verb-first renames for existing `service` and `_me` leaf tools;
- selector, configuration, example, documentation, and test updates; and
- explicit rejection tests for external attempts to claim internal names.

This change does not include:

- flow authoring tools;
- `_too` or `_hat` tool implementations;
- State refresh or mid-run State application;
- agic-produced runnable calls;
- hiding, rewriting, or specially rendering internal tools in progress output;
- root flow discovery or shared flow installation;
- compatibility aliases or migrations for old tool names; or
- Toolang or tree-sitter syntax changes.

## Vocabulary

| Concept | Form | Meaning |
| --- | --- | --- |
| Public namespace | `fs`, `web`, `shell`, `service` | User-facing built-in or external toolset namespace |
| Internal namespace | `_me`, `_too`, `_hat` | Toolang-owned model action family |
| Leaf name | `read`, `create_task` | Verb or verb phrase within one namespace |
| Selector identity | `fs/read` | Authored and CLI tool selection form |
| Model name | `fs__read` | Provider-facing encoded name |
| Plugin | entry-point implementation | Owner that may expose one or more public namespaces |

`_me` means mutation or inspection of data authored for the current agent. It
does not redefine formal Agent State: independent `tasks/*.md` and
`chores/*.md` remain Work and do not enter a State revision.
`_too` is reserved for future Toolang executor/runtime actions. `_hat` is
reserved for future Human-Agent Teaming communication. A leading underscore is
an authority boundary, not merely a display convention.

## Name Grammar

The two component grammars are:

```text
public-name       := ASCII_LETTER (ASCII_LETTER | "_")*
internal-namespace := "_" public-name
```

Additional rules apply:

- `__` is forbidden inside every component because it is the model-name
  separator;
- a leaf name always uses `public-name`, even inside an internal namespace;
- digits, hyphens, dots, slashes, whitespace, and non-ASCII letters are not
  valid component characters;
- `/` is accepted only as the separator in a toolset's namespaced registration
  key; and
- `__` is introduced only by the model adapter boundary through the shared
  encoder.

An external toolset's entry-point name and effective plugin name also use
`public-name`. A Toolang-owned internal toolset may use `internal-namespace` as
its entry-point name, plugin name, and default namespace.

The provider-facing encoding remains unchanged:

```text
encode("web", "search")       = "web__search"
encode("_me", "create_task") = "_me__create_task"
```

No provider adapter replaces `__` with a dot. A frontend may later render a
friendlier display name without changing stored or provider-facing identity.

## Namespace Authority

Tool registration must retain the source of the entry point until every leaf
tool has been validated:

- a Toolang built-in entry point may register a public or internal namespace;
- an external entry point may register only a public namespace;
- the rule applies to both the toolset's default namespace and every explicit
  `<namespace>/<leaf>` key; and
- all plugins, including built-ins, use the same leaf-name validation.

Internal authority comes only from installed distribution metadata whose
normalized distribution name is exactly `toolang`. An entry-point target under
a `toolang.*` Python module is not sufficient authority; an entry point without
distribution metadata is treated as external.

The loader rejects an invalid registration before constructing the effective
tool map. It must not silently skip, normalize, or truncate invalid names.
Duplicate encoded names remain errors.

Internal tools continue through the normal tool resource ceiling and authored
selector pipeline. A user can therefore deny `_me/*`; the underscore does not
bypass resource policy. Default tool listings and progress output remain
unchanged in this change. The prefix alone provides a stable classifier for a
later presentation policy.

## Built-In Namespace Renames

| Old namespace | New namespace | Source module |
| --- | --- | --- |
| `filesystem` | `fs` | `toolang.plugin.toolsets.filesystem` |
| `web_search` | `web` | `toolang.plugin.toolsets.web_search` |
| `shell` | `shell` | `toolang.plugin.toolsets.shell` |
| `service_use` | `service` | `toolang.plugin.toolsets.service_use` |
| `agent_state` | `_me` | `toolang.execution.tools.agent_state` |

Python source-module and class names may remain descriptive. Runtime plugin,
namespace, selector, model-name, configuration, and tool-room identities use
the new names.

## Leaf Tool Renames

The `fs` text-operation leaves become shorter:

| Old leaf | New leaf |
| --- | --- |
| `read_text` | `read` |
| `write_text` | `write` |
| `append_text` | `append` |

Its other leaves and the `web` and `shell` leaves remain, producing this final
catalog:

```text
fs:      list, read, write, append, glob, stat, mkdir, remove
web:     search
shell:   execute
```

The `service` leaves become:

| Old leaf | New leaf |
| --- | --- |
| `bridge_start` | `start_bridge` |
| `bridge_stop` | `stop_bridge` |
| `init` | `init` |
| `auth_start` | `start_auth` |
| `auth_complete` | `complete_auth` |
| `tool_list` | `list_tools` |
| `tool_call` | `call_tool` |
| `resource_list` | `list_resources` |
| `resource_template_list` | `list_resource_templates` |
| `resource_read` | `read_resource` |
| `prompt_list` | `list_prompts` |
| `prompt_get` | `get_prompt` |

The `_me` leaves become:

| Authored kind | Leaves |
| --- | --- |
| task | `list_tasks`, `get_task`, `create_task`, `update_task` |
| chore | `list_chores`, `get_chore`, `create_chore`, `update_chore` |
| psyche | `list_psyches`, `get_psyche`, `create_psyche`, `update_psyche`, `delete_psyche` |
| skill | `list_skills`, `get_skill`, `create_skill`, `update_skill`, `delete_skill` |
| service | `list_services`, `get_service`, `create_service`, `update_service`, `delete_service` |
| prompt | `list_prompts`, `get_prompt`, `create_prompt`, `update_prompt`, `delete_prompt` |

Inputs, outputs, defaults, validation, and filesystem effects do not change.
Flow leaves are added only by the later flow-authoring feature.

## Compatibility And Persistence

This is an intentional breaking rename. The old entry-point, configuration,
selector, and model names are removed without aliases. Documentation and
repository examples move atomically to the new names.

Stored execution records and model messages are immutable and are not
rewritten. Their historical tool names remain valid historical data. A retry
whose captured resource snapshot requires a removed tool identity fails as an
unavailable resource; rerun and new root runs resolve current names. Existing
tool runtime directories under old names are ignored, and new calls use the
new plugin-name directories.

Keeping one canonical current identity avoids exposing duplicate tools to new
model calls and prevents provider history from diverging through
adapter-specific name rewrites.

## Design Touchpoints

- `src/toolang/base/utils/tools.py`: component grammar and shared encoding.
- `src/toolang/plugin/loading.py`: preserve entry-point source while loading
  toolsets.
- `src/toolang/plugin/toolsets/registry.py`: registration validation and
  internal namespace authority.
- `src/toolang/plugin/toolsets/loading.py`: pass source provenance into every
  tool registration.
- `pyproject.toml`: canonical built-in entry-point names.
- `src/toolang/plugin/toolsets/{filesystem,web_search,shell,service_use}.py`:
  runtime namespace and leaf identities.
- `src/toolang/execution/tools/agent_state.py`: `_me` namespace and verb-first
  leaves.
- `docs/tools.md`, `docs/selectors.md`, `docs/plugins.md`, examples, and tests:
  current public names and migration examples.

Execution records, model adapters, StateWatcher, and State persistence require
no schema changes.

## Acceptance Tests

1. Built-in loading exposes `fs__read`, `web__search`, `shell__execute`,
   `service__call_tool`, and `_me__create_task`, and exposes none of the old
   model names.
2. Selector resolution accepts `fs/*`, `service/call_tool`, and `_me/*` and
   rejects selectors that reference removed names.
3. External plugin identities, public namespaces, and leaf names reject empty
   values, leading underscores, digits, hyphens, dots, embedded `__`, slashes,
   whitespace, and non-ASCII letters.
4. A built-in can register `_me/create_task`.
5. An external plugin cannot register `_me`, `_too`, `_hat`, another leading-
   underscore plugin or namespace, or an internal namespace through a
   namespaced key; a `toolang.*` target path grants no exception.
6. An external plugin may still expose multiple valid public namespaces through
   namespaced keys.
7. Every renamed built-in tool retains its existing inputs, outputs, validation,
   side effects, and tool-call recording.
8. Tool resource snapshots persist the new plugin, namespace, leaf, and encoded
   model names and resolve them exactly within new runs.
9. Tool configuration, CLI inspection, authored selectors, repository examples,
   and documentation use only canonical names.
10. The default offline verification suite passes.

## Risks

- Existing configuration and authored selectors using old names require manual
  updates. Atomic documentation and example changes make the migration explicit.
- Existing retry resource snapshots can reference removed names. This is an
  accepted consequence of having no aliases; rerun remains the migration path.
- An incomplete rename could leave two model-facing identities for one action.
  Tests assert both the complete new catalog and absence of old identities.
- Losing plugin provenance before registration would reopen internal namespace
  spoofing. Authority tests cover default and explicitly namespaced keys.

## Open Questions

None.

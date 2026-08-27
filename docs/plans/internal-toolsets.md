# Define Tool Names And Internal Toolsets

## Status

Approved for implementation on 2026-08-27.

## Goal

Give every model-facing tool one short, stable toolset and verb-first leaf name,
while reserving underscore-prefixed toolsets for Toolang-owned internal actions.

This change renames the existing built-in toolsets and moves the current
`agent_state` tools under `_me`. It establishes the naming and registration
boundary needed by future `_too` runtime actions and `_hat` Human-Agent Teaming
actions without implementing those actions yet.

## Success Criteria

- Public toolset names and all leaf tool names contain only ASCII letters
  and underscores, start with a letter, and never contain `__`.
- External toolset plugin identities follow the same public-name grammar.
- External toolset plugins cannot register an underscore-prefixed toolset,
  including through an explicit toolset key.
- Toolang-owned built-ins may register a toolset with exactly one leading
  underscore; the remaining component follows the public toolset rules.
- Model-facing names remain encoded as `<toolset>__<leaf>` for every provider.
- Selectors and human-readable identities remain `<toolset>/<leaf>`.
- Built-in toolset and leaf names use the mappings in this plan with no legacy
  aliases.
- Tool behavior, resource selection, execution records, and presentation
  behavior otherwise remain unchanged.
- The default offline verification suite passes.

## Current Behavior

Toolset plugins are loaded from the `toolang.toolset` entry-point group. A
toolset plugin may return either a bare leaf key or a `<toolset>/<leaf>` key.
The loader converts that identity to `<toolset>__<leaf>` for model APIs.

Name validation currently accepts letters, digits, and underscores. It allows
leading underscores and embedded `__`. Plugin source is available during entry
point inspection but is discarded before tool registration, so an external
plugin can currently claim a future internal toolset through either its plugin
name or an explicit toolset key.

The built-in runtime names are currently `filesystem`, `web_search`, `shell`,
`service_use`, and `agent_state`. Several leaf names use noun-first forms such
as `task_create`, `tool_call`, and `resource_read`.

## Scope

This change includes:

- canonical toolset and leaf-name validation;
- built-in versus external toolset authority during tool registration;
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
| Public toolset | `fs`, `web`, `shell`, `service` | User-facing built-in or external tool family |
| Internal toolset | `_me`, `_too`, `_hat` | Toolang-owned model action family |
| Leaf name | `read`, `create_task` | Verb or verb phrase within one toolset |
| Selector identity | `fs/read` | Authored and CLI tool selection form |
| Model name | `fs__read` | Provider-facing encoded name |
| Plugin | entry-point implementation | Owner that may expose one or more public toolsets |

`_me` means mutation or inspection of data authored for the current agent. It
does not redefine formal Agent State: independent `tasks/*.md` and
`chores/*.md` remain Work and do not enter a State revision.
The executor binds the current agent layout through `ToolContext`; `_me` tools
do not accept an agent name, home directory, root directory, or arbitrary path
for choosing their target. They expose no layer selector and operate only on
the current agent's home layer; `_me` never reads or mutates the root layer.
`_too` is reserved for future Toolang executor/runtime actions. `_hat` is
reserved for future Human-Agent Teaming communication. A leading underscore is
an authority boundary, not merely a display convention.

## Name Grammar

The two component grammars are:

```text
public-name       := ASCII_LETTER (ASCII_LETTER | "_")*
internal-toolset := "_" public-name
```

Additional rules apply:

- `__` is forbidden inside every component because it is the model-name
  separator;
- a leaf name always uses `public-name`, even inside an internal toolset;
- digits, hyphens, dots, slashes, whitespace, and non-ASCII letters are not
  valid component characters;
- `/` is accepted only as the separator in an explicit toolset registration
  key; and
- `__` is introduced only by the model adapter boundary through the shared
  encoder.

An external toolset's entry-point name and effective plugin name also use
`public-name`. A Toolang-owned internal toolset may use `internal-toolset` as
its entry-point name, plugin name, and default toolset.

The provider-facing encoding remains unchanged:

```text
encode("web", "search")       = "web__search"
encode("_me", "create_task") = "_me__create_task"
```

No provider adapter replaces `__` with a dot. A frontend may later render a
friendlier display name without changing stored or provider-facing identity.

## Toolset Authority

Tool registration must retain the source of the entry point until every leaf
tool has been validated:

- a Toolang built-in entry point may register a public or internal toolset;
- an external entry point may register only a public toolset;
- the rule applies to both the plugin's default toolset and every explicit
  `<toolset>/<leaf>` key; and
- all plugins, including built-ins, use the same leaf-name validation.

Internal authority comes only from installed distribution metadata whose
normalized distribution name is exactly `toolang`. An entry-point target under
a `toolang.*` Python module is not sufficient authority; an entry point without
distribution metadata is treated as external.

The loader rejects an invalid registration before constructing the effective
tool map. It must not silently skip, normalize, or truncate invalid names.
Built-in toolsets are processed before external toolsets. Duplicate effective
toolset names and duplicate encoded names remain explicit errors, including
when an external toolset collides with a built-in public name.

Internal tools continue through the normal tool resource ceiling and authored
selector pipeline. A user can therefore deny `_me/*`; the underscore does not
bypass resource policy. Default tool listings and progress output remain
unchanged in this change. The prefix alone provides a stable classifier for a
later presentation policy.

## Built-In Toolset Renames

| Old toolset | New toolset | Source module |
| --- | --- | --- |
| `filesystem` | `fs` | `toolang.plugin.toolsets.filesystem` |
| `web_search` | `web` | `toolang.plugin.toolsets.web_search` |
| `shell` | `shell` | `toolang.plugin.toolsets.shell` |
| `service_use` | `service` | `toolang.plugin.toolsets.service_use` |
| `agent_state` | `_me` | `toolang.execution.tools.agent_state` |

Python source-module and class names may remain descriptive. Runtime plugin,
toolset, selector, model-name, configuration, and tool-room identities use the
new names.

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

Other than removing cap `scope` inputs and restricting `_me` cap operations to
the current agent's home layer, inputs, outputs, defaults, validation, and
filesystem effects do not change. Flow leaves are added only by the later
flow-authoring feature.

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
  internal toolset authority.
- `src/toolang/plugin/toolsets/loading.py`: pass source provenance into every
  tool registration.
- `pyproject.toml`: canonical built-in entry-point names.
- `src/toolang/plugin/toolsets/{filesystem,web_search,shell,service_use}.py`:
  runtime toolset and leaf identities.
- `src/toolang/execution/tools/agent_state.py`: `_me` toolset and verb-first
  leaves.
- `docs/tools.md`, `docs/selectors.md`, `docs/plugins.md`, examples, and tests:
  current public names and migration examples.

New tool resource snapshots persist `toolset`; existing snapshots using the
former `namespace` field remain readable without rewriting. Model adapters,
StateWatcher, and State persistence require no other schema changes.

## Acceptance Tests

1. Built-in loading exposes `fs__read`, `web__search`, `shell__execute`,
   `service__call_tool`, and `_me__create_task`, and exposes none of the old
   model names.
2. Selector resolution accepts `fs/*`, `service/call_tool`, and `_me/*` and
   rejects selectors that reference removed names.
3. External plugin identities, public toolsets, and leaf names reject empty
   values, leading underscores, digits, hyphens, dots, embedded `__`, slashes,
   whitespace, and non-ASCII letters.
4. A built-in can register `_me/create_task`.
5. An external plugin cannot register `_me`, `_too`, `_hat`, another leading-
   underscore plugin or toolset, or an internal toolset through an explicit
   toolset key; a `toolang.*` target path grants no exception.
6. An external plugin may still expose multiple valid public toolsets through
   explicit toolset keys.
7. Built-ins are processed before external toolsets, and any duplicate effective
   toolset name is rejected explicitly rather than silently shadowing a built-in.
8. `_me` schemas expose no agent, path, or layer selector; cap reads and writes
   use only the current agent's home layer and never observe root-layer caps.
9. Every renamed built-in tool otherwise retains its existing inputs, outputs,
   validation, side effects, and tool-call recording.
10. Tool resource snapshots persist the plugin, toolset, leaf, and encoded model
    names, read the former `namespace` field for historical snapshots, and
    resolve current identities exactly within new runs.
11. Tool configuration, CLI inspection, authored selectors, repository examples,
   and documentation use only canonical names.
12. The default offline verification suite passes.

## Risks

- Existing configuration and authored selectors using old names require manual
  updates. Atomic documentation and example changes make the migration explicit.
- Existing retry resource snapshots can reference removed names. This is an
  accepted consequence of having no aliases; rerun remains the migration path.
- An incomplete rename could leave two model-facing identities for one action.
  Tests assert both the complete new catalog and absence of old identities.
- Losing plugin provenance before registration would reopen internal toolset
  spoofing. Authority tests cover default and explicit toolset keys.

## Open Questions

None.

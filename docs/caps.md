# Composable Agent Primitives

Caps are **composable agent primitives**: reusable psyche, skill, service and
prompt definitions made available through State. A cap's kind
determines how execution consumes it; availability alone does not invoke it.


## Kinds

| Kind | Execution role |
| --- | --- |
| `psyche` | Selected instructions included in model-call instruction assembly. |
| `skill` | Workflow guidance advertised by a short trigger; the model loads its body on demand. |
| `service` | MCP connection metadata and optional guidance, also advertised by a trigger. Loading guidance neither connects the service nor grants its tools. |
| `prompt` | Content template expanded with explicit input and arguments; it is not a runnable. |

[Instruction layers](agic.md#instruction-layers),
[guidance loading](tools.md#pick-guidance), and
[prompt expansion](call-input.md#prompt-expansion) own the corresponding contracts.


## Runtime Scope

Scope determines where a cap is available. Precedence is `root < home < here`:

| Scope | Meaning |
| --- | --- |
| `root` | Provided by the current Toolang root |
| `home` | Provided by the current agent home |
| `here` | Declared or referenced in one program module |

For each kind and name, the highest-precedence visible definition wins.

CLI query records expose scope through `tags`; HTTP read payloads retain the
`scope` field.

`too caps` and untargeted kind-specific lists show root-shared resources
filtered by root cap-kind allow policy. `too alice caps` combines root resources
with Alice's home and program caps using the precedence above, then
reads the main agent module's published effective allow selection. Other agents'
private resources, including an implicit `default` agent, are outside that view.
Root inspection prepares or reuses the shared root State layer, including resolved
remote metadata, before applying allow and query filters. It never prepares an
agent home. Remote content follows the same cache and refresh behavior as agent
State preparation.

`--all` includes allow-excluded resources without restoring shadowed definitions
or granting runtime access. Caps have no separate readiness protocol: invalid
or unresolvable definitions fail instead of becoming unavailable rows.
Per-module and run declarations can further narrow execution resources.

HTTP write payloads use `root` and `home` directly. CLI write commands expose
placement as command shape: without `AGENT`, they write root caps;
with `AGENT`, they write that agent's home caps. HTTP writes default to `home`.


## Form, Scope, And Origin

Cap entries separate how a cap is attached, where it is available, and where
its content comes from.

Form tells how a cap is attached:

| Form | Meaning |
| --- | --- |
| `authored` | Backed by files or folders in cap directories |
| `inline` | Defined directly in a program module |
| `configured` | Configured by a ref in `config.toml` |
| `referenced` | Attached by a program module `with` declaration |

`origin` describes where the cap content is authored:

| Origin | Meaning |
| --- | --- |
| `local` | Local authored content, including inline program caps and local files or directories |
| `remote` | A remote authored cap fetched through a ref |

Runtime APIs expose effective caps. They do not expose every authored source
variant as a separate history object.

Only `authored` and `configured` forms can have root or home scope. `referenced`
and `inline` forms belong to one program module and have `here` scope.

Authored placement, such as `config.toml`, `agent.too`, or a cap file path, is
exposed separately as `definition_file`. When known, APIs may also include
`line`.


## Inspection locations

[Resource Queries](queries.md) owns cap columns, tags and selectors. A location
addresses actual content: authored skills point to `SKILL.md`, inline caps use
`file:line`, and remote configured/referenced caps use their GitHub HTTPS URL.
Definition metadata remains separate from the displayed content address.

## Source Refs

API and read/write source refs identify the selected cap itself. These URIs
also serve as identities for persistence and deduplication. CLI query records
use `kind/name` as `ref` and expose the content address as `location`, without a
`source` field:

| Ref | Meaning |
| --- | --- |
| `inline://prompts/reviewer` | Embedded inline cap definition |
| `home://services/github` | Local cap in the agent home |
| `root://skills/reviewer` | Local cap under the Toolang root |
| `github://user/repo/path/name.md@rev` | Remote cap target |

GitHub cap refs must include `@rev`. Shorthand refs such as `owner/name`
resolve to the matched repository's default branch before they are stored.
Three-part shorthand such as `owner/repo/name` specifies the repository exactly
and probes only paths inside that repository.

Current cap shorthand probe rules are:

| Kind | Input | Probe order |
| --- | --- | --- |
| `skill` | `owner/name` | `github://owner/agents/skills/name@<default-branch>`, `github://owner/agent-skills/name@<default-branch>`, `github://owner/agent-skills/skills/name@<default-branch>`, `github://owner/skills/name@<default-branch>`, `github://owner/skills/skills/name@<default-branch>` |
| `psyche` | `owner/name` | `github://owner/agents/psyches/name.md@<default-branch>`, `github://owner/agent-psyches/name.md@<default-branch>`, `github://owner/psyches/name.md@<default-branch>` |
| `service` | `owner/name` | `github://owner/agents/services/name.md@<default-branch>`, `github://owner/agent-services/name.md@<default-branch>`, `github://owner/services/name.md@<default-branch>` |
| `prompt` | `owner/name` | `github://owner/agents/prompts/name.md@<default-branch>`, `github://owner/agent-prompts/name.md@<default-branch>`, `github://owner/prompts/name.md@<default-branch>` |

For `owner/repo/name`, Toolang uses the specified repository and probes only
kind-specific paths in that repository. Agents use `agents/name.too`, then
`name.too`. Skills use `skills/name`, then `name`, except dedicated
`agent-skills` and `skills` repositories prefer `name` first. File-backed caps
use `{kind}s/name.md`, then `name.md`, except dedicated cap repositories prefer
`name.md`.

Skill existence checks look for `SKILL.md` inside the candidate directory.
GitHub URLs are exact refs, not shorthand; a URL ending in
`skills/name/SKILL.md` is stored as the parent skill directory ref.


## Local Cap Paths

Root local caps:

- `${TOOLANG_ROOT}/psyches/`
- `${TOOLANG_ROOT}/skills/`
- `${TOOLANG_ROOT}/services/`
- `${TOOLANG_ROOT}/prompts/`

Home local caps:

- `${TOOLANG_ROOT}/agents/<agent>/psyches/`
- `${TOOLANG_ROOT}/agents/<agent>/skills/`
- `${TOOLANG_ROOT}/agents/<agent>/services/`
- `${TOOLANG_ROOT}/agents/<agent>/prompts/`


## State Cap Paths

Materialized caps live inside immutable State layer revisions:

- authored caps are copied under `files/caps/authored`
- configured caps are materialized under `files/caps/configured`
- inline module caps use `files/caps/inline/<module>`
- referenced module caps use `files/caps/referenced/<module>`

Root layers live under `${TOOLANG_ROOT}/.state/root/revs/<revision>`.
Home layers live under
`${TOOLANG_ROOT}/agents/<agent>/.state/home/revs/<revision>`. See
[agent-state.md](./agent-state.md) for the complete layout and revision rules.


## Local Cap Frontmatter

`skill` and `service` definitions use `description` as their progressive
loading trigger summary. It should be a short natural-language phrase that
helps the model decide whether to load the cap body or service details. The cap
name comes from its file or directory name, not from frontmatter.

Skill frontmatter:

| Field | Required | Meaning |
| --- | --- | --- |
| `description` | yes | Trigger summary used before the skill body is loaded |

Skill bodies are required and contain the loaded workflow instructions. Put the
selection hint in `description`; put the actual workflow, rules, and output
shape in the body.

Service frontmatter:

| Field | Required | Meaning |
| --- | --- | --- |
| `description` | yes | Trigger summary used before service details are loaded |
| `transport` | yes | `http` or `stdio` |
| `target` | yes | Endpoint URL for `http`; argv command line for `stdio` |
| `headers` | no | String map for HTTP headers |
| `env` | no | Comma-separated environment variable names |

Service bodies are optional and can document exposed service operations, auth notes,
and when optional `headers` or `env` values are expected. Header values like
`$API_TOKEN` declare required host environment variables. For `stdio`, `target`
is written as one shell-like command line, and `env` can list required variables
as `env: API_TOKEN, ANOTHER_ENV_VAR`.


## Effective Cap Set

`AgentState` captures the complete durable cap set. State
preparation:

1. collects root and home definitions
2. resolves configured and referenced entries
3. materializes runtime-ready artifacts when needed
4. selects the winning definition for each `(kind, name)`

State preparation applies the configured and startup `psyches`, `skills`,
`services`, and `prompts` allow fields once. `StateWatcher` publishes `AgentState`
directly; `caps_for(module)` returns its precomputed effective caps. Root-run
preparation copies concrete cap identities into tree-level `AgentResources`.
Request-level ceilings preserve the same four boundaries. Flow and agic
directives may narrow the result further, but cannot restore caps outside the
published resources.


## Integration and verification

[HTTP contracts](api.md#cap-publication) own writes, publication
receipts and effective reads. [Resource Queries](queries.md) owns the public
inspection shape; lists expose content locations, while API records also retain
source definition metadata.

[Catalog caps](../src/toolang/catalog/cap.py) own authored/configured changes;
[State](../src/toolang/state/) owns prepared effective caps.
[Catalog tests](../tests/unit/catalog/test_caps.py) and
[API publication tests](../tests/unit/api/test_cap_publication.py) verify those
boundaries.

# Capability Model

Caps are composable agent primitives.


## Kinds

Current cap kinds are:

- `psyche`
- `skill`
- `service`
- `prompt`


## Runtime Scope

State capabilities use runtime scope:

- `root`: provided by the current Toolang root
- `home`: available to one agent home
- `here`: declared or referenced in the current source and travels with it

Precedence is:

1. `here`
2. `home`
3. `root`

One effective cap set is built by applying this precedence to all visible cap
definitions.

CLI and HTTP read APIs expose these runtime `scope` values directly.

HTTP write payloads use `root` and `home` directly. CLI write commands expose
placement as command shape: without `AGENT`, they write root capabilities;
with `AGENT`, they write that agent's home capabilities.


## Form, Scope, And Origin

Cap entries separate how a cap is attached, where it is available, and where
its content comes from.

Form tells how a cap is attached:

| Form | Meaning |
| --- | --- |
| `authored` | Backed by files or folders in capability directories |
| `inline` | Defined directly in a program module |
| `configured` | Configured by a ref in `config.toml` |
| `referenced` | Attached by a program module `with` declaration |

Scope tells where a cap is available:

| Scope | Meaning |
| --- | --- |
| `root` | Provided by the current Toolang root |
| `home` | Provided by the current agent home |
| `here` | Declared or referenced in the current source and travels with it |

`origin` describes where the cap content is authored:

| Origin | Meaning |
| --- | --- |
| `local` | Local authored content, including inline program caps and local files or directories |
| `remote` | A remote authored cap fetched through a ref |

Runtime APIs expose effective caps. They do not expose every authored source
variant as a separate history object.

HTTP write payloads default to `home` scope. CLI write commands default to root
capabilities when `AGENT` is omitted. Only `authored` and `configured` forms
can have root or home scope. `referenced` and `inline` forms belong to one
program module and have `here` scope.

Authored placement, such as `config.toml`, `agent.too`, or a cap file path, is
exposed separately as `definition_file`. When known, APIs may also include
`line`.


## CLI List Projection

The `caps list` and `caps <kind> list` commands present a compact user-facing
view:

| Column | Meaning |
| --- | --- |
| `SOURCE` | Authored file path, `agent.too` line reference, or directly accessible remote URL |
| `FORM` | Source form: `authored`, `inline`, `configured`, or `referenced` |
| `SCOPE` | Runtime scope: `root`, `home`, or `here` |

`FORM` uses the same values as the runtime source form. It is not remapped for
display.

The `--filter` option accepts kind, form, and scope values. Values in
the same group are unioned; conditions across groups are intersected.


## Refs

Public cap refs identify the selected cap itself:

| Ref | Meaning |
| --- | --- |
| `inline://prompts/reviewer` | Embedded inline cap definition |
| `home://services/github` | Local capability in the agent home |
| `root://skills/reviewer` | Local capability under the Toolang root |
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

Root local capabilities:

- `${TOOLANG_ROOT}/psyches/`
- `${TOOLANG_ROOT}/skills/`
- `${TOOLANG_ROOT}/services/`
- `${TOOLANG_ROOT}/prompts/`

Home local capabilities:

- `${TOOLANG_ROOT}/agents/<agent>/psyches/`
- `${TOOLANG_ROOT}/agents/<agent>/skills/`
- `${TOOLANG_ROOT}/agents/<agent>/services/`
- `${TOOLANG_ROOT}/agents/<agent>/prompts/`


## State Capability Paths

Materialized capabilities live inside immutable State layer revisions:

- authored capabilities are copied under `files/caps/authored`
- configured capabilities are materialized under `files/caps/configured`
- inline module capabilities use `files/caps/inline/<module>`
- referenced module capabilities use `files/caps/referenced/<module>`

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

Service bodies are optional and can document exposed capabilities, auth notes,
and when optional `headers` or `env` values are expected. Header values like
`$API_TOKEN` declare required host environment variables. For `stdio`, `target`
is written as one shell-like command line, and `env` can list required variables
as `env: API_TOKEN, ANOTHER_ENV_VAR`.


## Effective Cap Set

`AgentState` captures one complete effective capability set. State
preparation:

1. collects root and home definitions
2. resolves configured and referenced entries
3. materializes runtime-ready artifacts when needed
4. selects the winning definition for each `(kind, name)`

At root-run start, `AgentSetup.ceiling.caps` narrows that captured set
into the tree-level `AgentResources`. Any request-level cap ceiling is applied
to that result. Flow and agic directives may narrow it further, but cannot
restore caps outside the agent resources.


## HTTP API

Read endpoints:

- `GET /api/v1/caps`
- `GET /api/v1/psyches`
- `GET /api/v1/skills`
- `GET /api/v1/services`
- `GET /api/v1/prompts`
- `GET /api/v1/psyches/{name}`
- `GET /api/v1/skills/{name}`
- `GET /api/v1/services/{name}`
- `GET /api/v1/prompts/{name}`
- `GET /api/v1/psyches/templates`
- `GET /api/v1/skills/templates`
- `GET /api/v1/services/templates`
- `GET /api/v1/prompts/templates`
- `GET /api/v1/psyches/templates/{template_name}`
- `GET /api/v1/skills/templates/{template_name}`
- `GET /api/v1/services/templates/{template_name}`
- `GET /api/v1/prompts/templates/{template_name}`

Write endpoints:

- `PUT /api/v1/psyches/{name}/authored`
- `PUT /api/v1/skills/{name}/authored`
- `PUT /api/v1/services/{name}/authored`
- `PUT /api/v1/prompts/{name}/authored`
- `DELETE /api/v1/psyches/{name}/authored`
- `DELETE /api/v1/skills/{name}/authored`
- `DELETE /api/v1/services/{name}/authored`
- `DELETE /api/v1/prompts/{name}/authored`
- `PUT /api/v1/psyches/{name}/configured`
- `PUT /api/v1/skills/{name}/configured`
- `PUT /api/v1/services/{name}/configured`
- `PUT /api/v1/prompts/{name}/configured`
- `DELETE /api/v1/psyches/{name}/configured`
- `DELETE /api/v1/skills/{name}/configured`
- `DELETE /api/v1/services/{name}/configured`
- `DELETE /api/v1/prompts/{name}/configured`

Authored write requests carry `scope` and `content`. Configured write requests
carry `scope` and `ref`. Deletes use a `scope` query parameter. Scope is
`root` or `home` and defaults to `home`.

Template detail responses include template metadata and raw content. Cap read
requests return the effective runtime view with `scope`, `origin`, `form`,
`ref`, `definition_file`, and optional `line`. CLI list commands project that
runtime view into `SOURCE`, `FORM`, and runtime `SCOPE`.

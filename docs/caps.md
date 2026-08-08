# Capability Model

Caps are composable agent primitives.


## Kinds

Current cap kinds are:

- `psyche`
- `skill`
- `service`
- `prompt`


## Runtime Scope

Prepared cap entries use runtime scope:

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

HTTP write payloads and internal storage still use `shared` and `private` to
choose authored placement. CLI write commands expose that placement as command
shape instead: without `AGENT`, they write root caps; with `AGENT`, they write
that agent's home caps.


## Form, Scope, And Origin

Cap entries separate how a cap is attached, where it is available, and where
its content comes from.

Form tells how a cap is attached:

| Form | Meaning |
| --- | --- |
| `inline` | Defined directly in `agent.too` |
| `ref` | Referenced by `use ...` in `agent.too` |
| `wired` | Wired in by `config.toml` |
| `file` | Backed by files or folders in cap directories |

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

HTTP write payloads default to `private` placement. CLI write commands default
to root caps when `AGENT` is omitted. Only `file` and `wired` forms can
be authored at shared placement and surface as `root`, because they can be
authored at the Toolang root. `ref` and `inline` forms are tied to one
source program and surface as `here`.

Authored placement, such as `config.toml`, `agent.too`, or a cap file path, is
exposed separately as `definition_file`. When known, APIs may also include
`line`.


## CLI List Projection

The `caps list` and `caps <kind> list` commands present a compact user-facing
view:

| Column | Meaning |
| --- | --- |
| `SOURCE` | Authored file path, `agent.too` line reference, or directly accessible remote URL |
| `FORM` | Source form: `inline`, `ref`, `wired`, or `file` |
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
| `home://services/github` | Private local cap in the agent home |
| `root://skills/reviewer` | Shared local cap under the Toolang root |
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

Shared local caps:

- `${TOOLANG_ROOT}/psyches/`
- `${TOOLANG_ROOT}/skills/`
- `${TOOLANG_ROOT}/services/`
- `${TOOLANG_ROOT}/prompts/`

Private local caps:

- `${TOOLANG_ROOT}/agents/<agent>/psyches/`
- `${TOOLANG_ROOT}/agents/<agent>/skills/`
- `${TOOLANG_ROOT}/agents/<agent>/services/`
- `${TOOLANG_ROOT}/agents/<agent>/prompts/`


## Prepared Cap Paths

Prepared materialized caps live inside immutable `.state` versions:

- `inline` caps are materialized under `files/inline`
- `ref` caps are materialized under `files/cited`
- `wired` caps are materialized under `files/wired`
- authored caps are copied under `files/authored`

Root versions live under `${TOOLANG_ROOT}/.state/versions/<version>`.
Agent-specific versions live under
`${TOOLANG_ROOT}/agents/<agent>/.state/versions/<version>`. See
[prepared-state.md](./prepared-state.md) for the complete layout and versioning
rules.


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

`AgentState` captures one complete effective prepared cap set. State
preparation:

1. collects root and home definitions
2. resolves wired and ref entries
3. materializes runtime-ready artifacts when needed
4. selects the winning definition for each `(kind, name)`

At root-run start, `AgentSetup.ceiling.caps` limits that captured set to the
private agent ceiling. Any request-level cap restriction is intersected with
that result. Flow and agic directives may narrow it further, but cannot restore
caps outside the agent ceiling.


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

- `PUT /api/v1/psyches/{name}/file`
- `PUT /api/v1/skills/{name}/file`
- `PUT /api/v1/services/{name}/file`
- `PUT /api/v1/prompts/{name}/file`
- `DELETE /api/v1/psyches/{name}/file`
- `DELETE /api/v1/skills/{name}/file`
- `DELETE /api/v1/services/{name}/file`
- `DELETE /api/v1/prompts/{name}/file`
- `PUT /api/v1/psyches/{name}/wired`
- `PUT /api/v1/skills/{name}/wired`
- `PUT /api/v1/services/{name}/wired`
- `PUT /api/v1/prompts/{name}/wired`
- `DELETE /api/v1/psyches/{name}/wired`
- `DELETE /api/v1/skills/{name}/wired`
- `DELETE /api/v1/services/{name}/wired`
- `DELETE /api/v1/prompts/{name}/wired`

File write requests carry `visibility` and `content`. Wired write requests
carry `visibility` and `ref`. Deletes use a `visibility` query parameter.
`visibility` is the HTTP write-placement field: `shared` maps to root
authored caps and `private` maps to the current agent's authored caps.

Template detail responses include template metadata and raw content. Cap read
requests return the effective runtime view with `scope`, `origin`, `form`,
`ref`, `definition_file`, and optional `line`. CLI list commands project that
runtime view into `SOURCE`, `FORM`, and runtime `SCOPE`.

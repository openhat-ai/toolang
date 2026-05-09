# Capability Model

Caps are composable agent primitives.


## Kinds

Current cap kinds are:

- `psyche`
- `skill`
- `service`
- `prompt`


## Visibility

Public cap views expose visibility:

- `shared`: available to all agents under the Toolang root
- `private`: available to one agent

Precedence is:

1. `private`
2. `shared`

One effective cap set is built by applying this precedence to all visible cap
definitions.

Prepared-state files, CLI, HTTP API, and UI-facing payloads all use `shared`
and `private`.


## Origins And Inclusions

Cap entries separate content origin from inclusion kind.

`origin` describes where the cap content is authored:

| Origin | Meaning |
| --- | --- |
| `local` | A local cap file or skill directory under the root or agent home |
| `remote` | A remote authored cap fetched through a ref |
| `inline` | A cap body written directly in an agent program |

`inclusion` describes how the cap is included:

| Inclusion | Meaning | Shared? |
| --- | --- | --- |
| `authored` | Included by a local authored cap file or skill directory | Yes |
| `configured` | Included by `config.toml` with a remote ref | Yes |
| `referenced` | Included by an agent program `use` statement | No |
| `embedded` | Included by an agent program declaration body | No |

Runtime APIs expose effective caps. They do not expose every authored source
variant as a separate history object.

Default visibility is `private`. Only `authored` and `configured` inclusions can
be `shared`, because they can be authored at the Toolang root. `referenced` and
`embedded` inclusions are scoped to one agent program, so they are always
`private`.

Authored placement, such as `config.toml`, `agent.too`, or a cap file path, is
exposed separately as `definition_file`. When known, APIs may also include
`line`.


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

One activation sees one effective cap set.

The runtime:

1. collects shared and private definitions
2. resolves remote entries
3. materializes runtime-ready artifacts when needed
4. selects the winning definition for each `(kind, name)`

Runs inherit the effective cap set of the activation that executes them.


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

- `PUT /api/v1/psyches/{name}/local`
- `PUT /api/v1/skills/{name}/local`
- `PUT /api/v1/services/{name}/local`
- `PUT /api/v1/prompts/{name}/local`
- `DELETE /api/v1/psyches/{name}/local`
- `DELETE /api/v1/skills/{name}/local`
- `DELETE /api/v1/services/{name}/local`
- `DELETE /api/v1/prompts/{name}/local`
- `PUT /api/v1/psyches/{name}/remote`
- `PUT /api/v1/skills/{name}/remote`
- `PUT /api/v1/services/{name}/remote`
- `PUT /api/v1/prompts/{name}/remote`
- `DELETE /api/v1/psyches/{name}/remote`
- `DELETE /api/v1/skills/{name}/remote`
- `DELETE /api/v1/services/{name}/remote`
- `DELETE /api/v1/prompts/{name}/remote`

Local write requests carry `visibility` and `content`. Remote write requests
carry `visibility` and `ref`. Deletes use a `visibility` query parameter.
Template detail responses include template metadata and raw content. Cap read
requests return the effective runtime view with `visibility`, `origin`,
`inclusion`, `ref`, `definition_file`, and optional `line`.

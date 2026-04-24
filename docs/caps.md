# Capability Model

Caps are composable agent primitives.


## Kinds

Current cap kinds are:

- `psyche`
- `skill`
- `service`
- `prompt`


## Scopes

Current cap scopes are:

- `global`
- `agent`

Precedence is:

1. `agent`
2. `global`

One effective cap set is built by applying this precedence to all visible cap
definitions.


## Sources

Caps may come from:

| Source | Meaning |
| --- | --- |
| `local` | A local file or directory under the root or agent home |
| `remote` | A remote ref stored in configuration |
| `inline` | An entry authored in the program and materialized for runtime use |

Runtime APIs expose effective caps. They do not expose every authored source
variant as a separate history object.


## Local Cap Paths

Global caps:

- `${TOOLANG_ROOT}/psyches/`
- `${TOOLANG_ROOT}/skills/`
- `${TOOLANG_ROOT}/services/`
- `${TOOLANG_ROOT}/prompts/`

Agent caps:

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

1. collects global and agent definitions
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

Local write requests carry `content`. Remote write requests carry `ref`.
Template detail responses include template metadata and raw content. Cap read
requests return the effective runtime view.

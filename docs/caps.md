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

Write endpoints:

- `PUT /api/v1/psyches/{name}`
- `PUT /api/v1/skills/{name}`
- `PUT /api/v1/services/{name}`
- `PUT /api/v1/prompts/{name}`
- `DELETE /api/v1/psyches/{name}`
- `DELETE /api/v1/skills/{name}`
- `DELETE /api/v1/services/{name}`
- `DELETE /api/v1/prompts/{name}`

Write requests target authored definitions. Read requests return the effective
runtime view.

# Toolang Caps Model

This document defines caps, cap kinds, cap scopes, cap sources, cap locators,
materialization, and effective runtime views.

Exact filesystem paths live in [layout.md](./layout.md).
Top-level lifecycle and runtime-resource vocabulary lives in
[model.md](./model.md).


## 1. Core Principle

In Toolang design docs, `caps` means composable agent primitives.

Toolang should keep authored cap definitions separate from runtime cap
visibility.

Authoring answers:

- what cap was declared?
- where is it visible?
- where is its source of truth?

Runtime answers:

- which caps are visible to this activation?
- which same-name definition wins after precedence is applied?

Rules:

- CLI and WebUI should manage authored cap definitions directly
- runtime APIs should project the effective visible cap set
- authored state and effective runtime state must not be collapsed into one
  mixed object


## 2. Cap Definition

A `cap definition` is one authored cap input.

Each definition carries:

- `kind`
- `name`
- `scope`
- `source`
- `locator`

Recommended management model:

- create, edit, and delete should target one specific cap definition
- the effective runtime set is derived from those definitions
- UI actions should not guess which authored definition to mutate from the
  effective runtime result alone

Reason:

- same-name caps may coexist across scopes
- authored definitions may also differ by source and locator
- destructive actions should identify one exact definition rather than a loose
  `(kind, name)` pair


## 3. Cap Kinds

Toolang currently manages four cap kinds:

- `psyche`
- `skill`
- `service`
- `prompt`

Kinds answer one question only:

- what sort of runtime input is this?

Kinds do not imply:

- visibility
- precedence
- materialization method
- remote or local storage


## 4. Cap Scopes

`scope` answers two questions at once:

- who can see this definition?
- how does it participate in same-name override precedence?

Canonical scopes:

- `agent`
  - visible only to the owning agent
  - typical authored source: `{AGENT_HOME}/{AGENT}.too`
- `home`
  - visible to all agents in the same agent home
  - typical authored source: `{AGENT_HOME}/agents.too`
  - typical local cap root:
    `${AGENT_HOME}/.toolang/{skills,services,prompts,psyches}/`
- `global`
  - visible to all agents under the Toolang root
  - typical authored source: `${TOOLANG_ROOT}/agents.too`
  - typical local cap root: `${TOOLANG_ROOT}/{skills,services,prompts,psyches}/`

Runtime precedence is:

1. `agent`
2. `home`
3. `global`

Rules:

- same-name caps in different scopes stay separate as authored definitions
- precedence is applied when building the effective runtime cap set
- shadowed definitions are still real definitions and may later become
  effective if higher-precedence definitions are removed

Legacy note:

- some current implementation surfaces still spell the `home` scope as
  `shared`
- new design vocabulary should use `home`


## 5. Cap Sources

`source` answers one question:

- where does the authoritative definition come from?

Canonical sources:

- `inline`
  - authored directly inside a Toolang program source file such as
    `{AGENT}.too` or `agents.too`
- `local`
  - authored as a local file or directory that already serves as the
    authoritative definition
- `remote`
  - authored as an external locator that must be resolved and materialized
    locally before runtime use

Important distinction:

- `local` and `remote` are not symmetrical storage flavors
- `local` already is the local source of truth
- `remote` points somewhere else and requires sync to obtain a local artifact
- `inline` is also authored locally, but its source of truth is the enclosing
  Toolang program rather than a standalone cap file

Old phrases such as:

- "home made"
- "global configured"

should not be modeled as separate source kinds.

They are better expressed as:

- `source = local`
- plus the appropriate `scope`


## 6. Cap Locator

`locator` is the canonical pointer to the authoritative definition.

It should unify older field ideas such as:

- `ref`
- `path`
- `source_file`

Meaning by source:

- `inline`
  - locator points to the containing program source and declaration location
- `local`
  - locator points to the authoritative local file or directory
- `remote`
  - locator points to the authoritative external resource

Rules:

- `locator` identifies the source of truth, not a cached byproduct
- if sync writes a local artifact for an `inline` or `remote` definition, that
  artifact path is derived state and should not replace the authoritative
  locator
- the exact locator syntax may vary by source family, but the concept should
  stay unified across CLI, WebUI, and API responses


## 7. Materialization And Sync

Toolang sync turns authored cap definitions into local runtime-ready
artifacts.

Source-specific behavior:

- `inline`
  - sync extracts or releases a local artifact from the enclosing program
    source
- `local`
  - the authoritative artifact is already local
  - sync may index or copy metadata, but it does not need to fetch a remote
    source of truth
- `remote`
  - sync resolves the remote locator, fetches the target, and materializes a
    local artifact

Rules:

- materialization is an implementation step, not a separate authored source
  category
- sync should preserve scope separation instead of collapsing same-name caps
  too early
- unchanged inputs should not require reparsing or refetching to rebuild the
  effective cap set

Current materialized roots remain scope-specific:

- `global`
  - `${TOOLANG_ROOT}/sync/`
- `home`
  - `${AGENT_HOME}/.toolang/sync/`
- `agent`
  - `${AGENT_HOME}/.toolang/agents/{AGENT}/sync/`


## 8. Markdown Cap Documents

Markdown-backed caps should treat raw authored text as the source of truth.

Conceptual model:

- `raw_text`
  - the exact authored Markdown document as loaded from a program block, local
    file, or fetched remote source
- parsed Markdown schema
  - `front_matter`
  - `body`

Rules:

- parsing must not rewrite or normalize `raw_text`
- hash and change detection for fetched or synced Markdown should derive from
  `raw_text`
- structured editing may parse Markdown into typed schema and synthesize a new
  document intentionally
- synthesis is not a license to rewrite unrelated remote content during sync
  or read-only projection

Current typed front-matter schema by kind:

- `service`
  - `transport`
  - `target`
  - `description`
  - `command`
  - `args`
  - `port`
  - `env`
- `prompt`
  - `description`
- `psyche`
  - `description`
- `skill`
  - `description`


## 9. Effective Cap Set

The `effective cap set` is the runtime-visible result after Toolang combines:

- visible definitions
- source-specific materialization
- same-name precedence

Recommended resolution flow:

1. collect authored cap definitions visible to the selected agent
2. materialize any `inline` and `remote` sources that need local artifacts
3. group candidates by `(kind, name)`
4. select one effective definition by scope precedence
5. mark the remaining candidates as shadowed

Rules:

- one activation starts with one effective visible cap set
- runs inherit the effective cap set of their activation
- steps may use prompts, skills, services, or psyches that came from that
  effective set
- `/api/v1/caps` should expose the effective runtime view, not the full
  authored cap inventory


## 10. Authoring Model For CLI And WebUI

CLI and WebUI should organize authoring around `cap definition`, not around the
effective runtime projection.

Recommended create flow:

1. choose `kind`
2. choose `scope`
3. choose `source`
4. provide the source-specific locator or content

Recommended edit and delete rules:

- target one specific definition id or one exact `(kind, scope, source,
  locator)` selection
- do not assume `(kind, name)` is unique enough for destructive actions
- when multiple same-name definitions exist, show which one is effective and
  which ones are shadowed

Recommended UI split:

- one authored-definition view for create, edit, sync, and delete
- one runtime-effective view for "what can this activation currently use?"

Path guidance:

- users should usually choose `scope`, not raw filesystem paths
- the system should derive the default path layout from `scope`
- raw path entry should remain an advanced escape hatch rather than the normal
  authoring flow


## 11. Remote Locator Resolution

The first supported remote registry family is GitHub.

One useful remote locator shorthand is:

- `owner/name`

Current GitHub probe rules:

- `service`
  - repos: `agent-services`, `services`
  - paths: `services/<name>.md`, `<name>.md`
- `prompt`
  - repos: `agent-prompts`, `prompts`
  - paths: `prompts/<name>.md`, `<name>.md`
- `psyche`
  - repos: `agent-psyches`, `psyches`
  - paths: `psyches/<name>.md`, `<name>.md`
- `skill`
  - repos: `agent-skills`, `skills`
  - paths: `skills/<name>/SKILL.md`, `<name>/SKILL.md`

Canonical layout for newly published GitHub caps:

- `service`
  - repo: `agent-services`
  - path: `services/<name>.md`
- `prompt`
  - repo: `agent-prompts`
  - path: `prompts/<name>.md`
- `psyche`
  - repo: `agent-psyches`
  - path: `psyches/<name>.md`
- `skill`
  - repo: `agent-skills`
  - path: `skills/<name>/SKILL.md`


## 12. Runtime API Projection

The runtime API exposes the effective visible cap set for the current
activation through:

- `GET /api/v1/caps`

Current response categories are:

- `psyches`
- `prompts`
- `skills`
- `services`
- `counts`

Current per-kind effective routes are:

- `GET /api/v1/psyches`
- `GET /api/v1/psyches/{name}`
- `GET /api/v1/prompts`
- `GET /api/v1/prompts/{name}`
- `GET /api/v1/services`
- `GET /api/v1/services/{name}`
- `GET /api/v1/skills`
- `GET /api/v1/skills/{name}`

Rules:

- the API uses `service` and `services`, not `server` and `servers`
- the payload is a runtime visibility view, not a full authored cap inventory
- counts should use the same names as the visible cap arrays
- per-kind detail routes describe one effective visible item, not every authored
  definition that may contribute candidates
- detail `content` should return the full raw authored document for that
  effective item
- Markdown detail payloads should not split `frontmatter` into a separate API
  field


## 13. Runtime Authoring Endpoints

The runtime API may also expose authored-definition mutations without turning
`GET /api/v1/caps` into an authored inventory endpoint.

Current authored cap mutation shape:

- `PUT /api/v1/caps/{kind}/{name}`
- `PUT /api/v1/{psyches|prompts|services|skills}/{name}`
  - requires explicit `scope`
  - accepts either:
    - local authored content via `content`
      - for Markdown caps, `content` carries the full raw document, including
        any front matter
    - remote authored attachment via `ref`
- `DELETE /api/v1/caps/{kind}/{name}`
- `DELETE /api/v1/{psyches|prompts|services|skills}/{name}`
  - requires explicit `scope`
  - should use explicit `source` when a local definition and a remote ref
    could both match the same `(kind, name, scope)`

Rules:

- authored mutations target one explicit cap definition
- effective runtime projection remains a separate read model
- UI flows should still distinguish:
  - authored local definitions
  - authored remote refs
  - runtime-effective visibility

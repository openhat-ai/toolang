# Prepared Layout

This note captures the current design direction for prepared state in the
runtime.


## Goals

- Keep root-level prepared artifacts shared across agents.
- Keep per-agent prepared paths short.
- Use one manifest file as the only durable prepared entrypoint.
- Keep local definitions in place.
- Materialize only inline and remote definitions into `.prepared`.


## Prepared Roots

Global prepared state:

```text
${TOOLANG_ROOT}/.prepared
```

Per-agent prepared state:

```text
${TOOLANG_ROOT}/agents/{agent}/.prepared
```


## Directory Layout

Global prepared layout:

```text
${TOOLANG_ROOT}/.prepared/
  lock.json
  inline/
    psyches/
    skills/
    services/
    prompts/
  remote/
    psyches/
    skills/
    services/
    prompts/
```

Agent prepared layout:

```text
${TOOLANG_ROOT}/agents/{agent}/.prepared/
  lock.json
  inline/
    psyches/
    skills/
    services/
    prompts/
    tasks/
    chores/
  remote/
    psyches/
    skills/
    services/
    prompts/
    tasks/
    chores/
```


## Lock File

`lock.json` is the only prepared manifest.

It should contain:

```json
{
  "scope": "global|agent",
  "updated_at": "...",
  "fingerprint": "...",
  "entries": []
}
```

Field meaning:

- `scope`
  - `global` for `${TOOLANG_ROOT}/.prepared`
  - `agent` for `${TOOLANG_ROOT}/agents/{agent}/.prepared`
- `updated_at`
  - the last successful prepare time
- `fingerprint`
  - identity of the prepared output represented by this lock file
- `entries`
  - all prepared runtime definitions for this scope

The manifest should only represent successful prepared output. Failed prepare
runs should not overwrite the active lock file.


## Entry Shape

Each entry should use this shape:

```json
{
  "kind": "prompt|service|psyche|skill|task|chore",
  "name": "rewrite",
  "shape": "file|dir",
  "locator": "...",
  "path": "...",
  "source": {
    "form": "local|inline|remote",
    "path": "...",
    "updated_at": "...",
    "fingerprint": "..."
  },
  "meta": {}
}
```

Field meaning:

- `kind`
  - the definition kind
- `name`
  - the logical runtime name
- `shape`
  - `file` for single-file definitions
  - `dir` for directory-backed definitions such as skills
- `locator`
  - the canonical identity of the definition
- `path`
  - the runtime entry file
- `source.form`
  - the authored form: `local`, `inline`, or `remote`
- `source.path`
  - the watched source path whose change requires re-prepare
- `source.updated_at`
  - the last observed update time of `source.path`
- `source.fingerprint`
  - content fingerprint of `source.path`
- `meta`
  - front matter parsed into JSON-friendly data


## Entry Path Rules

`path` always points to the runtime entry file.

Examples:

- file-backed prompt:
  - `prompts/rewrite.md`
- file-backed task:
  - `tasks/review.md`
- directory-backed skill:
  - `skills/reviewer/SKILL.md`

When `shape == "dir"`, assets may be discovered later from `path.parent`.

`locator` remains the canonical identity, not merely the runtime read path.


## Materialization Rules

- `local` definitions stay at their authoritative local path
- `inline` definitions are materialized under `.prepared/inline/...`
- `remote` definitions are materialized under `.prepared/remote/...`

Prepared materialization should preserve the same runtime file shape as local
definitions:

- prompts, services, psyches, tasks, and chores are file-backed
- skills are directory-backed, with `SKILL.md` as the entry file


## Conflict Rules

Conflicts are resolved during prepare, not during live load.

Within the same scope:

- if two entries share the same `(kind, name)` and have different `locator`
  values, prepare must fail

This keeps live loading simple and deterministic.


## Live Effective Set

Live loading reads the global and agent lock files and builds one effective
runtime set.

Caps use scope precedence:

1. `agent`
2. `global`

Jobs do not use name-based override rules in the current design.

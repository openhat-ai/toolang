# Prepared Lock

This document defines the intended `lock.json` format for prepared state.

Prepared state is derived from authored source files and is used directly by
the runtime. A prepared lock records three things:

- `sources`: authored inputs that decide whether prepared objects must be
  rebuilt.
- `artifacts`: materialized `.caps` files that decide whether local prepared
  files must be repaired.
- `prepared`: structured runtime objects built from sources and artifacts.


## Location

Prepared lock files live in prepared roots:

| Scope | Lock path | Path base |
| --- | --- | --- |
| Global | `${TOOLANG_ROOT}/.caps/lock.json` | `${TOOLANG_ROOT}` |
| Agent | `${TOOLANG_ROOT}/agents/<agent>/.caps/lock.json` | `${TOOLANG_ROOT}/agents/<agent>` |

The lock file does not store scope or agent name. Its location defines both.

All paths inside one lock are relative to that lock's path base. For example,
an agent lock uses `agent.too`, `config.toml`, and `skills/pdf/SKILL.md`.
A root lock uses `config.toml` and `skills/pdf/SKILL.md`.


## Format

```json
{
  "schema": 1,
  "built_at": "2026-05-24T10:00:00Z",

  "sources": {
    "program": {
      "path": "agent.too",
      "mtime": 1779616800000000000,
      "size": 1800,
      "fingerprint": "sha256"
    },
    "config": {
      "path": "config.toml",
      "mtime": 1779616800000000000,
      "size": 600,
      "fingerprint": "sha256"
    },
    "psyches": {
      "path": "psyches",
      "mtime": 1779616800000000000,
      "items": []
    },
    "skills": {
      "path": "skills",
      "mtime": 1779616800000000000,
      "items": [
        {
          "path": "skills/pdf",
          "shape": "dir",
          "mtime": 1779616800000000000,
          "items": [
            {
              "path": "skills/pdf/SKILL.md",
              "mtime": 1779616800000000000,
              "size": 900,
              "fingerprint": "sha256"
            }
          ]
        }
      ]
    },
    "services": {
      "path": "services",
      "mtime": 1779616800000000000,
      "items": []
    },
    "prompts": {
      "path": "prompts",
      "mtime": 1779616800000000000,
      "items": []
    },
    "tasks": {
      "path": "tasks",
      "mtime": 1779616800000000000,
      "items": [
        {
          "path": "tasks/daily-review.md",
          "shape": "file",
          "mtime": 1779616800000000000,
          "size": 500,
          "fingerprint": "sha256"
        }
      ]
    },
    "chores": {
      "path": "chores",
      "mtime": 1779616800000000000,
      "items": [
        {
          "path": "chores/sync-inbox.md",
          "shape": "file",
          "mtime": 1779616800000000000,
          "size": 550,
          "fingerprint": "sha256"
        }
      ]
    }
  },

  "artifacts": {
    "inline": {
      "path": ".caps/inline",
      "mtime": 1779616800000000000,
      "items": [
        {
          "path": ".caps/inline/prompts/summary.md",
          "shape": "file",
          "mtime": 1779616800000000000,
          "size": 300,
          "fingerprint": "sha256"
        }
      ]
    },
    "ref": {
      "path": ".caps/ref",
      "mtime": 1779616800000000000,
      "items": [
        {
          "path": ".caps/ref/skills/fund",
          "shape": "dir",
          "mtime": 1779616800000000000,
          "items": [
            {
              "path": ".caps/ref/skills/fund/SKILL.md",
              "mtime": 1779616800000000000,
              "size": 900,
              "fingerprint": "sha256"
            }
          ]
        }
      ]
    },
    "wired": {
      "path": ".caps/wired",
      "mtime": 1779616800000000000,
      "items": []
    }
  },

  "prepared": {
    "program": {
      "source": "program",
      "source_text": "agent alice\n\nuse skill github://coinbase/agentic-wallet-skills/skills/fund@<commit-sha>\n",
      "body_text": "use skill github://coinbase/agentic-wallet-skills/skills/fund@<commit-sha>\n",
      "uses": [
        {
          "kind": "skill",
          "ref": "github://coinbase/agentic-wallet-skills/skills/fund@<commit-sha>",
          "line": 12,
          "cap": 2
        }
      ],
      "structs": [
        {
          "name": "ReviewResult",
          "line": 20,
          "fields": [
            {
              "name": "summary",
              "type": "Text",
              "optional": false,
              "line": 21
            },
            {
              "name": "findings",
              "type": "ReviewFinding[]",
              "optional": false,
              "line": 22
            }
          ]
        }
      ],
      "instructs": [
        {
          "name": null,
          "line": 30,
          "content": "You are running {{thunk.name}}."
        }
      ],
      "caps": [
        {
          "kind": "prompt",
          "name": "summary",
          "line": 8,
          "cap": 1
        }
      ],
      "thunks": [
        {
          "name": "review",
          "line": 40,
          "params": [
            {
              "name": "input",
              "type": "Message",
              "optional": false
            },
            {
              "name": "path",
              "type": "Text",
              "optional": false
            },
            {
              "name": "focus",
              "type": "Text",
              "optional": true
            }
          ],
          "output": "ReviewResult",
          "directives": [
            {
              "key": "models",
              "op": "=",
              "values": ["gpt-5"],
              "line": 41
            },
            {
              "key": "skills",
              "op": "+=",
              "values": ["review", "patch"],
              "line": 42
            },
            {
              "key": "recall",
              "op": "=",
              "values": ["history", "memory"],
              "line": 43
            }
          ],
          "blocks": [
            {
              "kind": "instruct",
              "value": "default",
              "line": 44
            },
            {
              "kind": "context",
              "content": "Use the current review context.",
              "line": 46
            },
            {
              "kind": "user",
              "content": "Review {{path}} carefully.\n{{focus}}",
              "line": 49
            }
          ]
        }
      ]
    },

    "caps": [
      {
        "kind": "skill",
        "name": "pdf",
        "form": "file",
        "source": 0,
        "object": {
          "meta": {
            "description": "PDF processing skill"
          },
          "content": "# PDF\n\nUse this skill to process PDF files."
        }
      },
      {
        "kind": "prompt",
        "name": "summary",
        "form": "inline",
        "source": "program",
        "origin": {
          "line": 8
        },
        "artifact": 0,
        "object": {
          "meta": {},
          "content": "Summarize the current context."
        }
      },
      {
        "kind": "skill",
        "name": "fund",
        "form": "ref",
        "source": "program",
        "origin": {
          "line": 12,
          "ref": "github://coinbase/agentic-wallet-skills/skills/fund@<commit-sha>",
          "provider": "github",
          "repo": "coinbase/agentic-wallet-skills",
          "path": "skills/fund",
          "commit": "<commit-sha>"
        },
        "artifact": 0,
        "object": {
          "meta": {
            "description": "Wallet funding skill"
          },
          "content": "# Fund\n\nUse this skill to fund a wallet."
        }
      },
      {
        "kind": "skill",
        "name": "web",
        "form": "wired",
        "source": "config",
        "origin": {
          "ref": "github://acme/agents/skills/web@<commit-sha>",
          "provider": "github",
          "repo": "acme/agents",
          "path": "skills/web",
          "commit": "<commit-sha>"
        },
        "artifact": 0,
        "object": {
          "meta": {
            "description": "Web research skill"
          },
          "content": "# Web\n\nUse this skill for web research."
        }
      }
    ],

    "tasks": [
      {
        "kind": "task",
        "name": "daily-review",
        "form": "file",
        "source": 0,
        "object": {
          "meta": {
            "title": "Daily review",
            "state": "open"
          },
          "content": "Review new changes and summarize blockers."
        }
      }
    ],

    "chores": [
      {
        "kind": "chore",
        "name": "sync-inbox",
        "form": "file",
        "source": 0,
        "object": {
          "meta": {
            "title": "Sync inbox",
            "state": "active",
            "schedule": {
              "rrule": "FREQ=HOURLY;INTERVAL=1"
            }
          },
          "content": "Check inbox and prepare triage summary."
        }
      }
    ]
  }
}
```

Keys may be omitted when they do not apply to a scope. For example, a root
lock normally omits `sources.program`, `sources.tasks`, and `sources.chores`.
An absent source directory may be omitted or represented with an empty
`items` array.


## Source Records

`sources` describe authored files.

Top-level file sources use:

| Field | Meaning |
| --- | --- |
| `path` | Scope-relative file path |
| `mtime` | Filesystem mtime in nanoseconds |
| `size` | File size in bytes |
| `fingerprint` | Content fingerprint, normally SHA-256 |

Directory sources use:

| Field | Meaning |
| --- | --- |
| `path` | Scope-relative directory path |
| `mtime` | Directory mtime in nanoseconds |
| `items` | Authored items under the directory |

Directory `mtime` is diagnostic and may be used as an optimization hint. It is
not a correctness boundary. Correctness comes from comparing item path sets
and file fingerprints.

Source items may be files or directories:

```json
{
  "path": "prompts/review.md",
  "shape": "file",
  "mtime": 1779616800000000000,
  "size": 400,
  "fingerprint": "sha256"
}
```

```json
{
  "path": "skills/pdf",
  "shape": "dir",
  "mtime": 1779616800000000000,
  "items": [
    {
      "path": "skills/pdf/SKILL.md",
      "mtime": 1779616800000000000,
      "size": 900,
      "fingerprint": "sha256"
    }
  ]
}
```


## Artifact Records

`artifacts` describe files written under `.caps`.

Artifact buckets are:

| Bucket | Meaning |
| --- | --- |
| `inline` | Files materialized from inline program declarations |
| `ref` | Files materialized from program `use` refs |
| `wired` | Files materialized from config refs |

Artifact items use the same `shape` model as source items. A directory
artifact owns a nested file manifest. This lets one prepared cap point to one
artifact index even when the artifact contains multiple files.


## Prepared Records

`prepared` contains structured runtime data.

`prepared.program` is a program-specific structure based on
`tree-sitter-toolang` concepts. It does not use `object.meta` or
`object.content`.

Program fields are:

| Field | Meaning |
| --- | --- |
| `source` | Always `program`, referring to `sources.program` |
| `source_text` | Full prepared program source text |
| `body_text` | Program body after the optional agent header and shebang are removed |
| `uses` | Program `use` items |
| `structs` | Program `struct` items |
| `contexts` | Top-level `context` items |
| `instructs` | Top-level `instruct` items |
| `caps` | Program `psyche`, `skill`, `service`, and `prompt` items |
| `thunks` | Program `thunk` items |

Program cap items use `caps`, not `declarations` or `definitions`, because the
collection specifically describes program-level cap items. Each item may point
to the corresponding runtime cap with `cap`, an index into
`prepared.caps`.

Program `use` items may also point to the ref prepared cap with `cap`,
an index into `prepared.caps`.

Thunk fields follow grammar names:

| Field | Meaning |
| --- | --- |
| `params` | Thunk `params`, preserving source order |
| `output` | Thunk output type |
| `directives` | Thunk `directive` items |
| `blocks` | Thunk `block` items |

Use `directives`, not `overlays`, and `blocks`, not `messages`, in the lock
format. Those names match `tree-sitter-toolang`; overlay and message concepts
are runtime projections.

Type strings use the canonical source spelling from the grammar. Built-in
types include `Text`, `Number`, `Boolean`, `Json`, and `Message`. User type
names such as `Path`, `Artifact`, or `ReviewResult` use the same spelling as
the source. Array suffixes are preserved, for example `ReviewFinding[]`.

Caps, tasks, and chores use the same object envelope:

```json
{
  "object": {
    "meta": {},
    "content": ""
  }
}
```

`meta` stores structured fields such as frontmatter, state, schedule, transport,
or description. `content` stores the body text after metadata is removed.

Prepared item fields are ordered as:

```text
kind, name, form, source, origin, artifact, object
```

Only meaningful fields are stored:

- `file` items omit `origin` and `artifact`.
- `inline`, `ref`, and `wired` items include `origin` when source
  positioning or ref resolution matters.
- Items with `.caps` output include `artifact`.


## Source References

`source` identifies the source that rebuilds a prepared item.

For file caps, numeric `source` values select an item from the source bucket
implied by `kind`:

| Kind | Source bucket |
| --- | --- |
| `psyche` | `sources.psyches.items` |
| `skill` | `sources.skills.items` |
| `service` | `sources.services.items` |
| `prompt` | `sources.prompts.items` |

For jobs:

| Prepared collection | Source bucket |
| --- | --- |
| `prepared.tasks` | `sources.tasks.items` |
| `prepared.chores` | `sources.chores.items` |

String source values select top-level sources:

| Value | Meaning |
| --- | --- |
| `program` | `sources.program` |
| `config` | `sources.config` |

Examples:

- A file skill with `"source": 0` depends on `sources.skills.items[0]`.
- A ref cap with `"source": "program"` depends on `sources.program`.
- A wired config cap with `"source": "config"` depends on `sources.config`.


## Artifact References

`artifact` identifies the materialized output that should be repaired from a
prepared item when `.caps` files are changed.

The artifact bucket is implied by `form`:

| Form | Artifact bucket |
| --- | --- |
| `inline` | `artifacts.inline.items` |
| `ref` | `artifacts.ref.items` |
| `wired` | `artifacts.wired.items` |

Examples:

- An inline prompt with `"artifact": 0` owns `artifacts.inline.items[0]`.
- A ref skill with `"artifact": 0` owns `artifacts.ref.items[0]`.
- A wired skill with `"artifact": 0` owns `artifacts.wired.items[0]`.


## Origin

`origin` records extra provenance that is not already represented by `source`
or `artifact`.

For program-derived objects:

```json
{
  "line": 12
}
```

For remote objects:

```json
{
  "line": 12,
  "ref": "github://coinbase/agentic-wallet-skills/skills/fund@<commit-sha>",
  "provider": "github",
  "repo": "coinbase/agentic-wallet-skills",
  "path": "skills/fund",
  "commit": "<commit-sha>"
}
```

Canonical remote refs should resolve branch or shorthand refs to an immutable
revision, such as a Git commit SHA. The commit identifies the remote snapshot.
It does not replace the local artifact manifest; local files still need mtime,
size, and fingerprint records to detect local edits or deletion.


## Comparison Rules

Lock reuse and repair are a two-stage process.

First compare actual authored files with `sources`:

1. Scan the relevant authored paths and build a fresh lightweight manifest.
2. Compare path sets to find added and deleted files or source items.
3. For paths present in both manifests, compare `mtime` and `size`.
4. Recompute `fingerprint` only for added files or files whose `mtime` or
   `size` changed.
5. If source records changed, rebuild only prepared records that reference the
   changed source.

Then compare actual `.caps` files with `artifacts`:

1. Scan `.caps/inline`, `.caps/ref`, and `.caps/wired`.
2. Compare path sets to find added and deleted artifact files.
3. For paths present in both manifests, compare `mtime` and `size`.
4. Recompute `fingerprint` only for added files or files whose `mtime` or
   `size` changed.
5. If artifact records changed, find prepared records that reference the
   changed artifact and rewrite the artifact from prepared data.

If sources changed, prepared data is no longer authoritative for affected
items. Rebuild those prepared records first, then write or repair artifacts
from the rebuilt prepared data.

If only artifacts changed, sources and prepared data remain authoritative.
Repair the changed artifacts from existing prepared records.


## Directory Change Detection

Directory `mtime` is not sufficient to detect all changes in a directory tree.
It may change when direct children are added or removed, but it may not change
when a descendant file changes.

Directory correctness must use item manifests:

- Added and deleted files are detected by comparing path sets.
- Candidate modifications are detected by comparing `mtime` and `size`.
- Content changes are confirmed by recomputing `fingerprint`.

For directory source or artifact items, compare nested `items` the same way.


## Update Indexes

Implementations may build transient indexes from the lock:

```text
source reference -> prepared records
artifact reference -> prepared records
```

These indexes do not need to be stored in `lock.json`. They can be rebuilt from
`prepared` whenever the lock is loaded.

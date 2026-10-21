# Caps

Caps are composable agent primitives: psyches, skills, services, and prompts.



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

CLI query records expose scope through `tags`; HTTP read payloads retain the
`scope` field.

`too caps` and untargeted kind-specific lists show root-shared resources
filtered by root cap-kind allow policy. `too alice caps` combines root resources
with Alice's home and program capabilities using the precedence above, then
reads the main agent module's published effective allow selection. Other agents'
private resources, including an implicit `default` agent, are outside that view.
Root inspection prepares or reuses the shared root State layer, including resolved
remote metadata, before applying allow and query filters. It never prepares an
agent home. Remote content follows the same cache and refresh behavior as agent
State preparation.

`--all` includes resources excluded by allow in the same scope. Both views
show `CAP`, `DESCRIPTION`, `LOCATION`, and `TAGS` in one row per cap, following
the [CLI table width policy](cli.md) with 120 cells when geometry is unavailable.
Descriptions and locations
shrink first; long cells use `…`, with locations retaining their beginning and
end. Complete values remain available through `--json`; there is no `--long`.
Availability is `ready` or
`not_allowed`, alongside origin, scope, and form tags. The full view does not restore
shadowed definitions or grant runtime access. This applies to aggregate and
kind-specific lists through `too`, for example `too alice caps --all`,
`too alice skill list --all`, and `too psyche list --all`. Queries filter the
chosen default/full view. Caps have no separate readiness protocol; an invalid
or unresolvable definition remains an error, not an invented unavailable row.
`-a` aliases `--all`. Aggregate summaries use `N caps, M kinds`; omit the kind
count for zero or one cap. Kind-specific summaries use their own noun, such as
`N skills`. Empty human results print `0 caps` or `0 skills`; JSON output is `[]`.
Counts describe displayed rows after filtering.
Per-module and run declarations can further narrow execution resources.

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

CLI listing resolves current capability declarations without validating runnable
handlers or publishing executable agent State. Runtime APIs read the latest
successfully published State.

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

The `too caps` and kind-specific list commands share these columns in this
order:

| Column | Meaning |
| --- | --- |
| `CAP` | Cap Ref for queries, such as `skill/reviewer` |
| `DESCRIPTION` | Cap description, or `-` when absent |
| `LOCATION` | Absolute content path, inline `file:line`, or configured/referenced HTTP(S) URL |
| `TAGS` | Availability, access, origin, scope, and form tags |

Use repeatable `--query/-q` with native TQ. Both combined and kind-specific
lists match complete singular identities such as `skill/reviewer`; use
`*/reviewer` across kinds. For example, `skill/*[tags has all (home,remote)]`
selects remote skills in home scope. `--json` exposes the exact records used by
queries. JSON and query keys are lowercase; `--human` uppercases them for table
headers. Locations remain complete for copying; authored skills point to
`SKILL.md`, and only inline locations include a line number. See
[Resource Queries](queries.md).


## Source Refs

CLI query/JSON records and API summaries/details expose `cap` (`kind/name`)
and `source` (the resolved, canonical Source Ref). `location` is a separate
human-facing content address. Default queries match `cap`; bare
`skill/reviewer` still selects that identity. External TQ consumers use `cap`
instead of `ref` as their identity key; API response `ref` becomes `source`,
with `cap` added. Use identity syntax such as `skill/reviewer`, not a
`*[cap="skill/reviewer"]` predicate.
Configured write requests still use `ref` for the supplied Source Ref.

| Ref | Meaning |
| --- | --- |
| `inline://prompts/reviewer` | Embedded inline cap definition |
| `home://services/github` | Local capability in the agent home |
| `root://skills/reviewer` | Local capability under the Toolang root |
| `github://user/repo/path/name.md@rev` | Remote cap target |

GitHub cap refs must include `@rev`. Shorthand Source Refs such as `gh:owner/name`
probe the matched repository's default branch. Acquisition then pins that
selector to a full commit SHA in State.
Three-part shorthand such as `gh:owner/repo/name` specifies the repository exactly
and probes only paths inside that repository.

Current cap shorthand probe rules are:

| Kind | Input | Probe order |
| --- | --- | --- |
| `skill` | `gh:owner/name` | `github://owner/agents/skills/name@<default-branch>`, `github://owner/agent-skills/name@<default-branch>`, `github://owner/agent-skills/skills/name@<default-branch>`, `github://owner/skills/name@<default-branch>`, `github://owner/skills/skills/name@<default-branch>` |
| `psyche` | `gh:owner/name` | `github://owner/agents/psyches/name.md@<default-branch>`, `github://owner/agent-psyches/name.md@<default-branch>`, `github://owner/psyches/name.md@<default-branch>` |
| `service` | `gh:owner/name` | `github://owner/agents/services/name.md@<default-branch>`, `github://owner/agent-services/name.md@<default-branch>`, `github://owner/services/name.md@<default-branch>` |
| `prompt` | `gh:owner/name` | `github://owner/agents/prompts/name.md@<default-branch>`, `github://owner/agent-prompts/name.md@<default-branch>`, `github://owner/prompts/name.md@<default-branch>` |

For `gh:owner/repo/name`, Toolang uses the specified repository and probes only
kind-specific paths in that repository. Agents use `agents/name.too`, then
`name.too`. Skills use `skills/name`, then `name`, except dedicated
`agent-skills` and `skills` repositories prefer `name` first. File-backed caps
use `{kind}s/name.md`, then `name.md`, except dedicated cap repositories prefer
`name.md`.

`github:` aliases `gh:`. Exact `gh:owner/repo/path@rev` and
`github:owner/repo/path@rev` normalize to `github://` without shorthand probes.
Bare remote Source Refs, `oh:`, and `openhat:` are unsupported. This restriction
does not apply to Cap Ref queries (`skill/reviewer`) or local Source Refs.
HTTP(S) URLs fetch file-backed Caps directly. For skills, use a directory URL or
its `SKILL.md` URL; HTTP acquisition fetches only `SKILL.md`, while GitHub skill
sources include the directory's files. `with skill SOURCE-REF` supplies the kind
separately; source paths are ordinary provider paths.

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

Capabilities live inside immutable State layer revisions:

- `source/<original-relative-path>` preserves accepted authored files.
- `materialized/<acquisition-key>/<original-relative-path>` holds fetched files
  and assets, shared across consuming modules and aliases.
- `layer.json` holds parsed metadata and bodies for every accepted candidate.
  Inline declarations produce no generated Markdown files.

The acquisition key is SHA-256 of the canonical resolved Source Ref. GitHub
versions pin a full commit SHA before fetching; HTTP(S) refreshes compare fetched
bytes. Source owners control normalization and selection, so new sources require
no State-specific layout rules.

Root layers live under `${TOOLANG_ROOT}/.state/root/revs/<revision>`.
Home layers live under
`${TOOLANG_ROOT}/agents/<agent>/.state/home/revs/<revision>`. See
[State](state.md) for documents, times, and integrity rules.

Inline skills retain literal guidance and description directly in their Cap
value. Their inspection location is the authored `.too` file and declaration
line; they have no bundled asset list. See
[inline cap syntax](program.md#inline-caps) for declaration and selection.


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

`AgentState` captures the complete durable capability set. State
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


## HTTP API

[Cap endpoints](api.md#cap-endpoints) owns routes, request/response fields,
publication receipts, and conflict handling. Each kind has one bundled `default`
template. Runtime reads expose the effective cap view; source mutations address
`root` or `home` and await State publication without rebinding accepted Runs.
[Queries](queries.md) owns the CLI record shape and tag vocabulary.

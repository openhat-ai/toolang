# Define Directly Usable State Layers

## Status and Goal

Approved in #813 and implemented with layer schema 22 and composition schema 3.
Each revision is readable as a directory, directly usable from its JSON document,
verifiable offline, and comparable with upstream content. See
[Agent State](../agent-state.md) and the
[Source Ref contract](names-handles-and-source-refs.md).
GitHub aliases and HTTP(S) are supported; this work does not activate OpenHat.

## Scope

Change the persisted layer/composition formats, preparation, restoration, remote
refresh comparisons, and explicit integrity validation. Preserve source acceptance,
scope precedence, allow policies, public commands, and source acquisition rules.
Make State independent of the Source schemes and default selection policy.
Do not implement OpenHat acquisition, remote Mod imports, a source plugin
framework, a history database, or a new inspection command. CLI/API `cap`, `source`,
`definition_file`, and source-write/sync receipts keep their current contracts;
the item fields below describe persistence, not a public projection rename.

## Layout and File Records

Keep the existing root/home/agent stores, locks, revision directories, and
`current` pointers. A root or home revision contains:

```text
revs/<revision>/
  layer.json
  source/
    config.toml
    agent.too
    runnables/check.too
    prompts/style.md
    skills/review/SKILL.md
  materialized/
    <acquisition-key>/<original-relative-path>
```

Only files belonging to that scope appear. `source/` preserves accepted captured
paths and raw bytes, including config comments and line endings; configuration
projection/path resolution happens separately. Invalid inputs have records but
no stored bytes. Missing/default Programs do not create synthetic source files.
The raw config copy intentionally replaces the old projected TOML artifact.

`materialized/` contains fetched bytes and their assets, never generated inline
Markdown or directories named after consuming modules, Cap forms, or aliases.
The acquisition key is the full lowercase SHA-256 of the UTF-8 canonical
`resolved_ref`. Source owners supply the entry path and original relative file
paths; State validates portable paths, not Source-specific layouts. Preserve the
whole fetched directory when supplied. Single-file acquisition supplies one
filename. No GitHub/HTTP/OpenHat-specific directory rules belong in State.

Identical acquisition keys may share files. A key with conflicting bytes is an
error, never a last-writer-wins overwrite. Distinct sources, queries, versions,
or repository paths cannot collide merely because their local Cap names match.

## Layer Document

| Field | Contents |
| --- | --- |
| `schema` | New layer schema version. |
| `scope` | `root` or `home`. |
| `created_at` | UTC creation time described below. |
| `config` | This layer's normalized State-owned configuration and policy. Cap declarations move into `caps`; preserve workspace ordering. |
| `modules` | Named Program items; empty in root. |
| `caps` | All accepted shared and module-local Cap items in this layer. |
| `chores` | Effective home chores after Markdown/agent-inline precedence; empty in root. |
| `source` | Map from observed scope-relative input paths to file records. |
| `materialized` | Map from fetched paths relative to `materialized/` to file records. |

Each successful file record is `{size, sha256}` for exact raw bytes. A rejected
`source` record adds `error: {code, message, line?}`; an unreadable entry has null
size/digest. Absence of `error` means accepted. Deleted inputs are absent. These
two maps replace `source`/`sources`/`files` manifests and duplicate raw/prepared
digests; there is no separate `accepted` flag or per-resolution file manifest.

Items share `{ref, source, value, resolution?}`; Cap items also require `form`.
`source` is a key in the layer's `source` map, not a Source Ref or absolute path.
Only the structural default agent module may have a null `source`.

| Collection | `ref` | `value` |
| --- | --- | --- |
| `modules` | Existing stable module name: `agent` or `runnables::<name>`. | Persisted `Program`. |
| `caps` | Cap Ref: `kind/name`. | Parsed `meta` and `body`; no frontmatter parsing on normal use. |
| `chores` | `chore/<name>`. | Normalized `title`, `body`, and `schedule`. |

Do not introduce a public Mod Ref syntax. Cap `form` remains exactly `authored`,
`configured`, `inline`, or `referenced`. Module ownership for inline/referenced
Caps follows their `.too` source key and the module index. Runtime module kinds,
exports, source digests, Cap classifications, and existing public projections
are reconstructed from the persisted items and maps. Persist no duplicate
`kind`, `name`, `module`, `here_caps`, remote-ref alias, placeholder source
timestamp, or source fingerprint. Reconstruct existing fingerprints from
source/resource identities without using the layer creation time.

For skill assets, the authored `SKILL.md` parent or resolved materialized entry
parent defines the asset directory in the corresponding map. Inline skills have
no bundled assets. Asset reads may open stored files; Cap text, configuration,
Programs, and chore definitions load directly from JSON. Inline declarations,
including prompt parameters, remain in their Program for authored semantics;
extracted Cap/chore values provide direct text/metadata access without parsing
authored syntax again.
Explicit raw-file/detail requests may read captured bytes to preserve existing
raw content responses; ordinary summaries and semantic use consume `value`.

Remote configured/referenced Caps have one complete resolution object:

```yaml
ref: skill/review
form: referenced
source: agent.too
resolution:
  declared_ref: gh:owner/review
  resolved_ref: github://owner/skills/review@<full-commit-sha>
  materialized: <acquisition-key>/review/SKILL.md
value:
  meta:
    description: Review changes.
  body: Review changes carefully.
```

`resolution.materialized` selects an actual entry in the materialized map.
All three resolution fields occur together; local authored/inline Caps omit the
object. Acquired Mod provenance stays owned by `up/source.py`; passing exact
fetched bytes and resolution to a visiting module's State item must preserve the
same acquired Program and existing visiting-cache lifecycle. Its captured
`agent.too` remains a source key; this format does not change Mod discovery.

Keep distinct same-name Cap candidates as array items. Validate conflicts within
the existing scope before publishing; never deduplicate the new logical `ref`
alone. Root/home/shared definitions survive overrides in another scope because
other agents/modules may use them. Apply `root < home < here` and allow policies
when composing runtime views. `shadowed` is context-dependent inspection data,
not a persisted item property. Config is layer-local; chores are already selected
within home, with shadowed raw declarations retained in source/Program data.

## Source Boundary and Refresh

Source parsing/acquisition owns aliases, shorthand probes, default Source
selection, upstream version semantics, and fetching. It returns the original
`declared_ref`, a fully qualified canonical `resolved_ref`, an entry path, and a
map of exact fetched bytes. Canonical refs distinguish the Source, origin/registry
when applicable, and complete resource identity; aliases for one target produce
the same ref and stable entry layout. Sources deliver the existing Mod/Cap
content formats, so the owning
language/Cap parser is selected by item type, not transport. State consumes this
normalized result; it must not parse schemes, whitelist them, or branch on
provider names. Adding a content type is separate from adding a Source.

`resolved_ref` pins an immutable upstream revision when the Source supports one.
For mutable locations, captured file digests lock the actual bytes. Before any
current-State fast reuse, the Source owner checks whether the recorded result is
reusable under the effective source selection/configuration. Unchanged local
digests or an unchanged bare declaration alone cannot authorize reuse after a
default/configuration change. Ordinary preparation may keep recorded versions
without checking upstream movement; forced refresh asks the Source owner to check
again. State compares the returned ref, file set, and raw digests. On refresh, an
owner may reuse intact bytes after proving the upstream revision unchanged;
equal mutable locations alone do not prove no update is needed. One preparation
must not mix acquisitions for the same canonical key.

This definition requires the following behavior from existing Source owners:

- GitHub aliases resolve to a full commit SHA before fetching every file at that
  SHA. Resolve a repository/selector once per attempt and bypass selector caches
  on forced refresh. A changed commit changes provenance even for equal bytes.
- HTTP(S) keeps its canonical URL, including query, and refetches on forced
  refresh to compare bytes. Recognized GitHub URLs follow GitHub resolution.
  HTTP skills still fetch only `SKILL.md`; GitHub skills retain directory assets.

Adding a Source changes its parser/acquisition owner, not State schemas,
persistence, restoration, comparison, validation, or materialized path rules.
OpenHat (`oh:`/`openhat:`) is the intended default Source when its acquisition is
implemented. That default selection belongs at the Source boundary, not in State.
State preserves the user's original declaration while storing the fully qualified
result, including when a declaration omits a future default prefix. Defaults
affect new preparation only: existing Runs and historical/offline loads use the
captured values and refs without consulting current source selection. No
duplicated `provider`, `commit`, or `version` fields are needed.

The merged parser still rejects unsupported schemes before network access; this
plan does not activate OpenHat syntax. A failed fetch creates no partial
resolution and never substitutes old content as a successful new definition;
existing resource-local exclusion/fatal-config rules remain.

## Time, Identity, and Verification

Use fixed-width UTC RFC 3339 timestamps with microseconds, for example
`2026-10-15T08:30:00.000000Z`. Set `created_at` when writing a new revision and
include it in the existing canonical JSON encoding and whole-document hash.
The agent composition's `layers.json` also gains `created_at`; retain its other
inputs (layer revisions, name, overrides, workspace additions).

Before assigning time, compare the candidate document, excluding `created_at`,
with the current document. Equal candidates reuse the complete current revision
and time, including on a no-op forced refresh. Changed candidates receive a new
time and hash. Returning to earlier content after an intervening change creates
a new revision. Composition loading uses its stored document/time rather than
recomputing identity from layer revisions alone. Keep atomic writes and existing
locks; no separate metadata file, counter, or history index is needed.

JSON object keys and unordered item arrays are canonically sorted; preserve
semantic array order. Normal loading checks the stored value schemas/references
without hashing every file. Neither loading nor explicit integrity validation
parses Source Refs, loads resolvers, reads current configuration, accesses the
network, or reinterprets source syntax. Explicit offline validation requires:

1. Canonical document bytes and matching revision hashes, including time.
2. Exact directory file sets: error-free source entries and every materialized
   entry, with matching sizes/digests; reject symlinks and escaping paths.
3. Valid item sources, resource entry/asset membership, scope/module ownership,
   acquisition keys, and resolution/path consistency, without a scheme whitelist.
4. Valid stored value schemas and recursively valid root/home revisions for a
   composition. The document hash protects `value` and resolved configuration;
   integrity validation does not rerun parsing or environment-dependent path
   resolution to reproduce them. Preparation tests verify interpretation.

Rejected bytes are not stored, so verification authenticates their observation
records through the layer hash but cannot rehash absent originals. These hashes
verify stored content against a revision, not remote publisher authenticity.

To list new home revisions by recorded creation time from an agent home:

```sh
jq -r 'select(.created_at != null) | [.created_at, input_filename] | @tsv' \
  .state/home/revs/*/layer.json | sort
```

Time describes local creation, not strict causality under clock changes.

## Compatibility and Implementation Touchpoints

Old layer and composition schemas are unsupported; no compatibility or historical
migration is required. Derived current caches are rebuilt from authored inputs.
Historical directories and execution records are not converted or rewritten.
Raw config archival and timestamped identities are intentional format changes.

| Files | Responsibility |
| --- | --- |
| `common/github.py`, `common/source.py` | Source syntax/default boundary; current syntax unchanged. |
| `up/source.py`, existing Cap acquisition code | Source-specific fetch/provenance behind the common result; visiting-cache handoff. |
| `state/source.py`, `acceptance.py`, `config.py` | Raw capture, records, policy. |
| `state/state.py`, `prepare.py` | Direct values, Source-owned cache eligibility, generic comparison, reconstructed runtime fields. |
| `state/cache.py` | Schemas, maps, timestamps, reuse, persistence, validation. |
| `state/schemas.py`, `collections.py` | Preserve public projections without routine Cap text reads. |
| `docs/agent-state.md`, `caps.md`, `layout.md`, `CHANGELOG.md` | Update current behavior documentation with implementation. |

## Acceptance and Risks

Acceptance tests must cover:

- Direct JSON restoration with source/materialized reads prohibited; explicit
  asset reads, raw config preservation, rejected/unreadable inputs, defaults,
  and unchanged public receipts/projections.
- Same-name module Caps and existing same-scope conflicts; shared fetches and
  differing Sources/repos/paths/queries without overwrite or incorrect deduplication.
- GitHub branch/tag movement, consistent pinned fetches, equal-content changed
  commits, no-op refresh, HTTP changed bytes, and visiting Mod reuse.
- A synthetic new Source and a default/configuration change with identical local
  inputs bypass stale cache reuse without State changes; explicit refs stay
  unaffected by a default change. Frozen Runs and unknown schemes restore/validate
  offline with resolver/parser/network access prohibited; current acquisition
  still rejects unsupported OpenHat sources before network.
- All-store timestamps, no-op reuse, A/B/A evolution, concurrent publication,
  document/file tampering, extra/missing files, bad references, invalid value
  schemas, and recursive validation fail or pass as specified above.
- New-format historical loads, old-schema rejection, documented examples, and
  the default offline verification suite.

Raw config capture expands archived content compared with schema 21; its byte
preservation must be deliberate. Direct values add JSON size but remove routine
parsing. Timestamped identities require auditing all pure revision computations;
filesystem mtime and provider versions must not substitute for stored identity.
HTTP requires fetching to detect updates and cannot provide Git-like version
history. No protocol decisions remain open.

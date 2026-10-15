# Define Directly Usable State Layers

## Goal and Scope

Approved in #813. Layer schema 22 and composition schema 3 make each revision
usable from JSON, readable with file tools, verifiable offline, and comparable
with newly acquired upstream content. The current behavior is documented in
[Agent State](../agent-state.md).

Change persistence, preparation, restoration, refresh comparison, and integrity
validation. Preserve acceptance rules, scope precedence, allow policies, public
commands and projections, and the visiting Mod cache lifecycle. Source parsing
and default selection follow the [Source Ref contract](names-handles-and-source-refs.md).
GitHub aliases and HTTP(S) are supported. OpenHat acquisition, remote Mod
imports, and a Source plugin framework are outside this change.

## Stored Layout

Keep the root/home layer stores, agent composition store, locks, revision
directories, and `current` pointers. Each root or home revision contains:

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

Only inputs belonging to that scope appear. `source/` preserves accepted captured
paths and exact bytes, including config comments and line endings. Configuration
projection and path resolution are separate from archival. Rejected inputs have
records but no stored copy. A missing/default Program creates no source file.

`materialized/` preserves complete fetched acquisitions, including supplied
assets. Entry and file paths come from the Source owner; they are portable and
relative, with no directory prefix based on consuming modules, Cap forms, or
aliases. Inline declarations remain JSON values and produce no Markdown files.
The acquisition key is the full lowercase SHA-256 of the UTF-8 canonical
`resolved_ref`. Consumers may share a key only when the complete file set and
bytes agree. A conflicting capture is an error under the existing
resource-local exclusion and fatal-config rules.

## Layer Document

| Field | Contents |
| --- | --- |
| `schema` | `22`. |
| `scope` | `root` or `home`. |
| `created_at` | Fixed-width UTC creation time with microseconds. |
| `config` | Normalized State-owned configuration and policy for this layer; preserve workspace order. |
| `source` | Observed scope-relative input paths mapped to file records. |
| `materialized` | Paths relative to `materialized/` mapped to file records. |
| `modules` | Named Program items; empty in root. |
| `caps` | All accepted shared and module-local Cap items. |
| `chores` | Effective home chores after Markdown/agent-inline precedence; empty in root. |

A successful file record is `{size, sha256}` for exact raw bytes. A rejected
source adds `error: {code, message, line?}`. An unreadable source has null size
and digest. Absence of `error` means accepted; deleted inputs are absent.
These two maps are the complete persisted file inventory.

Items share `{ref, source, value, resolution?}`. Caps also require `form`.
`source` is a key in the source map. Only the empty structural `agent` module
has `source: null`, when its source is missing or excluded.

| Collection | `ref` | `value` |
| --- | --- | --- |
| `modules` | `agent` or `runnables::<name>`. | Parsed `Program`. |
| `caps` | Cap Ref: `kind/name`. | Parsed `meta` and `body`. |
| `chores` | `chore/<name>`. | Normalized `title`, `body`, and `schedule`. |

Cap `form` is `authored`, `configured`, `inline`, or `referenced`. Configured
Cap declarations live in `caps`; the `config` object contains the remaining
State-owned fields. Module ownership follows the item's `.too` source key and
the module index. Existing runtime classifications, exports, digests, source
receipts, and public projections are reconstructed from these records.

Keep accepted candidates from different scopes/modules as separate items.
Existing same-scope conflicts still reject preparation. Apply `root < home <
here` and allow policies when composing runtime views. `shadowed` is computed
for inspection context. Chores are already selected within home; their raw
shadowed declarations remain in captured source/Program data.

Programs retain inline declarations, including prompt parameters. Cap and chore
values provide direct semantic access without parsing source again. Cap text,
configuration, Programs, and chores load from JSON. Explicit raw details and
skill asset reads may open captured files. An authored skill's `SKILL.md` parent
or a fetched skill's materialized entry parent defines its asset directory;
inline skills have no bundled assets.

Fetched Caps and acquired visiting modules carry a complete resolution:

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

`resolution.materialized` selects an actual materialized map entry. All three
resolution fields occur together. Local authored/inline items omit resolution.
A visiting module keeps its captured `agent.too` source and the exact fetched
bytes supplied by `up/source.py`.

## Source Ownership and Refresh

The Source owner handles aliases, shorthand probes, default selection, upstream
versions, and fetching. It supplies the original declaration, a fully qualified
canonical resolved ref, an entry path, and exact fetched files. Canonical refs
distinguish the Source, origin/registry, resource, and immutable version when
available. Mutable locations retain their canonical URL and are locked by file
digests. Aliases for one acquisition produce the same ref and file layout.

State consumes this result without parsing schemes or choosing a provider.
Adding a Source or making OpenHat (`oh:`/`openhat:`) the default changes its
parsing/acquisition owner, not State persistence, restoration, validation, or
path rules. Content formats remain owned by the language/Cap parser.

Before current-State fast reuse, including watcher checks with unchanged local
files, the Source owner validates the recorded result against current selection
policy. An unchanged bare declaration or local digest does not establish that
selection is still valid. Explicitly pinned commits must match the requested
commit. Ordinary preparation may reuse recorded versions without upstream
polling. Historical loads and accepted Runs use captured values and refs without
consulting current defaults or resolvers.

Forced State refresh rechecks Cap acquisitions:

- GitHub selectors resolve to full commit SHAs before fetching all files at that
  SHA. Resolve each repository/selector once per layer build and bypass selector
  caches on refresh. Equal bytes at a changed commit still change provenance.
- HTTP(S) retains its canonical URL, including query, and refetches to compare
  bytes. Recognized GitHub URLs use GitHub version rules. HTTP skills fetch
  `SKILL.md`; GitHub skills retain their directory assets.

State compares resolved refs, complete file sets, and raw digests. Owners may
reuse intact bytes after proving an immutable revision unchanged. Visiting Mod
acquisition retains its existing one-hour cache lifecycle; State records the
supplied acquisition. Failed fetches produce no partial resolution or successful
fallback to old content. Existing resource-local exclusion and fatal-config
rules remain in effect. Unsupported schemes are rejected by Source parsing
before network access.

## Revision Identity and Integrity

`layer.json` and the composition's `layers.json` use canonical UTF-8 JSON:
sorted object keys, compact separators, direct Unicode, finite numbers, and no
trailing newline. Sort unordered item arrays; retain semantic array order.
Each revision is SHA-256 of its complete document bytes.

Both documents include `created_at`, such as `2026-10-15T08:30:00.000000Z`.
Compare a candidate with the current document excluding time before assigning a
new timestamp. Equal candidates reuse the current revision and time, including
no-op forced refresh. Returning to earlier content after a change creates a new
revision. Composition schema 3 retains its selected layer revisions, name,
overrides, and optional workspace additions alongside creation time. Loading
uses this stored document rather than recalculating an identity from layers alone.

Keep atomic writes, build locks, and publication locks. Recorded time describes
local creation, with no strict causality guarantee under clock changes. From an
agent home, list revisions by creation time with:

```sh
jq -r 'select(.created_at != null) | [.created_at, input_filename] | @tsv' \
  .state/home/revs/*/layer.json | sort
```

Normal loading checks stored value schemas and references without hashing all
files. Explicit offline integrity validation requires:

1. Canonical bytes and matching document/revision hashes, including time.
2. Exact source/materialized file sets with matching sizes/digests, portable
   paths, and no symlinks or escaping paths.
3. Valid item sources, scope/module ownership, acquisition keys, resource entries,
   asset membership, resolutions, and stored value schemas.
4. Recursive validation of root/home revisions for a composition.

Neither loading nor validation reads current configuration, resolves Source
Refs, accesses the network, or reinterprets captured syntax. The document hash
protects parsed values; preparation tests verify their interpretation. Rejected
bytes are absent, so only their observation records can be authenticated against
a revision. Hashes do not establish publisher authenticity.

Old layer/composition schemas are unsupported. Current derived caches rebuild
from authored inputs. Historical directories and execution records remain
untouched; historical data is not converted or migrated.

## Implementation and Acceptance

| Area | Responsibility |
| --- | --- |
| `common/source.py`, `common/github.py`, `catalog/source.py`, `up/source.py` | Source results, acquisition, version rules, and visiting provenance. |
| `state/source.py`, `acceptance.py`, `config.py` | Raw capture, accepted/excluded units, normalized policy. |
| `state/state.py`, `prepare.py`, `watcher.py` | Direct values, composition, Source-owned reuse checks. |
| `state/cache.py` | Canonical documents, maps, timestamps, persistence, restoration, validation. |
| State schemas/collections and CLI/API Cap consumers | Preserve public projections and raw detail requests. |

Acceptance checks cover:

- JSON restoration with source/materialized reads and Source services prohibited;
  raw config preservation, skill assets, rejected/unreadable inputs, defaults,
  and unchanged public receipts/projections.
- Same-name candidates, existing conflicts, duplicate declarations, shared
  acquisitions, mismatched file sets/bytes, and distinct origins/paths/queries.
- Pinned GitHub fetches, moved selectors, equal-content changed commits, no-op
  refresh, changed HTTP bytes, and visiting provenance/cache reuse.
- Synthetic new Sources and default-policy changes with identical local inputs;
  stable explicit refs; unknown historical schemes loaded/validated offline.
- Timestamps for all stores, A/B/A evolution, concurrent/failed publication,
  malformed values/references, document/file tampering, and exact file sets.
- Old-schema rejection, documented commands/links, and default offline checks.

Raw config archival preserves more bytes than a projected configuration;
parsed values increase JSON size while avoiding routine parsing. HTTP updates
require fetching and provide no Git-like version history. No design decisions
remain open.

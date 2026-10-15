# Agent Names and Source Refs

Design decisions for review, not a description of current support. Define these
contracts before public release; implement them separately. Scope is local Agent
Names and explicit GitHub/HTTP(S) Source Refs, with room for future OpenHat and
Handle rules. Keep `runnables/` unchanged. This PR records the plan only.

## Terms

| Term | Meaning |
| --- | --- |
| Display Name | Non-unique label such as `Brice Yan` or `Gus`; allows spaces, case, and Unicode. Changing it does not change identity or paths. |
| Agent Name | An agent's local name, including a default derived from a source filename. A future registered name may differ. |
| Handle | Future public participant identity, such as `by3` or `gus-by3`; terminology only here. |
| Mod (Module) | A `.too` module: the main `agent.too` or a runnable module. Its internal parsed representation remains `Program`. |
| Cap (Capability) | A skill, psyche, service, or prompt resource. |
| Source | A provider of Mod or Cap content, such as OpenHat, GitHub, or an HTTP service. |
| Source Ref | A content/source location without a Cap-kind discriminator, such as `gh:owner/some-name`, `github://…@rev`, a URL, or `root://skills/some-name`. |
| Cap Ref | A Cap's `kind/name` identity in the selected effective State/module context, such as `skill/some-name`; independent of its provenance. |
| Target | Command input interpreted in context, such as an Agent Name, file path, Source Ref, or Cap Ref. Replaces the user-facing term Selector. |

Runnable remains the execution unit (`agic` or `flow`); Mod is the module unit.

Use **Source Ref** and **Cap Ref** explicitly. `with skill SOURCE-REF` supplies
the kind separately; source paths may contain a directory called `skills`, but
that is not a kind field. Shorthand resolution receives the kind from its caller
for kind-specific path probes. Configuration and acquisition use Source Refs;
merged-Cap queries and policies use Cap Refs. The same `skill/some-name` may come
from `root://skills/some-name` or a remote source after layer composition. Do
not infer the category from the `x/y` spelling or change layer precedence.

Cap records use explicit field names in CLI JSON/queries and API summaries/details:

```json
{
  "cap": "skill/some-name",
  "source": "github://owner/repo/skills/some-name@main"
}
```

`cap` is the Cap Ref; `source` is the resolved, canonical Source Ref, not the
authored shorthand or a display path. Local sources retain `root://`, `home://`,
or `inline://`. Keep `location` for human-facing paths/URLs. Default Cap queries
match `cap`; bare `skill/some-name` queries retain their meaning. Replace the
ambiguous public Cap-record `ref` field and document explicit query-key migration
(`ref` to `cap`) and API field migration (`ref` to `source`, with `cap` added).
Internal provenance metadata remains distinct from this string projection; do
not globally rename every `ref` or `source` field.

## Agent names

- Local Agent Names use `[a-z][a-z0-9_]{0,63}`: 1–64 characters, no dots or
  hyphens. Explicit Agent Names are validated without trimming, case conversion,
  or truncation.
- For HTTP/GitHub/file-derived Agent Names, preserve an already valid filename
  stem; otherwise lowercase it, replace each run outside `[a-z0-9_]` with `_`,
  and trim surrounding underscores. Prefix a digit-leading result with `script_`.
  `Daily-Report.v2.too` becomes `daily_report_v2`; `2026 report.too` becomes
  `script_2026_report`. Reject an empty result or a final Agent Name over 64
  characters: do not truncate or add a hash. Keep the original filename and
  Source Ref.
- Cover resident creation, clone, visiting/roaming execution, and scripts
  (direct, `too run`, shebang). Pure source tools such as `fmt` and `parse` do
  not constrain filenames.
- If another source already owns the resulting home, reject the collision;
  `daily-report.too` and `daily_report.too` must not overwrite each other or
  silently share state. Hashes use source identity from
  [the runtime contract](unified-runtime-status.md), not the derived Agent Name.
- Validate before fetching/materializing/launching where the Agent Name is known.
  `clone` can supply an explicit Agent Name instead of using a derived default.
  Replace the visiting-root byte-truncation workaround with bounded Agent Names.
- Reserve `-` outside local Agent Names for future qualified Handles. The local
  64-character bound is not a Handle-length policy or a public-name conversion.

## Source refs

| Form | Source and interpretation |
| --- | --- |
| `gh:briceyan/gus`, `github:briceyan/gus` | GitHub shorthand using existing repository/path probes. `gh:owner/repo/name` selects a repository explicitly. |
| `gh:owner/repo/path@rev`, `github:owner/repo/path@rev`, `github://owner/repo/path@rev` | Equivalent exact GitHub Source Refs; normalize to `github://…@rev`. |
| `https://example.com/gus.too` | Agent Mod URL; complete HTTP(S) URLs remain supported. |
| `https://example.com/review.md` | Cap URL, including in `with`. |
| `oh:…`, `openhat:…` | Unsupported in this implementation; OpenHat parsing is deferred. |
| Unprefixed remote Source Refs such as `by3/some-skill` or `owner/repo/name` | Unsupported; reserved for a future OpenHat default. No implicit GitHub or HTTP resolution. |

Select the Source first; aliases resolve to the same canonical Source Ref.
Reject unsupported forms before network access, without parsing OpenHat payloads
or adding an OpenHat provider stub. Unknown source prefixes fail explicitly.
Local Agent Names and `.too` paths are not unprefixed remote Source Refs.
Cap Refs and existing local Source Refs (`home://`, `root://`, `inline://`) keep
their meanings; `skill/some-name` remains valid as a Cap Ref.
Use complete HTTP(S) URLs instead of domain/path shorthand.
For `gh:`/`github:`, `@rev` selects an exact repository/path with no probing;
without it, the documented shorthand forms retain their existing probes.

## Command behavior

| Command | Target and operation |
| --- | --- |
| `with <kind> <source-ref>` | Reference Cap content, including HTTPS URLs. Kind is separate from the Source Ref; this is not a Cap Ref query. |
| `too clone SOURCE [NAME]` | Copy a local agent or fetch published Mod content into a resident home. A local source requires an explicit target Agent Name. |
| `too serve TARGET` | Run a resident Agent Name, roaming file, or fetched visiting Mod in the selected environment. |

`clone` and `serve` share source parsing: bare local Agent Names remain local;
remote content uses an explicit supported Source Ref. A bare value such as
`gus-by3` fails local Agent Name validation without triggering Handle recognition
or public lookup. `talk` and existing messaging/Hub behavior are unchanged by
this plan.

## Delivery and verification

Currently, `with` syntax/formatting preserves these prefixes, but cap and agent
resolvers misinterpret them as GitHub owners. Bare remote Source Refs still use
GitHub, generic HTTP Cap fetching is not implemented, and Agent Names have no
length limit. Cap query records use `ref` for `kind/name`, while `CapInfo.ref`
contains a Source Ref. These are current gaps, not the proposed contracts.

The later implementation must update code, current documentation, and CLI help
together. Consolidate shared GitHub syntax/alias parsing around `GitHubRef` in
`common/github.py`; separate agent source parsing/materialization from process
control in `up/process.py`. Keep agent-specific and Cap-kind repository/path
rules with their owners, using one policy per concern instead of parallel parsers
or a provider framework. Keep Source Ref resolution separate from effective-Cap
querying and layer merging. Share Agent Name validation/derivation between
`common/layout.py`, `catalog/agent.py`, and their callers.
Align Cap projections in `state/collections.py` and `state/schemas.py` with their
CLI/API consumers; existing `StateCap.effective_ref` and `CapView.source_ref`
provide the two distinct values.

Update CLI routing, argument descriptions, usage examples, and errors alongside
the refactoring. Reconcile README, source/Cap/Mod/layout docs, and bundled
examples/templates; replace implicit GitHub refs and stale Selector terminology.
Generate the implementation changelog through `too aid.too update_changelog`.
Existing GitHub `owner/name` or `owner/repo/name` source declarations need a `gh:`
or `github:` prefix; existing exact GitHub Source Refs and URLs retain their
meaning. Provide migration guidance for Cap record/query fields, invalid old
Agent Names, and changed paths; do not automatically move state or histories.

Acceptance: test 64-character boundaries, character normalization, empty/overlong
results, and stem collisions without partial writes; cover script entry points
and explicit clone targets. Verify AST/format round trips, equivalent shorthand
and exact source aliases, URL support across `with`/`clone`/`serve`, and rejection
of OpenHat prefixes/unprefixed remote Source Refs with zero network calls. Verify
local Targets and local Source Refs still work. Verify CLI/API `cap` and `source`
agree, shorthand resolves before populating `source`, and queries use the new
keys. Bare Cap Ref queries, policy selection, and layer precedence retain their
behavior; an override may change `source` while retaining `cap`. CLI help/docs
must distinguish the two Ref categories and show supported forms and migration.
Keep valid peers visible beside invalid legacy entries; Handle-like input
triggers no Handle lookup. Implementation changes run the default checks.

## Outside this scope

Handle recognition, syntax/length validation, registration, local bindings,
ownership changes, Talk/public-Hub routing, and registration/content service
protocols require separate designs. OpenHat source/prefix parsing and an
unprefixed remote Source Ref default are also deferred. This plan reserves
compatible syntax only, without implementing those features.

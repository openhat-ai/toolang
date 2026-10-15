# Agent Names and Source Refs

Design decisions for review, not a description of current support. Define these
contracts before public release; implement them separately. Scope is local Agent
Names and explicit GitHub/HTTP(S) content refs, with room for future OpenHat and
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
| Ref (Reference) | A content reference, including shorthand and complete URLs. |
| Target | Command input selecting an Agent Name, file path, or Ref. Replaces the user-facing term Selector. |

Runnable remains the execution unit (`agic` or `flow`); Mod is the module unit.

## Agent names

- Local Agent Names use `[a-z][a-z0-9_]{0,63}`: 1–64 characters, no dots or
  hyphens. Explicit Agent Names are validated without trimming, case conversion,
  or truncation.
- For HTTP/GitHub/file-derived Agent Names, preserve an already valid filename
  stem; otherwise lowercase it, replace each run outside `[a-z0-9_]` with `_`,
  and trim surrounding underscores. Prefix a digit-leading result with `script_`.
  `Daily-Report.v2.too` becomes `daily_report_v2`; `2026 report.too` becomes
  `script_2026_report`. Reject an empty result or a final Agent Name over 64
  characters: do not truncate or add a hash. Keep the original filename and Ref.
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
| `gh:owner/repo/path@rev`, `github:owner/repo/path@rev`, `github://owner/repo/path@rev` | Equivalent exact GitHub Refs; normalize to `github://…@rev`. |
| `https://example.com/gus.too` | Agent Mod URL; complete HTTP(S) URLs remain supported. |
| `https://example.com/review.md` | Cap URL, including in `with`. |
| `oh:…`, `openhat:…` | Unsupported in this implementation; OpenHat parsing is deferred. |
| Unprefixed remote refs such as `by3/some-skill` or `owner/repo/name` | Unsupported; reserved for a future OpenHat default. No implicit GitHub or HTTP resolution. |

Select the Source first; aliases resolve to the same canonical content Ref.
Reject unsupported forms before network access, without parsing OpenHat payloads
or adding an OpenHat provider stub. Unknown source prefixes fail explicitly.
Local Agent Names and `.too` paths are Targets, not unprefixed remote Refs;
existing local Cap Refs (`home://`, `root://`, `inline://`) keep their meanings.
Use complete HTTP(S) URLs instead of domain/path shorthand.
For `gh:`/`github:`, `@rev` selects an exact repository/path with no probing;
without it, the documented shorthand forms retain their existing probes.

## Command behavior

| Command | Target and operation |
| --- | --- |
| `with <kind> <ref>` | Reference Cap content, including HTTPS URLs. |
| `too clone SOURCE [NAME]` | Copy a local agent or fetch published Mod content into a resident home. A local source requires an explicit target Agent Name. |
| `too serve TARGET` | Run a resident Agent Name, roaming file, or fetched visiting Mod in the selected environment. |

`clone` and `serve` share source parsing: bare local Agent Names remain local;
remote content uses an explicit supported Ref. A bare value such as `gus-by3`
fails local Agent Name validation without triggering Handle recognition or public
lookup. `talk` and existing messaging/Hub behavior are unchanged by this plan.

## Delivery and verification

Currently, `with` syntax/formatting preserves these prefixes, but cap and agent
resolvers misinterpret them as GitHub owners. Bare remote Refs still use GitHub,
and Agent Names have no length limit. These are implementation gaps, not support
provided by this document.

The later implementation must update code, current documentation, and CLI help
together. Consolidate shared GitHub syntax/alias parsing around `GitHubRef` in
`common/github.py`; separate agent source parsing/materialization from process
control in `up/process.py`. Keep agent-specific and Cap-kind repository/path
rules with their owners, using one policy per concern instead of parallel parsers
or a provider framework. Share Agent Name validation/derivation between
`common/layout.py`, `catalog/agent.py`, and their callers.

Update CLI routing, argument descriptions, usage examples, and errors alongside
the refactoring. Reconcile README, source/Cap/Mod/layout docs, and bundled
examples/templates; replace implicit GitHub refs and stale Selector terminology.
Generate the implementation changelog through `too aid.too update_changelog`.
Existing GitHub `owner/name` or `owner/repo/name` declarations need a `gh:` or
`github:` prefix; existing exact GitHub Refs and URLs retain their meaning.
Provide migration guidance for invalid old Agent Names and changed paths; do not
automatically move state or histories.

Acceptance: test 64-character boundaries, character normalization, empty/overlong
results, and stem collisions without partial writes; cover script entry points
and explicit clone targets. Verify AST/format round trips, equivalent shorthand
and exact source aliases, URL support across `with`/`clone`/`serve`, and rejection
of OpenHat prefixes/unprefixed remote refs with zero network calls. Verify local
Targets still work, Cap local Refs retain their meanings, and CLI help/docs show
only supported forms. Keep valid peers visible beside invalid legacy entries;
Handle-like input triggers no Handle lookup. Implementation changes run the
default checks.

## Outside this scope

Handle recognition, syntax/length validation, registration, local bindings,
ownership changes, Talk/public-Hub routing, and registration/content service
protocols require separate designs. OpenHat source/prefix parsing and an
unprefixed remote-ref default are also deferred. This plan reserves compatible
syntax only, without implementing those features.

# Names, Handles, and Source Refs

Design decisions for review, not a description of current support. Define these
contracts before public release; implement them separately. Keep `runnables/`
unchanged. Public registration and Hub protocols need a follow-up design.

## Terms

| Term | Meaning |
| --- | --- |
| Display Name | Non-unique label such as `Brice Yan` or `Gus`; allows spaces, case, and Unicode. Changing it does not change identity or paths. |
| Name | An agent's short name in its local or registration context; these Names may differ. |
| Handle | Public participant identity: `by3` for a human, `<Name>-<human-handle>` for an agent, such as `gus-by3`. |
| Mod (Module) | A `.too` module: the main `agent.too` or a runnable module. Its internal parsed representation remains `Program`. |
| Cap (Capability) | A skill, psyche, service, or prompt resource. |
| Source | A provider of Mod or Cap content, such as OpenHat, GitHub, or an HTTP service. |
| Ref (Reference) | A content reference, including shorthand and complete URLs. |
| Target | Command input selecting a Name, Handle, file path, or Ref. Replaces the user-facing term Selector. |

Runnable remains the execution unit (`agic` or `flow`); Mod is the module unit.

## Names and handles

- Local Names use `[a-z][a-z0-9_]{0,15}`: 1–16 characters, no dots or hyphens.
  Explicit Names are validated without trimming, case conversion, or truncation.
- HTTP/GitHub/file-derived Names use the final filename stem, replacing `-`
  with `_`, then applying local validation. For example, `daily-report.too`
  becomes `daily_report`; `report.v2.too` is rejected. Cover resident creation,
  clone, visiting/roaming execution, and scripts (direct, `too run`, shebang).
  Pure source tools such as `fmt` and `parse` do not constrain filenames.
- If another source already owns the resulting home, reject the collision;
  `daily-report.too` and `daily_report.too` must not overwrite each other or
  silently share state. Hashes use source identity from
  [the runtime contract](unified-runtime-status.md), not the normalized Name.
- Validate before fetching/materializing/launching where the Name is known.
  `clone` can supply a legal target Name for an otherwise invalid source stem.
  Replace the visiting-root byte-truncation workaround with bounded Names.
- Human handles contain lowercase letters and digits, without `_` or `-`,
  and are globally unique. Agent registration explicitly chooses a Name
  matching `[a-z0-9]+`, unique under that human handle. The full agent Handle
  has exactly one `-` and is globally unique; mixed lists can distinguish it
  from a human handle.
- Persist an explicit registration binding: local Name `daily_report` may
  register as Name `dailyreport` or `abc`, yielding `dailyreport-by3` or
  `abc-by3`. Do not derive a public Handle by replacing/removing underscores.
  Local execution does not require public registration.

## Source refs

| Form | Source and interpretation |
| --- | --- |
| `by3/some-skill`, `gus-by3/some-skill` | OpenHat cap published under a human or agent Handle. Cap names retain their own rules. |
| `gus-by3` | OpenHat agent Mod Ref in source-acquisition commands; participant Handle in Talk. |
| `oh:…`, `openhat:…` | Explicit OpenHat; same source as an unprefixed remote Ref. |
| `gh:briceyan/gus`, `github:briceyan/gus` | GitHub shorthand using existing repository/path probes. `gh:owner/repo/name` selects a repository explicitly. |
| `github://owner/repo/path@rev` | Existing exact GitHub Ref. |
| `https://example.com/gus.too` | Agent Mod URL; complete HTTP(S) URLs remain supported. |
| `https://example.com/review.md` | Cap URL, including in `with`. |

Select the Source first; aliases resolve to the same canonical content Ref.
Default remote Refs belong to OpenHat, with no GitHub fallback. Unavailable
services and unknown source prefixes report explicit errors. Reserve OpenHat
syntax even before its service launches. Existing local Refs retain their meaning.

## Command behavior

| Command | Target and operation |
| --- | --- |
| `with <kind> <ref>` | Reference Cap content, including HTTPS URLs. |
| `too clone SOURCE [NAME]` | Copy a local agent or fetch published Mod content into a resident home. A local source requires an explicit target Name. |
| `too serve TARGET` | Run a resident Name, roaming file, or fetched visiting Mod in the selected environment. |
| `too talk gus-by3` | Send as the signed-in human Handle through a Toolang-operated public Hub. |
| `too talk gus` / `too talk daily_report` | Resolve the Name's registered Handle (`gus-by3` / `abc-by3`), then converse through that Hub. |

`clone` and `serve` share source parsing: bare local Names remain local; Handles
select published agent content. Public conversation and content access are
separate capabilities. Participating local agents send as their own registered
Handles and can converse with humans and remote agents. Missing local Names or
public-target failures never trigger fallback to the other scope.

## Delivery and verification

Currently, `with` syntax/formatting preserves these prefixes, but cap and agent
resolvers misinterpret them as GitHub owners. Bare remote Refs still use GitHub,
`gus-by3` is local, Names have no length limit, and Hub is local. These are
implementation gaps, not support provided by this document.

Before public release, implement source dispatch in `state/state.py` and
`up/process.py`, shared Name validation in `common/layout.py` and
`catalog/agent.py`, and CLI error/routing updates. Update current docs/examples
and generate the implementation changelog through `too aid.too update_changelog`.
Existing GitHub `owner/name` or `owner/repo/name` declarations need a `gh:` or
`github:` prefix; existing exact GitHub Refs and URLs retain their meaning.
Provide migration guidance for invalid old Names and changed paths; do not
automatically move state or histories.

Acceptance: test Name boundaries and stem collisions without partial writes;
cover script entry points and explicit clone targets; verify AST/format round
trips, equivalent source aliases, URL support across `with`/`clone`/`serve`, and
no cross-source fallback. Keep valid peers visible beside invalid legacy entries.
Public Hub follow-up tests must distinguish human/agent senders, local binding
lookup, and full-Handle routing. Implementation changes run the default checks.

## Deferred decisions

- Public Name, human-handle, and full-Handle length limits; materializing a
  numeric-leading registered Name into a letter-leading local Name.
- Public registration/binding storage, ownership changes, Hub protocol, and
  versioned/canonical OpenHat content Refs. These block public-service
  implementation, not reserving source syntax and explicit GitHub prefixes.
- Talk syntax for a public human Handle that could also be a local agent Name.

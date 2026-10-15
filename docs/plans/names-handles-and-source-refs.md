# Agent Names, Handles, and Source Refs

Design decisions for review, not a description of current support. Define these
contracts before public release; implement them separately. Keep `runnables/`
unchanged. Public registration and Hub protocols need a follow-up design.

## Terms

| Term | Meaning |
| --- | --- |
| Display Name | Non-unique label such as `Brice Yan` or `Gus`; allows spaces, case, and Unicode. Changing it does not change identity or paths. |
| Agent Name | An agent's name in its local or registration context; these may differ. |
| Handle | Public participant identity: `by3` for a human, `<agent-name>-<human-handle>` for an agent, such as `gus-by3`. |
| Mod (Module) | A `.too` module: the main `agent.too` or a runnable module. Its internal parsed representation remains `Program`. |
| Cap (Capability) | A skill, psyche, service, or prompt resource. |
| Source | A provider of Mod or Cap content, such as OpenHat, GitHub, or an HTTP service. |
| Ref (Reference) | A content reference, including shorthand and complete URLs. |
| Target | Command input selecting an Agent Name, Handle, file path, or Ref. Replaces the user-facing term Selector. |

Runnable remains the execution unit (`agic` or `flow`); Mod is the module unit.

## Agent names and handles

- Local Agent Names use `[a-z][a-z0-9_]{0,63}`: 1–64 characters, no dots or
  hyphens. Explicit Agent Names are validated without trimming, case conversion,
  or truncation. This bound is independent of Handle-length policy.
- For HTTP/GitHub/file-derived Agent Names, preserve an already valid filename
  stem; otherwise lowercase it, replace each run outside `[a-z0-9_]` with `_`,
  and trim surrounding underscores. Prefix a digit-leading result with `script_`.
  `Daily-Report.v2.too` becomes `daily_report_v2`; `2026 report.too` becomes
  `script_2026_report`. Reject an empty result or a final Agent Name over 64
  characters: do not truncate or add a hash. Keep the original filename and Ref.
  OpenHat agent content uses its registered Agent Name as the seed for this same
  local derivation rule.
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
- Human handles contain lowercase letters and digits, without `_` or `-`,
  and are globally unique. Agent registration explicitly chooses an Agent Name
  matching `[a-z0-9]+`, unique under that human handle. The full agent Handle
  has exactly one `-` and is globally unique; mixed lists can distinguish it
  from a human handle.
- The public registration service owns shorter Handle/component length limits.
  Do not introduce a Handle-length cap in this client or reuse the local Agent
  Name's 64-character bound for public identity validation.
- Persist an explicit registration binding: local Agent Name `daily_report` may
  register as Agent Name `dailyreport` or `abc`, yielding `dailyreport-by3` or
  `abc-by3`. Do not derive a public Handle by replacing/removing underscores.
  Local execution does not require public registration.

## Source refs

| Form | Source and interpretation |
| --- | --- |
| `by3/some-skill`, `gus-by3/some-skill` | OpenHat cap published under a human or agent Handle. Cap names retain their own rules. |
| `gus-by3` | OpenHat agent Mod Ref in source-acquisition commands; participant Handle in Talk. |
| `oh:…`, `openhat:…` | Explicit OpenHat; same source as an unprefixed remote Ref. |
| `gh:briceyan/gus`, `github:briceyan/gus` | GitHub shorthand using existing repository/path probes. `gh:owner/repo/name` selects a repository explicitly. |
| `gh:owner/repo/path@rev`, `github:owner/repo/path@rev`, `github://owner/repo/path@rev` | Equivalent exact GitHub Refs; normalize to `github://…@rev`. |
| `https://example.com/gus.too` | Agent Mod URL; complete HTTP(S) URLs remain supported. |
| `https://example.com/review.md` | Cap URL, including in `with`. |

Select the Source first; aliases resolve to the same canonical content Ref.
Default remote Refs belong to OpenHat, with no GitHub fallback. Unavailable
services and unknown source prefixes report explicit errors. Reserve OpenHat
syntax even before its service launches. Existing local Refs retain their meaning.
For `gh:`/`github:`, `@rev` selects an exact repository/path with no probing;
without it, the documented shorthand forms retain their existing probes.

## Command behavior

| Command | Target and operation |
| --- | --- |
| `with <kind> <ref>` | Reference Cap content, including HTTPS URLs. |
| `too clone SOURCE [NAME]` | Copy a local agent or fetch published Mod content into a resident home. A local source requires an explicit target Agent Name. |
| `too serve TARGET` | Run a resident Agent Name, roaming file, or fetched visiting Mod in the selected environment. |
| `too talk gus-by3` | Send as the signed-in human Handle through a Toolang-operated public Hub. |
| `too talk gus` / `too talk daily_report` | Resolve the Agent Name's registered Handle (`gus-by3` / `abc-by3`), then converse through that Hub. |

`clone` and `serve` share source parsing: bare local Agent Names remain local;
Handles select published agent content. Public conversation and content access
are separate capabilities. Participating local agents send as their own
registered Handles and can converse with humans and remote agents. Missing local
Agent Names or public-target failures never trigger fallback to the other scope.

## Delivery and verification

Currently, `with` syntax/formatting preserves these prefixes, but cap and agent
resolvers misinterpret them as GitHub owners. Bare remote Refs still use GitHub,
`gus-by3` is local, Agent Names have no length limit, and Hub is local. These are
implementation gaps, not support provided by this document.

Before public release, implement source dispatch in `state/state.py` and
`up/process.py`, shared Agent Name validation/derivation in `common/layout.py` and
`catalog/agent.py`, and CLI error/routing updates. Update current docs/examples
and generate the implementation changelog through `too aid.too update_changelog`.
Existing GitHub `owner/name` or `owner/repo/name` declarations need a `gh:` or
`github:` prefix; existing exact GitHub Refs and URLs retain their meaning.
Provide migration guidance for invalid old Agent Names and changed paths; do not
automatically move state or histories.

Acceptance: test 64-character boundaries, character normalization, empty/overlong
results, and stem collisions without partial writes; cover script entry points
and explicit clone targets. Verify AST/format round trips, equivalent shorthand
and exact source aliases, URL support across `with`/`clone`/`serve`, and
no cross-source fallback. Keep valid peers visible beside invalid legacy entries.
Public Hub follow-up tests must distinguish human/agent senders, local binding
lookup, and full-Handle routing. Implementation changes run the default checks.

## Deferred decisions

- Public registration/binding storage, ownership changes, Hub protocol, and
  versioned/canonical OpenHat content Refs. These block public-service
  implementation, not reserving source syntax and explicit GitHub prefixes.
- Talk syntax for a public human Handle that could also be a local Agent Name.

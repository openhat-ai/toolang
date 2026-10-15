# Agent Names and Source Refs

Approved contract for local Agent Names, explicit Mod/Cap sources, and effective
Cap identity. Runtime roots, source hashes, lifecycle ownership, and discovery
follow [Unified runtime status](unified-runtime-status.md).

## Terms

| Term | Meaning |
| --- | --- |
| Display Name | Non-unique human-facing label such as `Brice Yan` or `Gus`; not an identity or filesystem path. |
| Agent Name | Local agent identifier, including a default derived from a source filename. A future public registration may choose a different name. |
| Handle | Future public participant identity, such as human `by3` or agent `gus-by3`; terminology only in this scope. |
| Mod (Module) | A `.too` module: `agent.too` or a runnable module. Its parsed representation remains `Program`. |
| Cap (Capability) | A skill, psyche, service, or prompt. |
| Source | A provider of Mod or Cap content. |
| Source Ref | Content location without a Cap-kind discriminator, such as `gh:owner/name`, `github://…@rev`, an HTTP(S) URL, or `root://skills/name`. |
| Cap Ref | Effective `kind/name` identity in the selected State/module, such as `skill/reviewer`, independent of provenance. |
| Target | Command input interpreted in context as an Agent Name, file path, Source Ref, or Cap Ref. |

A runnable is an execution entry (`agic` or `flow`) in a Mod. Keep `runnables/`
and internal `Program` types. Use Target for command inputs; TQ selectors and
sandbox selectors retain their separate meanings.

## Agent Names

- Explicit Agent Names match `[a-z][a-z0-9_]{0,63}`: 1–64 lowercase ASCII
  characters, starting with a letter. Validate without trimming or conversion.
- For a filename-derived default, preserve a valid stem. Otherwise lowercase it,
  replace each run outside `[a-z0-9_]` with `_`, trim surrounding underscores,
  and prefix a digit-leading result with `script_`. `Daily-Report.v2.too` becomes
  `daily_report_v2`; `2026 report.too` becomes `script_2026_report`.
- Reject an empty or overlong final result. Keep the original filename and Source
  Ref; do not truncate names or add hash suffixes. `clone SOURCE NAME` can supply
  a valid explicit name instead of an invalid derived default.
- Apply the same rules to creation, cloning, visiting/roaming layouts, and script
  entry points (direct, `too run`, and shebang). `fmt` and `parse` do not restrict
  source filenames. Validate before fetching or writing when the name is known.
- A roaming home belongs to one canonical source path. Concurrent callers for
  that source may reuse it; a different source normalizing to the same home
  must fail without replacing the link or reading that source's state. Source
  hashes remain independent of the normalized name.
- Ignore invalid legacy names during discovery and automatic port reservations,
  keeping valid peers usable. Migration is explicit; no automatic state moves.

The local 64-character bound is not a public Handle policy. Local `_` and the
absence of `-` leave room for future qualified Handles without implementing them.

## Source Refs

| Form | Interpretation |
| --- | --- |
| `gh:owner/name`, `github:owner/name` | GitHub shorthand with owner-specific repository/path probes. |
| `gh:owner/repo/name`, `github:owner/repo/name` | GitHub shorthand in one explicit repository. |
| `gh:owner/repo/path@rev`, `github:owner/repo/path@rev`, `github://owner/repo/path@rev` | Equivalent exact refs, canonicalized to `github://…@rev`; no shorthand probes. |
| Complete `http://` or `https://` URL | Mod file or Cap content. Recognized GitHub blob/tree/raw URLs resolve to GitHub refs. |
| `home://`, `root://`, `inline://` | Existing local Source Refs retain their meanings. |
| Unprefixed remote refs, `oh:`, `openhat:`, or unknown prefixes | Unsupported; reject before network access. |

Shared parsing selects the Source without I/O. Agent and Cap owners retain their
own repository/path probes; exact refs do not trigger shorthand discovery.
[Agent Sources](../concepts.md#agent-sources) and
[Cap Source Refs](../caps.md#source-refs) define those probes.

`with KIND SOURCE-REF` supplies the kind separately. A directory called `skills`
in a source path is an ordinary provider path, not a kind field. HTTP file-backed
Caps fetch their URL directly; HTTP skills fetch only `SKILL.md` from a directory
or entry URL. GitHub skills retain the directory's files. URL path decoding occurs
once before GitHub fetch URLs are encoded.

Current State caches obey these source rules even when authored bytes have not
changed. Explicit historical revision loads retain their captured declarations.

## Cap Projection

CLI JSON/queries and API summaries/details distinguish identity and provenance:

```json
{
  "cap": "skill/reviewer",
  "source": "github://owner/repo/skills/reviewer@main"
}
```

`cap` is the Cap Ref; `source` is the resolved Source Ref, including local
`root://`, `home://`, or `inline://` forms. `location` is the separate human-facing
content address. Cap TQ uses `cap` as its identity key; bare `skill/reviewer`
queries retain their meaning. Layer precedence and allow policies are unchanged.
Configured write requests retain `ref` for the supplied Source Ref; internal
provenance and unrelated `ref` fields are not globally renamed.

## Command and Implementation Boundaries

| Entry point | Contract |
| --- | --- |
| `too new NAME` | Create a resident agent with an explicit Agent Name. |
| `too clone SOURCE [NAME]` | Copy a resident agent or fetch a remote Mod. Local cloning requires a target name. |
| `too serve TARGET` | Host a resident name, roaming file, or visiting remote Mod. |
| `with KIND SOURCE-REF` | Acquire Cap content for the authored Mod. |
| Cap CLI/API queries | Select effective Cap Refs and filter public source/provenance fields. |

Source parsing belongs in `common/github.py` and `common/source.py`; shared Name
rules belong in `common/layout.py`. `up/source.py` owns agent source resolution
and materialization; `up/process.py` owns workload control/query. Cap acquisition
stays with State, and `state/collections.py` / `state/schemas.py` own public Cap
projections consumed by CLI/API. Targets retain the parsed Source instead of
duplicating provider fields; shared source parsing owns URL path decoding, and
Mod validation consumes that path unchanged. Update help, examples, docs, and
tests with the contract; do not add a provider framework or duplicate parsers.

Handle parsing, registration, length rules, bindings, public Talk/Hub routing,
OpenHat source parsing/defaults, shared script servers, and directory renames are
outside this implementation. Existing messaging and script execution isolation
are unchanged; repeated execution of the same source/config reuses prepared
Agent State without making separate invocation histories the same execution.

## Migration and Acceptance

Prefix former implicit GitHub sources with `gh:` or `github:`; replace domain/path
shorthand with complete HTTP(S) URLs. External TQ Cap consumers switch their
identity key from `ref` to `cap`; API readers switch response `ref` to `source`
and receive `cap` separately. Stop invalidly named legacy workloads with the
previous version before renaming homes/paths manually. Keep state and histories.

Acceptance checks:

- Explicit/derived name boundaries, normalization, rejection before writes,
  clone overrides, all script entry points, and unrestricted source-only tools.
- Same-source concurrent materialization, conflicting sources, stable source
  hashes, and valid peers beside invalid legacy homes.
- `with` AST/format round trips; exact aliases, shorthand probes, HTTP acquisition,
  local refs, and unsupported sources rejected with zero network calls.
- Matching CLI/API `cap` and `source`, override precedence, unchanged Cap Ref
  queries, configured write `ref`, and current-cache/historical-load behavior.
- Current documentation/help/examples agree with the contract, and the default
  Ruff, type, and offline pytest checks pass.

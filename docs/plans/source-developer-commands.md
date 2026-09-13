# Integrated Source Developer Commands

## Goal and Status

Approved for implementation by the user, including preservation of `fmt --check`.
Development uses an isolated branch on PR #532 until its changes reach main.

Expose `parse`, `fmt`, and `highlight` through both `too` and `toolang`, using
installed Tree-sitter Python packages and Rich. Installation alone must suffice:
no external Tree-sitter CLI, grammar checkout, network, model configuration, or
agent setup. Success means consistent source handling, both AST/CST displays,
shared highlighting, and formatting aligned with the conventions below.

## Baseline and Prerequisite

Verified against Toolang `ba81f137`, `tree-sitter==0.25.2`, and
`tree-sitter-toolang==0.3.1`:

- Hidden `fmt` supports files/directories, stdin, `--check`, and `--tab-size`.
  Hidden `parse` emits validated semantic AST JSON and supports `--compact`.
- Both executables share the CLI entry point and lazy command factories.
- `lang/ast.py` owns parsing; its runtime path masks query-data hashes and may
  append LF. Formatting uses CST helpers without requiring semantic validity.
- The grammar exports `HIGHLIGHTS_QUERY`; local query probes succeeded.
  Rich `Text` changes tabs/CRLF; `Segment`/`Segments` preserved source bytes.
- Current formatting adds `: Part[]` to untyped `_` and inserts blank lines
  between every `with` and between adjacent inline messages.

Before final integration, rebase onto merged [PR #532](https://github.com/openhat-ai/toolang/pull/532).
It was open when implementation started; inherit its released 0.3.2 grammar, comment node
names, parameter documentation, and formatter attachment fixes. Do not duplicate
its grammar/cache migration. Verify its final merged behavior before coding.

Convention inputs: the repository's
[authoring conventions](../toolang-authoring-conventions.md), the
[Toolang coding conventions skill](https://github.com/briceyan/agents/blob/main/skills/toolang-coding-conventions/SKILL.md),
and [PR #528](https://github.com/openhat-ai/toolang/pull/528), commit `70a2c29c`.
PR #528 was closed without merging; its selected mechanical rules are explicit
new scope below. PR #532's `#@` and `## @param` rules supersede older examples.

## CLI Contract

Tree selection and representation are independent. Default to **AST and
S-expression**; `--json` changes representation for either tree.

| Command | Behavior |
| --- | --- |
| `too parse FILE [--ast]` | Indented semantic AST S-expression |
| `too parse FILE --cst` | Indented named-node CST S-expression |
| `too parse FILE --json` | Existing semantic AST JSON |
| `too parse FILE --cst --json` | Complete CST JSON |
| `too fmt PATH...` | Existing in-place formatting of files/directories |
| `too fmt PATH... --check` | Existing formatting check; no writes |
| `too fmt FILE --stdout` | Plain formatted source; no writes |
| `too fmt FILE --highlight` | Highlighted formatted source; no writes |
| `too highlight FILE` | Original source with automatic terminal coloring |
| `too highlight FILE --html` | Standalone highlighted HTML on stdout |

Shared input rules:

- Accept `-` as the sole source; `--stdin-filepath PATH` labels diagnostics and
  never reads/writes that path. Preserve `fmt --stdin-filepath PATH` without `-`
  as a compatibility shorthand; other commands require explicit `-`.
- No arguments shows help; never implicitly scan the working directory.
  Support paths with spaces and `--` for names beginning with a dash.
- In-place/check formatting retains recursive `.too` discovery, resolved-path
  deduplication, ordering, and multiple inputs. Other modes require one file
  or stdin. Explicit files must have the `.too` suffix; URLs/agent selectors
  are not source inputs.
- Expose a `Source Commands` panel after `Script Commands`, ordered `parse`,
  `fmt`, `highlight`, using existing factories and target-free routing.
  Preserve script shorthand, shebangs, and agent routes; `agent:highlight`
  escapes collision with the newly reserved command.

`--ast` and `--cst` are mutually exclusive. Keep `--compact` as shorthand for
compact JSON, implying `--json`; `--json --compact` also works. Do not add
`--format`, `--tree`, `--sexp`, or top-level `ast`/`cst` aliases. The default
parse display intentionally changes from JSON to S-expression: existing JSON
consumers must add `--json`; compact consumers and AST JSON schemas stay intact.
Document this migration in help, command docs, and the changelog.

### Formatter Output

`--highlight` implies stdout mode; explicit `--stdout --highlight` also works.
Both stdout modes reject directories, multiple inputs, and `--check` before any
mutation. Preserve `fmt -` as plain stdin-to-stdout formatting and reject
`--check` with stdin. No output mode both writes a source file and prints code.
Preserve in-place/check status messages and `--tab-size` (positive, default 2).

Share `--color auto|always|never` and `--html` with `highlight`; on `fmt` they
require `--highlight`. Reject explicitly supplied rendering options otherwise,
without treating defaults as supplied options. For example:

```sh
too fmt example.too --highlight --color always
too fmt - --highlight --html > formatted.html
too highlight - --color always
```

Format completely before rendering; errors emit no code/HTML. Capture the
**formatted result**, since original offsets become stale. Under equivalent
rendering settings, direct output equals `too fmt FILE --stdout | too highlight -`;
the direct command also retains formatter failure status. Plain output/file
writes contain no generated ANSI; highlighted pipes remain plain in auto mode.

## Formatter Conventions

Implement the following in `format_source()` so every formatter mode shares
one result. Remain CST-based and usable on syntax-valid, semantic-invalid input.

| Area | Mechanical rule |
| --- | --- |
| Indentation | Retain two-space default, `--tab-size`, structural spacing, and existing final-LF normalization. |
| Signatures | Preserve omitted types; stop expanding `_` to `_: Part[]`. Retain explicit types including defaults, return annotations, `()`, optional `?`, names, and order. |
| Imports | Adjacent `with` clauses of the same cap kind have no blank separator; different kinds have one. Preserve full source order; no sorting or deduplication. |
| Resource directives | Apply adjacent-run spacing by directive key (`models`, `tools`, etc.). Preserve operators, values, query ordering, and statement order; never regroup interleaved keys by moving statements. |
| Inline messages | Remove gratuitous blank separators between adjacent CST-identified inline messages. Preserve roles/order; do not collapse block text or remove explicit `user:`. |
| Prose/flow boundaries | Use one structural blank line between distinct prose and explicit flow statements where attachment is unchanged. Preserve internal blank lines and relative indentation of text bodies, implicit requests, and literal blocks. |
| Documentation | Use 0.3.2 nodes for `#`, `#!`, `##`, `#@`, legacy `##!`, and `## @param`. Normalize spacing while preserving wording, tag/name order, exact names, attachment, and deliberate detachment. |

Comments, attachment barriers, and text ownership take precedence over grouping.
Do not move executable statements or declarations across comments. Retain
block-declaration spacing, flow clause order, first-byte shebangs, and PR #532's
module-documentation boundary fixes. Preserve legacy marker spelling; use `#@`
in new examples. Marker-like literal body text, including its first line,
remains text. Preserve module, runnable, and parameter documentation bindings.

Authoring-only guidance stays outside automatic formatting: choosing names,
adding `{{_}}`, rewriting/capitalizing prose, deleting comments/explicit types,
removing default capability directives, hoisting `context`/`instruct`, omitting
a sole `user:` role, or changing stages/concurrency/iteration. Encode selected
rules in code/tests/docs; do not read external skills or docs at runtime.

Acceptance invariant: formatting is idempotent and preserves semantic AST
content/documentation, ignoring only source locations and location-derived
internal names. Test normalization must not hide real metadata changes.

## Tree and Error Contracts

**AST** is Toolang's lowered, validated `Program`, using `Program.from_source()`
and `to_data()` as authorities. Preserve preprocessing, validation, JSON fields,
and one-based `span.line`. Validation stays source-level: no installed-cap
resolution, State preparation, network, or execution. Invalid input emits no
partial AST. Formatting checks remain formatting-only.

AST S-expressions are a Toolang presentation, not native Tree-sitter output:
`(kind field: value ...)` retains all dataclass fields in declaration order;
spans use `(span line: N)`, sequences `(list ...)`, and mappings
`(map ("key" value) ...)` in existing iteration order. Use JSON string/scalar
escaping with literal Unicode; retain empty containers. Walk typed AST objects
rather than inferring nodes from arbitrary metadata keys. Importing these
S-expressions is out of scope.

**CST** parses original UTF-8 bytes without formatting, added LF, hash masking,
lowering, or semantic validation. It includes incomplete/error nodes. The raw
view can differ from AST preprocessing; document that distinction.

CST JSON has the following versioned envelope:

```text
schema_version: 1
grammar: {name: "toolang", version: installed grammar package version}
source: original decoded UTF-8 source
root: node
diagnostics: [{kind, node_type, message, start_byte, end_byte, start_point, end_point}]
```

Every node contains `type`, `field` (parent field name or null), `is_named`,
`is_extra`, `is_error`, `is_missing`, `has_error`, byte/point ranges, and ordered
`children` including anonymous nodes. Ranges are half-open; points are
`{row, column}`, zero-based UTF-8 byte columns. Omit process-local IDs. Diagnose
`ERROR`, missing, and grammar `invalid_*` nodes in source order; `has_error`
alone is insufficient. Original `source` preserves whitespace omitted by nodes.

CST S-expressions follow `str(root_node)`'s named-node shape, fields, and
error/missing markers; indent by tree traversal, not splitting serialized text.
They omit anonymous nodes/source text; complete inspection uses JSON. Both
S-expression views use deterministic two-space indentation, each nested
child/field on its own line, independent of terminal width. CST vocabulary
tracks grammar versions; JSON is the supported scripting interface, not a
runtime persistence format.

| Outcome | stdout | stderr | Exit |
| --- | --- | --- | --- |
| Successful output mode | Requested artifact only | Empty | 0 |
| Invalid CST source | Complete tree | Labeled syntax diagnostics | 1 |
| Invalid AST or formatter source | Empty | Labeled error | 1 |
| Incomplete source highlighted | Best-effort original source | Empty | 0 |
| Read/decode/query failure | No success artifact | Actionable error | 1 |
| Invalid options | Empty | Usage error | 2 |

Preserve existing in-place/check formatter messages and failure behavior,
including exit 1 for unformatted files. Retain first-error AST validation.
JSON/S-expressions contain no generated ANSI and end with LF. Source outputs
preserve their ending except for formatter normalization. New CST/highlight
read paths must disable newline translation.

## Shared Highlighting

Use the installed grammar's `HIGHLIGHTS_QUERY` with `QueryCursor.matches()`.
Retain capture byte ranges and pattern indices; no Pygments or duplicated token
rules. Resolve overlap by shortest covering range, then later query pattern,
then lexical capture name; discard zero-length captures. This is an explicit
Toolang policy, not full native-highlighter parity. Exclude injection/local-scope
processing and custom priority directives; check future query changes for them.

Map dotted names by longest known prefix: comments/docs/punctuation `dim`,
keywords/operators `magenta`, types `cyan`, functions `blue`, properties and
`variable.parameter` `yellow`, constants `bright_cyan`, strings `green`.
Unknown captures remain unstyled; no forced terminal background.

Split UTF-8 bytes at resolved boundaries and decode into Rich `Segment` objects.
Never use byte offsets as character indices. Disable wrapping, cropping, padding,
markup interpretation, added newlines, and platform newline translation.
With color disabled, write source directly. Removing generated ANSI must recover
exact input, including long lines, Unicode, tabs, CRLF, and trailing spaces.
For `fmt --highlight`, this invariant applies to the formatted source.

Resolve environment at the CLI boundary: explicit `always`/`never` wins; in
`auto`, nonempty `NO_COLOR` disables styling, otherwise nonempty `FORCE_COLOR`
other than `0` enables it, otherwise use terminal capability detection with
`TERM=dumb` disabled. Disabled styling means no ANSI, including bold/dim.
`--html` uses Rich recorded-segment export and the same palette regardless of
terminal color settings. Escape source markup; include no remote assets or ANSI.
No browser opening/output-path flag; shell redirection selects the destination.

## Implementation Touchpoints

| Area | Likely files and responsibilities |
| --- | --- |
| Parsing | New `src/toolang/lang/cst.py`: grammar loading, raw parsing, projection, diagnostics. `lang/ast.py` delegates parser construction while retaining its preprocessing/validation. |
| Formatting | `src/toolang/lang/format.py`: selected signature/grouping/message rules, building on PR #532. |
| Captures | New `src/toolang/lang/highlight.py`: queries and deterministic byte spans; no Rich/filesystem/environment/CLI dependencies. |
| CLI | `src/toolang/cli/toolang/commands/program.py`: inputs, discovery, validation, orchestration. New `cli/toolang/source_output.py`: S-expression presentation, shared rendering options/palette, Rich/HTML. |
| Registration | `src/toolang/cli/toolang/{main,routing}.py`: existing factories, help panels, target-free commands. |
| Dependencies | `pyproject.toml`, `uv.lock`: require `tree-sitter>=0.25.2,<0.26` for QueryCursor; inherit `tree-sitter-toolang>=0.3.2,<0.4` from PR #532. Reuse Rich. |
| Tests | New `tests/unit/lang/test_cst.py`, `test_highlight.py`, `tests/unit/cli/test_source_output.py`, `tests/integration/cli/test_source_commands.py`; extend formatter and CLI routing/help/entry-point tests. |
| Docs | New `docs/source-commands.md`; update authoring conventions, `docs/index.md`, README examples, and `CHANGELOG.md`. |

Keep facades narrow and behavior with its owner. Exclude a new top-level `check`,
grammar/semantic changes, external CLI integration, custom queries/themes,
embedded-language highlighting, AST editing, LSP/watch/incremental sessions,
tags/arbitrary query commands, formatter diffs, and runtime/state/storage changes.
Existing `too query` remains unrelated collection-query tooling.

## Acceptance and Delivery

1. Verify both executable entry points, lazy Source Commands help, no setup or
   side effects, preserved script/agent routing, and command-name collision escape.
2. Cover all formatter modes, check status, discovery/deduplication, stdin labels,
   tab size, rejected combinations before writes, unchanged source files in
   output modes, and direct highlighting versus highlighting formatted stdout.
   Exercise every convention above, mixed inline/block messages, attachment
   barriers, interleaved directive keys/operators, idempotence, and AST invariance.
3. Cover all four tree/representation combinations, default AST/S-expression,
   mutually exclusive flags, compact compatibility, and exact existing AST JSON.
   Exercise every AST family, strings/escapes/Unicode/containers, and metadata
   keys named `kind`; TTY must not change representation.
4. Compare CST fields/anonymous nodes/ranges with the installed parser. Cover
   errors/missing/invalid nodes, raw hashes, empty/no-final-LF input, shebangs,
   tabs, CRLF, Unicode/emoji, and AST preprocessing differences. Cover 0.3.2
   module/item/parameter docs, `_`, duplicate/unknown/malformed tags, ordinary
   `@return` prose, legacy markers, literal markers, and doc/name/type/optional
   preservation. Keep syntax-valid, semantic-invalid formatting/highlighting.
5. Test packaged query captures, overlap/ties/prefix fallback/unknown captures,
   incomplete source, color/environment precedence, exact bytes after removing
   ANSI, long lines, and escaped standalone HTML without remote assets.
6. Cover missing paths, unsupported suffixes, single-source restrictions,
   invalid UTF-8, stdin combinations, spaces/dashes, and stdout/stderr separation.
   Exercise large generated source without timing thresholds; avoid quadratic
   span resolution and per-character parsing. Keep all tests offline.

Implementation order: confirm this definition → rebase onto merged PR #532 →
shared raw parser/CST → formatter conventions → shared highlighting and tree
presentation → CLI/help/docs and acceptance coverage. Do not implement ahead of
definition approval. Before committing implementation, run:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

Before PR handoff, fetch/rebase onto current `origin/main`, rerun verification,
push with `--force-with-lease` if rebased, and resolve review threads. Open a
ready PR; humans own merge decisions.

## Risks and Open Questions

Main risks: accidental AST-preprocessing changes during extraction, changed doc
attachment/literal text during formatting, query-overlap changes after grammar
upgrades, and rendering that changes bytes. The acceptance cases target these
boundaries. Parse's new default requires JSON consumer migration; formatter
layout changes only under the selected rules. Raising Tree-sitter's minimum
excludes 0.24.x environments; the current lock already uses 0.25.2.

No technical choice remains open within the approved scope. Later capabilities
require another definition.

## Technical References

- [Tree-sitter highlighting](https://tree-sitter.github.io/tree-sitter/3-syntax-highlighting.html)
- [Python QueryCursor](https://tree-sitter.github.io/py-tree-sitter/classes/tree_sitter.QueryCursor.html)
- [Rich rendering and export](https://rich.readthedocs.io/en/stable/console.html)
- [CLI output conventions](https://clig.dev/#output)
- [Toolang 0.3.2 documentation grammar](https://github.com/openhat-ai/tree-sitter-toolang/blob/v0.3.2/GRAMMAR.md#comments-and-documentation)

# Language Commands

`parse`, `fmt`, and `highlight` operate offline on Toolang source through the
installed Tree-sitter grammar. They require no agent Setup, model configuration,
or network access. [AST](ast.md) owns semantic representation;
[Program](program.md) owns declarations and [Flow](flow.md) owns evaluation.

## Parse

Parse exposes two views:

| View | Boundary |
| --- | --- |
| AST, the default | Lower and validate a semantic `Program`; invalid source yields no partial AST. |
| CST | Retain the original UTF-8 syntax tree, including incomplete source and recovery nodes. |

Both views support S-expressions and JSON independently of terminal detection.
JSON is the downstream scripting interface. AST serialization retains semantic
nodes and documentation; CST JSON has schema version `1`, grammar/version,
original source, complete nodes, and syntax diagnostics. CST node ranges are
half-open byte offsets with zero-based rows and UTF-8 byte columns; AST source
lines are one-based. Grammar node names are version-dependent and do not define
a runtime storage schema.

AST validation checks declarations, templates, types, and statically known
Flow contracts without resolving installed resources. Unknown inherited context,
dynamic values, conversions, and current resource readiness remain runtime checks.
Validation does not infer signatures through calls. [AST](ast.md#validation-boundaries)
owns this division.

Raw CST inspection does not mask query-data hashes or append a missing final
newline as the AST path can. The views can therefore expose different recovery
regions. CST output remains complete on syntax failure, with diagnostics on
stderr and exit status 1; semantic errors do not invalidate CST output.

`parse --check` recursively validates and deduplicates source files without
printing trees, rewriting files, or requiring canonical formatting. It reports
the first failure per file and continues across the batch.

## Format

Formatting transforms syntax-valid source, independently of semantic validation.
It preserves source order, explicit types, literal text, and documentation
bindings while normalizing structural whitespace and adjacent directive/import
groups. [AUTHORING.md](../AUTHORING.md#automatic-formatting) owns the formatting
conventions.

In-place and check modes discover `.too` files recursively and deduplicate
resolved paths. A failed file is never written; successful earlier writes in a
batch remain. Check mode reports whether formatting changes are needed.
Stdout/highlight modes accept one file or stdin without modifying source.
Formatting precedes highlighting, so capture positions refer to formatted text.

Syntax failures report the first source diagnostic. Invalid formatter-generated
syntax is attributed to generated coordinates rather than the input file.
Rendering completes before code or HTML is emitted, preventing partial artifacts.

## Highlight

Highlighting applies the grammar's packaged query without syntax or semantic
validation. Incomplete source is rendered best-effort. Removing terminal colors
leaves the original text, including tabs, line endings, trailing whitespace,
and a missing final newline. HTML escapes source markup and embeds its assets.

Explicit color mode wins. Auto mode respects `NO_COLOR`, then `FORCE_COLOR`,
then terminal capability; pipes are plain unless forced. HTML uses the shared
palette independently of terminal color settings. [Terminal UI](terminal-ui.md)
owns that palette. Rendering failures retain the source label without fabricated
source coordinates.

## Stdin, paths, and errors

Inputs are UTF-8 local `.too` files or explicit stdin (`-`), never remote refs or
agent names. A stdin filepath supplies a diagnostic label without reading or
writing that path. No-argument calls show help without scanning the working
directory. AST/format paths normalize line endings; raw CST/highlight retain them.

Source diagnostics use one-based lines and UTF-8 byte columns, with escaped,
bounded excerpts and optional notes for related declarations. Unknown coordinates
are omitted. Parser recovery ranges identify observed syntax structure rather
than proving the causal token. Syntax diagnostics and semantic errors remain
distinct; library exceptions expose structured diagnostics separately from text.

Successful output modes write only their artifact to stdout and return 0.
Source, I/O, decode, and rendering failures return 1 on stderr; invalid option
combinations normally return 2. JSON/S-expressions end with LF and contain no
generated ANSI. Source highlighting retains its original ending; formatting
applies its normalization.

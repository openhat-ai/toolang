# Source Commands

`too` and `toolang` expose the same offline `parse`, `fmt`, and `highlight`
commands, listed by `too more` rather than the main help. They use the
installed Tree-sitter Python grammar and Rich; no
Tree-sitter CLI, agent setup, model configuration, or network is required.

## Parse

```sh
too parse work.too                    # Semantic AST, S-expression
too parse work.too --ast --json       # Semantic AST, JSON
too parse work.too --cst              # Raw CST, S-expression
too parse work.too --cst --json       # Complete CST, JSON
too parse scripts/ other.too --check  # Validate sources without printing trees
```

`--ast` (default) and `--cst` select the tree; `--json` independently selects
JSON instead of the default indented S-expression. The representation does not
change between a terminal and a pipe. `--compact` implies compact JSON and can
also accompany `--json`.

The AST is the lowered, validated Toolang `Program`. It includes declarations,
flow statements, signatures, and module/runnable/parameter documentation.
Validation checks source semantics without resolving installed caps or
preparing execution. Invalid input produces an error and no partial AST.

`--check` accepts multiple files/directories, recursively discovers `.too` files,
and deduplicates resolved paths. It emits nothing on success, reports the first
error per file as `path:line:column: message`, continues checking other files,
and exits 1 if any source fails. It never rewrites files or requires canonical
formatting. Stdin is supported as the sole source. `--cst`, `--json`, and
`--compact` cannot accompany `--check`; explicit `--ast` is allowed.

Both AST modes check template syntax, reserved references, declared inputs,
known types, and operation contracts. Flow checks follow local bindings and
item/list shapes, detect definitely missing call inputs, and validate known
repeat/settle windows. Empty parallel operations and implicit-seed singleton
settle validate inputs without checking unused child history templates.
An array-valued item is distinct from a flow list.
Unknown inherited context, dynamic values, and value conversions remain runtime
checks; insufficient history within a valid window remains normal until warm-up.
These checks do not infer signatures through calls. Source errors use
`path:line:column: reason: 'excerpt'`. Locations are one-based lines and UTF-8
byte columns (a tab counts as one byte). Unknown coordinates are omitted.
I/O and highlighting failures do not receive a fictitious `1:1` source position.

Syntax errors explain missing punctuation, missing types/values, or malformed
statements when supported by the parser. Other failures use `Parse error` and
retain the enclosing recovery range. A nested recovery node alone does not prove
which token caused the error. Semantic errors use known declaration/statement
anchors; previous declarations or conflicting properties appear on separate
`path:line:column: note: reason` lines.

Excerpts come from the original source, are escaped and bounded to 100 characters
plus a truncation marker, and include block context for recovery errors. Error
messages do not contain repair advice. Source order takes precedence over
specificity. See [diagnostic examples](#diagnostic-examples) for representative AST/CST differences.

Library source exceptions expose their structured diagnostic separately from
`str(error)`, which includes known locations but no source excerpt.

The CST parses the original UTF-8 bytes, including incomplete source. Unlike
the AST path, it neither masks query-data hashes nor adds a final newline, so
the two views can differ. Syntax errors still produce a complete CST, with
diagnostics on stderr and exit status 1. Semantic errors do not invalidate CST
output.

CST S-expressions show named nodes, fields, and error/missing markers. JSON also
includes anonymous tokens and preserves all whitespace through the original
`source` field. Its envelope contains `schema_version: 1`, the grammar name and
installed version, `source`, `root`, and `diagnostics`. Nodes contain:

- `type`, `field` (parent field name or null), and ordered `children`;
- `is_named`, `is_extra`, `is_error`, `is_missing`, and `has_error`;
- `start_byte`/`end_byte` and `start_point`/`end_point`.

Ranges are half-open. Points are `{row, column}` with zero-based rows and UTF-8
byte columns. AST `span.line` remains one-based. Diagnostics distinguish native
errors, missing nodes, and grammar-specific invalid nodes. Node names track the
grammar version; this inspection format is not a runtime storage schema.
Diagnostic `message` values contain only the reason, without a location or
excerpt. They use the same syntax explanations as AST parsing and formatting. All native diagnostic entries and overlapping ranges are retained;
human explanations do not change the raw node types or parser recovery markers.

AST S-expressions use `(kind field: value ...)`, `(span line: N)`, `(list ...)`,
and `(map ("key" value) ...)`. Strings/scalars use JSON escaping, and empty
containers remain explicit. Both tree displays use two-space indentation;
JSON is the supported interface for downstream scripts.

## Format

```sh
too fmt work.too                      # Rewrite in place
too fmt scripts/ other.too --check    # Check only; no writes
too fmt work.too --stdout             # Plain formatted source
too fmt work.too --highlight          # Highlighted formatted source
too fmt work.too --highlight --html > formatted.html
```

In-place formatting and `--check` accept multiple files/directories, discover
`.too` files recursively, and deduplicate resolved paths. `--check` returns 0
when formatting is current and 1 when changes are needed. It checks formatting,
not semantic validity. Syntax errors prevent formatting.
Syntax failures use `path:line:column: message`, including stdin's diagnostic
label, with the same location units as `parse`. Formatting retains only the
first syntax error. If the formatter itself produces invalid syntax, the error
explicitly identifies a generated line/column instead of attributing that
position to the input file. The failing file is not written; files formatted
successfully earlier in the same batch remain changed.

`--stdout` and `--highlight` accept exactly one file or stdin and never modify
source files. `--highlight` implies stdout mode; `--stdout --highlight` is also
valid. Both reject directories, multiple files, and `--check`. Formatting runs
before highlighting, so capture positions refer to the formatted source.
Output modes emit only code/HTML, without status messages.


`too fmt` applies mechanical conventions consistently to file writes, `--check`,
`--stdout`, and `--highlight`:

- Use two spaces for structural indentation by default (`--tab-size` overrides).
- Preserve omitted signature types: `agic rewrite(_, instruction):` remains
  concise. Keep explicit types, return annotations, `()`, and optional `?`.
- Keep adjacent `with` clauses of the same cap kind together, with one blank
  line between different kinds. Preserve source order; do not alphabetize.
- Group each contiguous resource-directive section by key, ordering key groups
  by first appearance and preserving each key's operator/value order. Keep all
  groups compact without blank lines. Never move directives across comments or
  other syntax, so documentation ownership remains unchanged.
- Keep adjacent inline `user:`/`assistant:`/`tool:` messages compact. Keep block
  messages as blocks and preserve role names.
- Separate prose and explicit flow statements with a structural blank line,
  while preserving whitespace inside each literal text body.
- Normalize comment/tag spacing using the installed grammar. Preserve documentation
  attachment and deliberate detachment, exact parameter names, legacy marker
  spellings, module boundaries, and the executable shebang.

Comments and text ownership take precedence over compact grouping. Formatting
must be idempotent and preserve semantic content and documentation bindings.
The formatter works on syntax-valid source even when semantic validation fails.

Naming, inserting `{{_}}`, rewriting prose, deleting explicit types or redundant
resource directives, hoisting `context`/`instruct`, and omitting a sole `user:`
role remain authoring decisions. The formatter does not make those rewrites.
Recommended source style belongs to the website
[Authoring Conventions](https://toolang.ai/docs/toolang-conventions).


## Highlight

```sh
too highlight work.too
too highlight work.too --color always > colored.txt
too highlight work.too --html > source.html
```

Highlighting uses the grammar's packaged query. Incomplete and semantic-invalid
source is rendered best-effort without validation errors. With colors removed,
terminal output retains the input exactly, including tabs, CRLF, trailing spaces,
and a missing final newline. HTML escapes source markup and uses no remote assets.
Rendering failures include the source label and underlying cause, without a
fabricated source position. Both standalone highlighting and `fmt --highlight`
finish rendering before emitting code or HTML, so failures emit no partial result.

Both `highlight` and `fmt --highlight` accept:

- `--color auto|always|never`: default `auto`; explicit `always`/`never` wins.
  In auto mode, nonempty `NO_COLOR` disables styling, otherwise nonempty
  `FORCE_COLOR` other than `0` enables it, otherwise a capable terminal enables
  it (`TERM=dumb` disables it). Pipes are plain unless color is forced.
- `--html`: export standalone HTML, using the shared palette regardless of
  terminal color settings. `--color` applies only to terminal output.

On `fmt`, rendering options require `--highlight`. Custom grammars, queries,
themes, embedded-language highlighting, and arbitrary Tree-sitter query
execution are outside this interface. Resource lists expose query fields through `--json`.

## Stdin, Paths, and Errors

```sh
cat work.too | too parse - --cst --json
cat work.too | too fmt - --highlight
cat work.too | too highlight - --stdin-filepath work.too
too highlight -- -example.too
```

Use `-` as the sole source. `--stdin-filepath PATH` supplies a diagnostic label;
it never reads or writes that path. The legacy `fmt --stdin-filepath PATH`
shorthand also reads stdin without an explicit `-`. `fmt --check` rejects stdin.
No-argument calls show help without scanning the working directory. Inputs must
be UTF-8 local `.too` files; URLs and agent selectors are not accepted. Stdin is
also decoded as UTF-8, independently of Python's stream encoding. AST parsing
and all formatter modes accept LF, CRLF, and CR line endings through universal
newline normalization; raw CST and original-source highlighting retain them.

Successful output modes return 0 and write only their artifact to stdout.
Syntax/validation, file/decode, and rendering failures return 1 with diagnostics
on stderr; invalid option combinations return 2. Existing in-place formatter
messages and legacy option error statuses are retained. JSON/S-expressions
end with LF and contain no generated ANSI; source output retains its own ending
except for formatter normalization.

## Diagnostic examples

These inputs intentionally fail `too parse --check - --stdin-filepath case.too`
with exit 1 and no stdout. Escaped newlines show exact source boundaries.

| Input | Expected diagnostic |
| --- | --- |
| `flow work(value: Text:\n  pass\n` | `case.too:1:22: Expected ')' in parameter list` |
| `struct X:\n  field:\n` | `case.too:2:9: Expected a field type` |
| `flow work:\n  sort these items\n` | `case.too:2:3: Malformed flow statement 'sort'` |

Actual stderr appends the bounded source excerpt. AST/check reports the first
error; raw CST retains every native error, including overlapping recovery
regions. Missing final newlines can change raw CST recovery; AST/fmt normalize
the source boundary. `highlight` renders these invalid sources without syntax
validation. Duplicate declarations/properties report the current location and a
separate note pointing to the previous one.

Implementation: [language tooling](../src/toolang/lang/),
[CLI source commands](../src/toolang/cli/toolang/commands/program.py).
Verification: [diagnostics](../tests/unit/lang/test_diagnostics.py),
[format contracts](../tests/unit/lang/test_format_contract.py),
[CLI diagnostic contract](../tests/integration/cli/test_diagnostic_contract.py).

# Source Commands

`too` and `toolang` expose the same offline `parse`, `fmt`, and `highlight`
commands. They use the installed Tree-sitter Python grammar and Rich; no
Tree-sitter CLI, agent setup, model configuration, or network is required.

## Parse

```sh
too parse work.too                    # Semantic AST, S-expression
too parse work.too --ast --json       # Semantic AST, JSON
too parse work.too --cst              # Raw CST, S-expression
too parse work.too --cst --json       # Complete CST, JSON
```

`--ast` (default) and `--cst` select the tree; `--json` independently selects
JSON instead of the default indented S-expression. The representation does not
change between a terminal and a pipe. `--compact` implies compact JSON and can
also accompany `--json`.

**Migration:** `parse` previously emitted AST JSON by default. Add `--json` to
existing scripts; `--compact` retains its previous behavior and AST JSON fields
remain unchanged.

The AST is the lowered, validated Toolang `Program`. It includes declarations,
flow statements, signatures, and module/runnable/parameter documentation.
Validation checks source semantics without resolving installed capabilities or
preparing execution. Invalid input produces an error and no partial AST.

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

`--stdout` and `--highlight` accept exactly one file or stdin and never modify
source files. `--highlight` implies stdout mode; `--stdout --highlight` is also
valid. Both reject directories, multiple files, and `--check`. Formatting runs
before highlighting, so capture positions refer to the formatted source.
Output modes emit only code/HTML, without status messages.

`--tab-size N` sets structural indentation (positive integer, default 2).
Formatting preserves omitted types, compacts adjacent imports of the same cap
kind and resource directives of the same key, and keeps adjacent inline role
messages compact. Different import kinds/directive keys are separated by one
blank line. Source order, authored explicit types, literal text, and documentation
bindings are preserved. See the
[formatter conventions](./toolang-authoring-conventions.md#automatic-formatting)
for the boundary between mechanical rules and authoring decisions.

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

Both `highlight` and `fmt --highlight` accept:

- `--color auto|always|never`: default `auto`; explicit `always`/`never` wins.
  In auto mode, nonempty `NO_COLOR` disables styling, otherwise nonempty
  `FORCE_COLOR` other than `0` enables it, otherwise a capable terminal enables
  it (`TERM=dumb` disables it). Pipes are plain unless color is forced.
- `--html`: export standalone HTML, using the shared palette regardless of
  terminal color settings. `--color` applies only to terminal output.

On `fmt`, rendering options require `--highlight`. Custom grammars, queries,
themes, embedded-language highlighting, and arbitrary Tree-sitter query
execution are outside this interface. `too query` remains collection-query help.

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
be UTF-8 local `.too` files; URLs and agent selectors are not accepted.

Successful output modes return 0 and write only their artifact to stdout.
Syntax/validation, file/decode, and rendering failures return 1 with diagnostics
on stderr; invalid option combinations return 2. Existing in-place formatter
messages and legacy option error statuses are retained. JSON/S-expressions
end with LF and contain no generated ANSI; source output retains its own ending
except for formatter normalization.

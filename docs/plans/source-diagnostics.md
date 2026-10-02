# Source Diagnostics

Proposed; implementation requires human approval. Make source-command failures
actionable by sharing syntax diagnosis while preserving each command's parsing
and validation contract. This supplements [source developer commands](source-developer-commands.md)
and [static source checks](static-source-checks.md).

## Scope and success criteria

- Cover syntax messages in default/AST `parse`, `parse --check`, all `fmt` modes,
  and `parse --cst`; also label highlighting failures and distinguish formatter
  output failures from invalid input.
- Report the most specific supported explanation, source label, and location.
  Remove the unconditional `Expected Toolang 0.3 syntax` suggestion. It does not
  establish that the input uses an older grammar.
- Preserve accepted syntax, semantic validation, grammar dependencies, public
  flags, exit codes, AST data, and raw CST schema/ranges. Keep independent
  `highlight` tolerant of incomplete and semantically invalid source.
- Exclude grammar repair, automatic fixes, new validation rules, editor/LSP
  integration, runtime diagnostics, and changes to batch formatting semantics.

## Verified problems

Reproduced against `243998d6` using the installed grammar. The first five rows
use literal source; the final two use injected failures, not naturally occurring
formatter/query failures.

| Input or failure | Current behavior | Required outcome |
| --- | --- | --- |
| `flow work(value: Text:\n  pass\n` | AST/fmt replace `MISSING rparen` with a version hint; CST says `Missing rparen` | Explain the missing `)` and retain its location. |
| `flow work:\n  sort these items\n` or `agic review:\n  user, broken\n` | Generic version hint or internal `invalid_*` name | Identify the malformed flow statement or message header and its keyword. |
| `struct X:\n  field:\n` | CST says `Missing Text` | Explain that a field type is required; do not prescribe the parser's recovery choice as the only valid type. |
| `flow work(value: ):\n  run\n  repeat:\n` | AST/fmt choose the enclosing `ERROR` at 1:1 before examining its erroneous descendants | Prefer the localized parameter error; retain a fallback when recovery yields only a broad error. |
| `skill review:\n  description =\n  Review.\n` | AST explains the empty property, fmt gives a version hint, CST says `Missing text_line` | Give syntax-level consumers an actionable missing-value message; preserve the existing AST validation exception and details. |
| Highlight query raises `ValueError`/`QueryError` with a stdin filename | Original exception text survives, but the filename is omitted | Include the source label and original cause, without inventing a source position. |
| Valid source formats into invalid source | Final `_syntax_tree(formatted)` reports generated line numbers as if they belonged to the original file | Identify formatter output validation failure and explicitly label any position as generated. |

Formatting also drops structured columns for every syntax failure. Tests currently
require the version hint. CST JSON retains useful kind/type/ranges, but its
human messages expose implementation names such as `rparen` and `text_line`.
Missing-colon and empty-body examples can produce only a broad `ERROR`: changing
message text alone cannot reliably infer the intended repair.

## Design decisions

- Add a pure `lang/diagnostics.py` shared by AST parsing, formatting, and CST
  projection. It accepts CST nodes and the relevant source, with no CLI, file,
  runtime, or semantic-validator dependency. Keep exception definitions in
  `lang/errors.py`; remove formatter dependence on AST's message helper.
- Separate diagnostic collection, primary-error selection, and message rendering.
  For AST/fmt, inspect the earliest erroneous region and prefer its earliest
  localized erroneous descendant over a covering `ERROR`. Keep source order
  ahead of error kind; never choose a later missing token just because its
  message is easier to explain. Fall back to the covering region if necessary.
- Translate missing punctuation to literal tokens. Use parent/field context for
  categories such as parameter/field type and property value. Map both installed
  `invalid_flow_reserved_statement` and `invalid_agic_reserved_message` families.
  Unknown grammar nodes fall back to an unexpected source fragment and enclosing
  construct, not an unsupported claim about the expected token or language version.
- Keep messages bounded and single-line with escaped source excerpts. Preserve
  useful existing AST semantic messages, including empty capability properties;
  formatting and raw CST inspection must not call semantic validation.
- Carry structured line/column on syntax-related `ToolangFormatError` while
  retaining its `ValueError` compatibility. Render source syntax failures through
  one CLI path as `label:line:column: message`, consistently across fmt modes,
  AST parsing, and checks. Locations are one-based UTF-8 byte columns, matching
  existing native-node anchors; tabs count as one byte. Do not silently introduce
  character/display-column conversion. Retain declaration anchors for semantic
  errors when no token location exists.
- Keep raw CST node data and diagnostics entries, order, kinds, node types, and
  zero-based byte ranges intact; improve only diagnostic messages. Text stderr
  retains all CST diagnostics. Primary-error selection applies only to AST/fmt.
  Raw CST still parses original bytes; AST/fmt retain hash masking and final-LF
  normalization, so identical diagnostics across these paths are not guaranteed.
- Clamp locations caused solely by an appended newline to original EOF for
  AST/fmt. Excerpts come from original, unmasked source. Preserve BOM, shebang,
  CRLF, Unicode, and final-newline behavior; do not remap raw CST JSON ranges.
- Label rendering errors as `label: Could not highlight source: <cause>` in
  independent highlight and fmt highlight/HTML modes. Keep complete rendering
  before output, with no partial code/HTML. Do not add syntax checking to highlight.
- Wrap only the formatter's final syntax validation as a formatter output
  failure. Report `label: Formatter produced invalid syntax` plus its cause and
  explicitly generated position; do not present it as an original-file anchor.
  Do not write that file or emit formatted output after failure. Preserve existing
  first-error batch formatting and earlier successful writes; no batch transaction.

## Implementation and acceptance

- [ ] Add focused failing regression tests for every verified row before changing
  diagnostics. Assert explanations and locations rather than the version hint or
  incidental Rich wrapping; retain a few exact CLI-format assertions.
- [ ] Implement shared diagnosis and wire `lang/{ast,cst,format,errors}.py`;
  preserve existing specific semantic errors and exception compatibility.
- [ ] Update `cli/toolang/commands/program.py` to share source-error rendering,
  pass source labels through highlighting, and identify output-validation errors.
- [ ] Cover normal parse, explicit AST, check, CST text/JSON, fmt file/check/stdin/
  stdout/highlight/HTML, and standalone highlight color-never/color-always/HTML.
  Verify exit codes, stderr labels, no partial AST/code/HTML, and unchanged files
  on failed per-file formatting. CST must still emit a complete tree on failure.
- [ ] Cover nested errors, unknown diagnostic node fallback, missing punctuation/
  types/values, missing colon, empty body, malformed indentation, EOF without LF,
  BOM, shebang, CRLF, tabs, Unicode before an error, and query hashes. Require
  useful context for broad errors without asserting an unverified repair.
- [ ] Preserve parse-check continuation and first error per file, CST schema and
  complete overlapping diagnostics, highlight tolerance, rendering-cause details,
  and successful output bytes. Use deterministic failure injection for formatter
  output and query errors; require no live providers.
- [ ] Update [source-command documentation](../source-commands.md) to define
  positions and failure categories. Extend `tests/unit/lang/{test_cst,
  test_grammar_0_3,test_program_format,test_highlight}.py`, shared diagnostic tests,
  and `tests/integration/cli/{test_source_commands,test_parse_check}.py` as needed.
- [ ] Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
  and `uv run pytest -n auto` before each implementation commit. For this plan-only
  PR, verify claims and relative links and run `git diff --check`.

## Risks and open questions

Tree-sitter recovery nodes identify evidence, not necessarily the user's intended
repair. Prefer an honest localized fallback over speculative suggestions.
Grammar upgrades may change node names; mappings need fallback tests. Human
message wording changes intentionally, while raw schema and location units remain
stable. Keep the generic formatter exception catch compatible with existing
callers. No open technical decisions; human approval of this scope is pending.

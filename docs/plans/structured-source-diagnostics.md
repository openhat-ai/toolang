# Structured Source Diagnostics

Proposed; human approval is required before implementation. This supersedes the
message construction and primary-error selection rules in
[source diagnostics](source-diagnostics.md), retaining its command contracts.
PR #666 is a presentation fix; this definition addresses the underlying design.

## Goal and scope

Make every source error separate its cause, location, and source excerpt. A single
renderer must produce concise output without removing text from exception strings.
Location precision must reflect available evidence, not imply that parser recovery
has identified the faulty token.

Cover syntax and static semantic failures from `Program.from_source`, source
formatting, CST inspection, and their CLI commands. Include the presentation of
I/O, highlight-rendering, and generated-output failures at these command boundaries.
Preserve grammar dependencies, accepted programs, validation rules, command flags,
exit codes, AST serialization, and raw CST nodes/ranges/diagnostic entries.
Independent highlighting remains tolerant of invalid source.

Exclude automatic repairs, inferred expected-token sets, new grammar recognition
rules, terminal caret displays, public diagnostic codes, LSP integration, and a
redesign of runtime exceptions. No speculative fix or suggestion fields are needed.

## Verified causes

Inspected main at `7419b906` and the output audit of PR #666 at `2fa6ec4d`.

- `lang/diagnostics.py` combines explanation, line number, and excerpt in one
  string; `program.py` removes a particular prefix before adding the CLI location.
- `validate.py`, `lower.py`, and `contracts.py` also embed locations in messages.
  Empty properties still repeat their line after the PR #666 change.
- Generic context concatenation produces `Expected a field type in field
  declaration`. Static flow diagnostics use another capitalization/style convention.
- `primary_error` treats deeper generic recovery nodes as more precise. An empty
  `repeat` body can therefore point to the following valid `run` statement.
- AST `Span` currently carries only a line. Semantic token columns cannot be
  recovered merely by changing the message format.
- Raw CST intentionally retains overlapping errors and does not append a newline;
  differences from AST/fmt are not all rendering defects.

## Design decisions

### Diagnostic data and ownership

Add small immutable records to `lang/types.py`: a source location, a related
location with its explanation, and a source diagnostic. A diagnostic contains a
plain reason, an optional primary location, and related locations. Locations carry
one-based line/UTF-8 byte columns, an optional end point, precision (`token`,
`construct`, or `recovery`), and origin (`authored` or `generated`). A column may be
unknown; an end point is exclusive. No file access, CLI label, or full source text
belongs in these records. Raw CST zero-based ranges remain a separate projection.

Exceptions carry this diagnostic. Keep existing exception classes, catch behavior,
and constructor compatibility; preserve `ToolangFormatError` as a `ValueError`.
Expose `.line` and `.column` through the diagnostic instead of keeping independent
copies. `source_location` fills absent locations without overwriting more specific
ones. Retain existing `__cause__` chains.

`str(error)` uses a shared plain-text renderer that includes known location once,
so non-CLI callers retain useful errors. The source CLI reads the diagnostic
directly. Remove prefix stripping and do not parse legacy prose to extract positions.
Opaque third-party causes retain their text and acquire only independently known
location context.

### Cause generation

Keep syntax interpretation in `lang/diagnostics.py` and semantic rules in their
own validators. A universal catalog of all semantic error sentences is unnecessary.
Unify their message contract instead: sentence case, a factual reason, quoted
identifiers, no location, no excerpt, no repair advice, and no terminal period.
Do not rewrite or change the meaning of opaque upstream messages.

Use four explicit syntax templates: `Expected {token}`, `Expected {category}`,
`Malformed {construct}`, and `Parse error`. A category already contains necessary
context (`a field type`, `a parameter type`, `a property value after '='`). Append
context only when it adds information, as in `Expected ')' in parameter list`.
Retain grammar evidence for malformed reserved statements and message headers.
Unknown nodes use the fallback; do not report recovery's arbitrary `Text` token
as the only valid field type.

Migrate all source-reachable errors in lowering, validation, operation contracts,
and flow validation in one implementation milestone. Preserve useful identifiers
and semantic distinctions. Move the primary line into its location; move genuinely
different positions (previous declarations or conflicting properties) into related
locations. Never delete every `at line` substring indiscriminately.

### Location and error selection

Keep source order ahead of specificity. In the earliest error region, descend only
when the first erroneous branch supplies supported token/construct evidence;
never skip an earlier unexplained error to report a later easier one. A nested
generic `ERROR` is not by itself stronger evidence. Without a supported descendant,
retain the covering recovery range and identify its known enclosing construct.
Do not label the next valid statement as the cause of an empty preceding block.

Use native positions for syntax evidence and existing declaration/statement anchors
for semantic errors. Correct explicit property/documentation anchors while migrating
their messages. Do not expand public AST spans or invent missing token coordinates.
An unknown location stays unknown instead of becoming `1:1`.

Retain BOM, shebang, newline, tab, Unicode, query-mask, and authored-EOF behavior.
Build any line index once per source; avoid per-diagnostic prefix scans.

### Rendering and compatibility

- CLI output is `path:line:column: reason: 'excerpt'`. Omit unknown coordinates
  and absent excerpts rather than manufacturing them.
- The renderer receives the resolved source label and source text at the call
  site. Use an escaped excerpt bounded to 100 source characters. Token/construct
  diagnostics use the original line around the anchor; recovery diagnostics use
  the bounded recovery region, including escaped newlines when necessary.
- Emit related locations on separate `path:line:column: note: reason` lines.
  These are facts such as a previous declaration, never repair suggestions.
- Library rendering uses the same reason and location rules without a file label
  or source excerpt. Word-for-word exception strings may change intentionally.
- CST JSON retains schema version, keys, raw ranges, ordering, and overlapping
  entries. Its `message` becomes the plain reason; the structured range already
  supplies location. CST stderr renders every entry through the shared renderer.
- AST/check/fmt retain the first error per file. Raw CST normalization differences
  remain documented; equal evidence must render equally, not necessarily produce
  identical diagnostic sets.
- Generated formatter failures explicitly label generated coordinates and never
  use them as the input-file position. I/O and highlight-rendering errors use the
  file label and underlying cause without a fictitious source coordinate.

Examples of the intended format (locations are still evidence-dependent):

```text
broken.too:1:22: Expected ')' in parameter list: 'flow work(value: Text:'
fields.too:2:9: Expected a field type: 'field:'
skill.too:2:16: Property 'description' in skill 'review' must be nonempty: 'description ='
aide.too:4:1: Parse error in agic block: 'agic issue(_: Text) sdf:\n  pass'
```

The `aide` example deliberately does not claim column 21 or an expected `:`/`->`:
the current recovered tree does not reliably supply those facts.

## Implementation milestones and touchpoints

1. Introduce records and exception compatibility in `lang/{types,errors}.py`,
   with pure rendering/diagnosis in `lang/diagnostics.py`.
2. Migrate `lang/{ast,lower,validate,contracts,flow_validation,format,cst}.py` and
   route source CLI output through `cli/toolang/{commands/program,source_output}.py`.
   Complete all source-reachable semantic paths before handing off this milestone.
3. Apply evidence-based primary selection and bounded recovery-region excerpts;
   update `docs/source-commands.md` and the diagnostic output matrix together.

Do not merge a partial migration that leaves regex/prefix cleanup in the CLI.
The existing concise-output PR can land independently; it is not a prerequisite.

## Acceptance and verification

- Preserve the 14 audited source cases as a table-driven corpus. Check structured
  reason/location/precision separately from exact CLI output. Review the full
  output matrix for ordinary parse, check, fmt, CST text/JSON, and highlight modes.
- Missing punctuation/types/values remain specific; the empty-property case has
  no repeated line; field types have no duplicated context. Generic failures make
  no unsupported expected-token or faulty-token claim.
- Empty `repeat` bodies retain recovery context instead of accusing the following
  valid `run`. Earlier generic errors still take precedence over later missing tokens.
- Duplicate properties and conflicting declarations retain both relevant locations.
  Cover validation through `lower`, `validate`, `contracts`, and `flow_validation`,
  plus template failures wrapped at the language boundary.
- Verify unchanged exception catch compatibility and useful `str(error)` output
  through library and state-preparation callers; no duplicate location in generated
  formatter failures. Test unknown positions without a synthetic `1:1`.
- Cover long lines, control characters, Unicode, tabs, CRLF, BOM, shebang, EOF without
  LF, query masking, and many diagnostics. Preserve the linear-scan regression test.
- Preserve accepted programs, flags, exit codes, raw CST fields/ranges, invalid-source
  highlighting, parse-check continuation, no partial formatted output, and unchanged
  files on formatting failure.
- Extend `tests/unit/lang/`, `tests/integration/cli/{test_source_commands,
  test_parse_check}.py`, and relevant `tests/unit/state/` compatibility tests.
  Before each code commit run Ruff lint/format, ty, and `uv run pytest -n auto`.
  For this definition, verify references and run `git diff --check` only.

## Risks and open questions

Grammar recovery can change across dependency versions. Test evidence policies as
well as representative outputs, and keep a safe fallback. Message wording is an
intentional compatibility change; exception types and CST structure are preserved.
This design improves consistency and honest localization, not universal root-cause
inference. More precise grammar-specific diagnosis requires a separate definition.

No blocking technical questions remain. Human approval of this definition is pending.

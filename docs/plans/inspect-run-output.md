# Inspect Run Output

## Status

Approved by the human on 2026-10-02, including Rich rendering for JSON.
Implementation is authorized.

## Goal And Success Criteria

Read a Run's result without knowing its `Output` and `Local` storage wrappers.
Keep complete structured output available and make Markdown viewing optional.

Approved syntax (`too` is an alias for `toolang`):

```sh
too SCRIPT inspect RUN output
too SCRIPT inspect RUN output --json
too SCRIPT inspect RUN output --markdown
```

Success means these commands return only the result body, formatted JSON, or
rendered Markdown respectively, without requiring `/output/local/value`, `jq`
for indentation, or an external Markdown renderer.

## Verified Current Behavior

- `inspect` accepts explicit `RUN tree` and `STEP call` projectors through a
  typed registry. There is no `output` projector.
- Run records contain `output: Output | None`; `Output` contains `local` and
  `binding`, while `Local` contains the typed value and dimension.
- Pointer `--json` prints the selected canonical data without dereferencing it.
  JSON already uses two-space indentation, so `| jq '.'` is unnecessary solely
  for formatting.
- Human value inspection resolves references; Text is literal text, while
  Parts use the shared response presentation. Structured field selections can
  produce tables or summaries instead of a complete result document.
- `RunStore.resolve_local()` already resolves nested references and validates
  the resulting type. No new execution resolver is needed.

The reported `examples/redoc.too` is absent from the inspected checkout. These
findings come from implementation and existing tests, not that specific Run.

## Decisions

### Syntax And Scope

Register `output` as an explicit terminal projector for whole Run subjects,
beside `tree`. It is not a field alias or a change to Pointer grammar.
`inspect RUN/output` and deeper pointers retain their existing meanings.
`inspect RUN` continues to inspect the record.

The first version covers Runs only. Step, Thread, Control, collection, and field
subjects reject the `output` projector using the existing allowed-view errors.
Do not add a top-level `output` command, an abbreviated alias, a latest-Run
selector, Run-ID prefixes, or implicit agent/script selection in this change.

### Value Resolution

Read the selected Run and resolve `run.output.local` with
`RunStore.resolve_local()` within the existing inspection read transaction.
Serialize the resolved Local with `local_to_protocol_data()` and take its
`value`. Do not return the binding, type, dimension, or a synthetic envelope.
Preserve all nested fields, list order, Part records, and scalar types in JSON.

This is a resolved result view: its JSON can differ from the raw canonical
`RUN/output/local/value --json` when the stored value contains references.
The raw Pointer view remains available for provenance and debugging.

If `run.output is None`, fail with exit code 1 and an error identifying the Run
and its status; write no result to stdout. Do not wait, select another Run, or
infer output from its last Step. A present output containing null, an empty
string, or an empty collection is valid and succeeds. An output present on any
Run status can be read. Missing Runs, broken references, cycles, and validation
failures retain the existing inspection error handling.

### Presentation

`--human`, `--json`, and the new `--markdown` are mutually exclusive; Human is
the default. `--markdown` is supported only with the new `output` projector;
other queries reject it with a usage error. Existing queries retain their
current presentation and options.

| Result | Default / `--human` | `--json` | `--markdown` |
| --- | --- | --- | --- |
| Text | Literal complete text | JSON string | Render text as Markdown |
| Part / Part[] | Plain response text via existing `parts_response_text()` policy | Complete serialized Part value | Render that response text as Markdown when textual content exists |
| Other values | Complete indented JSON | Complete indented JSON | Usage error; suggest `--json` |

For Parts without textual content, Human retains the existing helper's
structured fallback; Markdown rejects nonempty nontext-only content rather
than rendering its JSON as prose. Empty Parts render an empty body. The Parts
Human view remains a response projection and can omit reasoning and nontext
content; `--json` preserves the entire resolved value.

Literal Text preserves whitespace; the CLI adds a final newline only when
needed. Do not interpret Rich markup, wrap, summarize, or truncate plain text
and JSON. No titles, metadata tables, or footers precede or follow the result.
Use Rich for JSON syntax highlighting on terminals and explicit Markdown
presentation. Redirected JSON remains valid, uncolored JSON. Apply this to the
new output projector only; existing JSON projections remain unchanged. Do not
change the selected format based on whether stdout is a terminal or a pipe.

The default remains useful with external tools:

```sh
too SCRIPT inspect RUN output | rich -m
too SCRIPT inspect RUN output --json | jq '.summary'
```

### Tradeoffs

An explicit projector fits the current grammar and keeps record inspection
predictable. Shortening `/output` by implicitly unwrapping it would change an
existing canonical field view. A separate command duplicates inspect routing.
Automatic Markdown for all Text would alter plain-text piping; an explicit
flag lets callers choose the presentation.

## Design Touchpoints And Likely Files

- `src/toolang/cli/toolang/commands/inspect.py`: register and implement the Run
  projector, resolve its value, validate formats, and render complete output.
  Derive help and allowed-projector errors from the registry.
- `src/toolang/cli/common/human_values.py`: reuse the existing Parts response
  text and Markdown presentation helpers; change only if a small shared helper
  is needed. Preserve existing callers' behavior.
- `tests/unit/cli/test_inspect_subject_navigation.py`: registry and grammar.
- `tests/unit/cli/test_inspect_rendering.py`: output types and formats.
- `tests/unit/cli/test_cli_help.py`: preserve error assertions when the new
  option appears in spelling suggestions.
- `tests/integration/cli/test_local_core_commands.py`: offline CLI acceptance.
- `docs/api.md`: document syntax, resolved JSON, format selection, and errors.

No persistence, schema, execution, API, plugin, or entry-point changes are
needed. Script selection and execution-store opening retain current behavior.

## Acceptance Tests

1. A Run with Text output prints only its full text, including Unicode, blank
   lines, indentation, and Rich-like markup, without width-based truncation or
   wrapping. The JSON view parses as the same string and is indented.
2. Structured, scalar, null, and empty outputs round-trip through JSON; Human
   emits complete JSON for nontext values rather than table previews.
3. Textual and mixed Parts obey the shared Human response policy; JSON retains
   all Parts. Markdown renders textual content, handles empty text, and rejects
   unsupported values with a clear usage error.
4. Direct, chained, and nested references resolve in both Human and JSON
   output; missing targets, cycles, and type errors fail without partial stdout.
5. Absent output fails with Run/status context; present empty or null output
   succeeds. Existing history is read without executing the script or creating
   an absent store.
6. Only whole Run subjects accept `output`; conflicting format flags and
   `--markdown` on other queries fail. Help lists the new view and its formats.
7. Existing Pointer views, raw JSON, `RUN tree`, and `STEP call` remain
   unchanged. Pipes preserve the explicitly selected presentation.
8. Run the repository's default verification before implementation commits.

## Risks And Open Questions

Resolving large outputs can be expensive; keep the current unbounded inspection
model without adding pagination or truncation. Explicitly document the
difference between raw Pointer JSON and resolved result JSON. No unresolved
implementation decisions remain in this proposal; the scope and command design are approved.

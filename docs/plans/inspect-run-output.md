# Inspect Run Output

## Status

Approved by the human on 2026-10-02: add only an `output` view that extracts
content. Leave presentation to external tools and retain existing format flags.
Implementation is authorized.

## Goal And Success Criteria

Read a Run result without its `Output` and `Local` storage wrappers. `too` is
an alias for `toolang`.

```sh
too SCRIPT inspect RUN output
too SCRIPT inspect RUN output | rich -m
too SCRIPT inspect RUN output | rich --json
too SCRIPT inspect RUN output | jq '.'
too SCRIPT inspect RUN output --json
```

Text is emitted without interpretation; structured results are JSON. JSON
renderers require structured output or text that itself contains valid JSON.
The view does not perform presentation, colorization, wrapping, or truncation.

## Baseline Behavior

Before this feature, `inspect` supports `RUN tree`, `STEP call`, and canonical
field Pointers such as `RUN/output/local/value`. Pointer JSON returns raw stored
data without dereferencing it. `RunStore.resolve_local()` already resolves
nested references and validates their types; this feature reuses that resolver.

## Scope And Design

Register `output` as a terminal projector for whole Runs alongside `tree`.
Step, Thread, Control, collection, and field subjects reject it using existing
allowed-view errors. Keep Pointer grammar and existing views unchanged. Do not
add commands, formatting flags, aliases, latest-Run selection, Run-ID prefixes,
or implicit script selection.

Within the existing inspection read transaction, resolve `run.output.local`
and serialize its `value` with `local_to_protocol_data()`. Omit binding, type,
dimension, and synthetic envelopes. Preserve every nested field, list element,
Part, and scalar type in JSON. This resolved JSON can differ from raw
`RUN/output/local/value --json` when the stored result contains references.

If the Run has no output, fail with exit code 1 and its ID and status, without
writing a result to stdout. Present null and empty values succeed. Read any
present output regardless of Run status; do not wait or infer a last-Step
result. Missing Runs, broken references, cycles, and validation errors retain
existing inspection error handling.

## Output Contract

| Resolved value | Default / `--human` | `--json` |
| --- | --- | --- |
| Text | Original text | JSON string |
| Part / Part[] containing TextPart | Concatenated TextPart bodies in order | Complete serialized value |
| Empty Part array | Empty body | Empty JSON array |
| Other values, including nontext Parts | Complete indented JSON | Complete serialized value |

Preserve text whitespace, tabs, Markdown, and code fences without trimming or
rendering. Add a final newline only when nonempty text lacks one; empty text
emits nothing. Textual Parts may omit reasoning and nontext content from their
default view; JSON always preserves the full value. Use exact Part type
compatibility rather than a name suffix, so authored structs such as
`ReportPart` remain structured data. Never parse strings to infer JSON types.

Output is identical in terminals and pipes. Do not add ANSI codes, metadata
headings, footers, width-based wrapping, or truncation, even under `FORCE_COLOR`.
The existing `--human` and `--json` flags remain mutually exclusive. Raw Pointer
inspection remains available for wrappers and provenance.

## Touchpoints

- `src/toolang/cli/toolang/commands/inspect.py`: projector registration,
  resolution, content extraction, plain output, and registry-derived help.
- `tests/unit/cli/test_inspect_subject_navigation.py`: grammar and registry.
- `tests/unit/cli/test_inspect_rendering.py`: exact Part classification and
  identical plain output on terminals and pipes.
- `tests/integration/cli/test_local_core_commands.py`: offline acceptance.
- `docs/api.md`: syntax, output contract, resolved JSON, and errors.

No persistence, schema, execution, API, plugin, or entry-point changes are
required. Historical script selection and execution-store opening are unchanged.

## Acceptance Tests

1. Text and textual Parts emit complete unrendered content. Assert exact
   whitespace, Markdown, code fences, Unicode, and long-content preservation.
2. Empty text emits nothing; whitespace-only text retains its whitespace.
   JSON round-trips null, empty containers, scalars, Parts, and nested values.
3. Terminal and piped output contain no added ANSI codes, including under
   forced-color environments. Narrow terminal widths do not alter content.
4. Nontext Parts and custom struct arrays, including empty `ReportPart[]`,
   emit complete JSON instead of crashing or disappearing.
5. Direct, chained, and nested references resolve. Missing targets, cycles,
   and type errors fail without partial stdout; raw Pointer JSON is unchanged.
6. Missing output reports Run/status context. Missing history creates no store.
   Script inspection reads history without running the script.
7. Only whole Runs accept `output`; existing format conflicts still fail.
   Help lists the view without new options; Pointer, tree, and call views stay
   compatible. The default lint, format, type, and full offline checks pass.

## Risks And Open Questions

Resolving large outputs can be expensive; retain the current unbounded
inspection model. There are no remaining product questions.

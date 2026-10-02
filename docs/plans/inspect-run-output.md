# Inspect Run Output

## Status

Approved by the human on 2026-10-02: add only the `output` view, automatically
select Rich presentation from the result type, and retain existing format flags.
Implementation is authorized.

## Goal And Success Criteria

Read a Run result without knowing its `Output` and `Local` storage wrappers or
piping it to an external renderer. `too` is an alias for `toolang`.

```sh
too SCRIPT inspect RUN output
too SCRIPT inspect RUN output --json
```

The default view renders the result with Rich. The existing `--json` flag
returns complete resolved data suitable for further processing.

## Baseline Behavior

Before this feature, `inspect` supports `RUN tree`, `STEP call`, and canonical
field Pointers such as `RUN/output/local/value`. Pointer JSON returns raw stored
data without dereferencing it. `RunStore.resolve_local()` already resolves
nested references and validates their types; this feature reuses that resolver.

## Scope And Design

Register `output` as a terminal projector for whole Runs alongside `tree`.
Step, Thread, Control, collection, and field subjects reject it using existing
allowed-view errors. Keep Pointer grammar and all existing views unchanged.
Do not introduce commands, formatting flags, aliases, latest-Run selection,
Run-ID prefixes, or implicit script selection.

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

## Presentation

| Resolved value | Default / `--human` | `--json` |
| --- | --- | --- |
| Text | Rich Markdown | Exact JSON string |
| Part / Part[] containing TextPart | Rich Markdown from concatenated response text, trimmed as in the existing Parts response policy | Complete serialized value |
| Empty Part array | Empty body | Empty JSON array |
| Other values, including nontext Parts | Rich JSON | Complete serialized value |

Choose the renderer from the runtime type, not by parsing strings to guess
whether they contain JSON. Use exact Part type compatibility rather than a
name suffix; authored structs such as `ReportPart` remain structured data.
Empty or whitespace-only TextPart content produces an empty Markdown body.
Textual Parts may omit reasoning and nontext content from their human view;
JSON always preserves the full value. Nontext Parts fall back to JSON.

Rich handles Markdown layout and terminal JSON highlighting. JSON is complete
and does not wrap at the terminal width. Redirected JSON remains valid and
uncolored even when `FORCE_COLOR` is set. Markdown remains a presentation view
when redirected; callers needing exact data use `--json` (and `jq -r` for raw
text). Do not emit metadata headings or footers.

The existing `--human` and `--json` flags remain mutually exclusive. No
`--markdown` option is added. Raw Pointer inspection remains available for
provenance, wrappers, and debugging.

## Touchpoints

- `src/toolang/cli/toolang/commands/inspect.py`: projector registration,
  resolution, type-directed Rich rendering, and registry-derived help.
- `tests/unit/cli/test_inspect_subject_navigation.py`: grammar and registry.
- `tests/unit/cli/test_inspect_rendering.py`: exact Part classification and
  terminal versus redirected JSON.
- `tests/integration/cli/test_local_core_commands.py`: offline acceptance.
- `docs/api.md`: syntax, presentation, resolved JSON, and errors.

No persistence, schema, execution, API, plugin, or entry-point changes are
required. Historical script selection and execution-store opening are unchanged.

## Acceptance Tests

1. Text and textual Parts render as Markdown without an extra format flag.
   Empty textual content renders an empty body, not a serialized Part wrapper.
2. JSON round-trips Unicode, whitespace, null, empty containers, scalars, Parts,
   and long nested values. Terminal JSON is highlighted; piped JSON contains no
   ANSI codes, including under forced-color environments.
3. Nontext Parts and custom struct arrays, including empty `ReportPart[]`,
   render complete JSON instead of crashing or disappearing.
4. Direct, chained, and nested references resolve. Missing targets, cycles,
   and type errors fail without partial stdout; raw Pointer JSON is unchanged.
5. Missing output reports Run/status context. Missing history does not create
   a store. Script inspection reads history without running the script.
6. Only whole Runs accept `output`; existing format conflicts still fail.
   Help lists the new view without adding format options. Existing Pointer,
   `tree`, and `call` views remain compatible.
7. The repository's default lint, format, type, and full offline checks pass.

## Risks And Open Questions

Resolving large outputs can be expensive; retain the current unbounded
inspection model. Markdown presentation changes visual whitespace, so document
JSON as the exact-data view. There are no remaining product questions.

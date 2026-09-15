# Compact Resource Directive Groups

## Status and Goal

Approved for implementation by the user on 2026-09-14.

Format each contiguous runnable resource-directive section as compact, stable
key groups. Success means directives with the same key are adjacent, key groups
follow first-appearance order, each key preserves its authored operation order,
and no blank lines remain between directive lines.

Confirmed direction (2026-09-14): use stable grouping rather than preserving the
full interleaved source order. For example:

```too
agic work:
  models = first
  tools = fs/*
  models += second
  skills = org/review
  tools += shell/*
```

formats as:

```too
agic work:
  models = first
  models += second
  tools = fs/*
  tools += shell/*
  skills = org/review
```

## Verified Baseline

At `025503be`, `format_source()` records each directive line with a
`("directive", key)` spacing group. `_normalize_blank_lines()` removes blanks
between adjacent same-key directives and inserts one blank between different
keys, but never reorders lines. The current convention and approved source
command plan explicitly require that behavior.

Directive application is independent by key:

- lowering stores directives in source order;
- validation filters by directive name;
- execution selects the directives for one resource name before applying its
  ordered `=`, `+=`, and `-=` operations;
- model and resource query results return to immutable base order.

Therefore, moving one key group relative to another does not change the operation
order within either resource collection. Reordering directives with the same key
would change semantics and is forbidden.

## Formatting Contract

Within each `agic` or `flow` body, identify every maximal contiguous directive
section. Blank lines between directives are part of that section and do not form
a barrier. For each section:

1. collect directive lines by key;
2. order key groups by the first occurrence of each key in that section;
3. preserve the exact relative order of all directives within each key,
   including `=`, `+=`, and `-=` operations;
4. emit every directive line contiguously, with no blank lines between same-key
   or different-key groups.

A plain or documentation comment ends the current directive section. Group a
later directive section independently and do not move directives across the
comment, so documentation attachment and detachment remain unchanged. Any other
non-directive syntax ends the runnable's directive region under the grammar;
never move directives across context/instruct settings, messages, flow
statements, literal text, or runnable boundaries.

Examples:

```too
agic work:
  tools = fs/*
  models = first
  tools += shell/*
  models -= slow
```

becomes:

```too
agic work:
  tools = fs/*
  tools += shell/*
  models = first
  models -= slow
```

A comment remains a barrier:

```too
agic work:
  models = first
  tools = fs/*
  # Runtime additions.
  models += fallback
  tools += shell/*
```

becomes two independently grouped compact sections and does not move either
post-comment directive above the comment.

Apply the same output to in-place formatting, `--check`, `--stdout`, and
`--highlight`, all of which share `format_source()`. Preserve directive text
normalization, query/value order, inline comments, indentation, final newline,
semantic lowering, and idempotence.

## Scope and Touchpoints

This definition adds only this plan. A later implementation is limited to:

- `src/toolang/lang/format.py`: stably reorder directive lines within each
  contiguous, comment-free directive section before blank-line normalization,
  then keep all directive groups compact.
- `tests/unit/lang/test_format_contract.py`: replace the current no-reordering
  expectation with exact stable-grouping cases, comment/non-directive barriers,
  inline comments, semantic preservation, and idempotence.
- `tests/unit/lang/test_program_format.py`: update existing exact formatter
  fixtures so different directive keys no longer require blank separators.
- `tests/integration/cli/test_source_commands.py`: verify the shared `fmt`
  surfaces expose the same grouped result and `--check` recognizes it.
- `docs/toolang-authoring-conventions.md`: state stable first-seen key grouping,
  compact group adjacency, per-key operation-order preservation, and barriers.
- `docs/plans/source-developer-commands.md`: historical approved context remains
  unchanged; do not rewrite it.
- `CHANGELOG.md`: record the changed resource-directive formatting convention.

No parser, AST schema, directive evaluator, query semantics, command flags,
grammar, or runtime behavior changes.

## Acceptance Tests

1. Interleaved directive keys are grouped stably: groups follow first appearance,
   directives within each group preserve authored order, and no directive lines
   have blank separators.
2. Existing blank lines between directives are removed and do not split a
   section; plain and documentation comments split independently grouped
   sections, and documentation comment ownership is unchanged.
3. Both `agic` and `flow` bodies follow the rule for every directive key
   recognized by the current grammar and mixed `=`, `+=`, and `-=` operators
   accepted for that key. Keyword-looking prose remains literal text.
4. Inline comments remain attached to their original directive, source values
   and query ordering are unchanged, and parsed semantics before and after
   formatting are equal modulo spans/source-generated names.
5. Formatting is idempotent at the default and an overridden tab size. In-place,
   `--stdout`, `--highlight`, and `--check` agree on the canonical result.
6. The convention documentation and changelog describe the new rule, and default
   verification passes: Ruff lint/format, `ty`, and offline pytest.

## Risks and Open Questions

- Stable grouping intentionally changes textual source order across different
  directive keys. Independence by key makes runtime behavior unchanged, but
  diffs can be larger on first formatting.
- Plain and documentation comments are barriers; moving directives across them
  could change documentation ownership or invent comment attachment semantics.
- A later directive section does not merge with an earlier one across any
  non-directive line.

No unresolved implementation choices.

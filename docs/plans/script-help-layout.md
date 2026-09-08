# Script Help Layout

## Status and Goal

Approved for implementation on 2026-09-08, following the merged Script CLI
Input Usage change (#510). Common options are accepted before or after the
runnable, using the recommended policy below.

Make Script root help describe runnable selection and common options, and make
runnable help describe execution and signature arguments in consistent rows.
Supersede only the help layout and common-option placement sections of
[Script CLI Input Usage](script-cli-input-usage.md).

## Root Help

```text
Usage: too examples/deep_search.too [OPTIONS] RUNNABLE

Run runnables from examples/deep_search.too.

Runnables
  agic:expand_queries  Agic expand_queries.
  agic:search_web      Search the web for one query and return a compact evidence bundle.
  flow:research       Flow research.
```

Use `Run runnables from SCRIPT.` for the root description, substituting the
invoked script path. Keep qualified labels, declaration order, visibility rules,
and authored descriptions. When a description is absent or blank, use
`Agic NAME.` or `Flow NAME.` Remove the trailing `Use RUNNABLE --help ...` hint.

Show all common Script options in the root Options panel, in this order:
`--allow`, `--limit`, `--model`, `--sandbox`, `--out` / `-o`, `--quiet` / `-q`,
`--dev`, and `--help`. Use the same order in runnable help; `--dev` is the last
execution option, immediately before `--help`. This root is `too SCRIPT`, not the
agent-management `too` root; global `--root` and `--version` are outside scope.

### Option Placement

Accept common options on either side of RUNNABLE, before input, matching root
synopsis and preserving existing runnable-level invocations:

```text
too app.too --model MODEL research INPUT
too app.too research --model MODEL INPUT
```

Reuse the same Annotated option declarations at both levels. An explicitly
supplied runnable-level scalar overrides its root value; an absent child option
retains its root value.
Concatenate repeated `--allow` and `--limit` values in command-line order.
`--quiet` at either level enables quiet mode. `--help` describes the level at
which it appears. Preserve qualified selectors after root options.

## Runnable Description

Keep signature-controlled synopsis: `[ARGS]` only for named parameters, and
one required `INPUT` only when the signature accepts primary input.

Use `Run agic NAME.` or `Run flow NAME.` without an authored description.
Otherwise use `Run agic NAME - DESCRIPTION` or
`Run flow NAME - DESCRIPTION`, preserving authored paragraphs and formatting.

For flows, continue naturally with `This flow executes the following steps:`
and the existing indented outline before Arguments. Retain statement docs,
nesting, ordinals, literal text, and truncation. Agics have no step section.

Render all outline text in normal terminal style, including ordinals, authored
statement descriptions, and generated operation descriptions. Do not dim or
emphasize individual lines. Separate consecutive sibling steps with one blank
line, including within nested blocks. Keep a step's authored description and
operation description adjacent, and retain indentation for nested steps.

```text
  [0] Set value to topic

  [1] Expand the research question into diverse search queries
      Scatter into 6 items with expand_queries

  [2] Search the web for each query
      Map each item with search_web, up to 4 at once
```

## Arguments Panel

Use Typer's default Arguments rendering. Configure the name/metavar, authored
type, requiredness, and help through Annotated argument declarations. Keep
named parameters in signature order; INPUT is last. Authored type labels are
uppercase. This example illustrates the information, not a custom table layout:

```text
*  topic=ARGUMENT  TEXT [required]    Topic description.
   abc=ARGUMENT    BOOLEAN           Argument description.
*  INPUT           PART[] [required]  Input description.
```

Let Typer determine columns, widths, colors, wrapping, and required-marker
placement. Follow native boolean-metavar visibility as well: the pinned Typer
renderer suppresses Boolean type labels. Do not patch the renderer to force
the illustrative column positions or labels.

`ARGUMENT` is the literal placeholder in named metavars, not a type or a new
argument syntax. Actual invocation still uses `topic=value`. Optional rows
have neither `*` nor `[required]`; do not insert `Optional.` as a description.
Use existing parameter doc metadata when present; otherwise leave named
descriptions blank. Do not add language syntax for parameter documentation.

The INPUT argument's help includes its capture instructions: `Text after
arguments and options; -- explicitly starts text; - reads stdin to EOF.
Omit text to read piped or redirected stdin.` Preserve an authored
input description before these instructions when available. No explanatory
paragraph follows the argument rows. Omit INPUT and its instructions when the
signature forbids primary input; omit the panel for empty signatures.

## Typer Integration

Use the repository's existing command-factory and Annotated declaration
patterns for both Options and Arguments. Configure `metavar`, `help`, aliases,
types, requiredness, and panel assignment through `typer.Option` and
`typer.Argument` metadata. Reuse existing Annotated aliases where they match.

Common options use normal declarations. The variable part of runnable
signatures generates equivalent annotated parameter metadata and passes it
through Typer's command/parameter construction. Preserve Script's input
collector and downstream coercion; help metadata must not introduce early
conversion of Content or named assignment values.

Remove the custom Arguments table and separate capture notes. Do not build,
copy, or hardcode Options/Arguments rows, columns, styles, or required markers.
Use Typer's default parameter panels. Keep only local adaptations needed for
the approved synopsis, section ordering, runnable descriptions/listing, and
flow outline. Do not add a generic rendering or dynamic-command framework.

## Scope and Verification

Likely implementation files:

- `src/toolang/cli/toolang/commands/script.py`: descriptions, Annotated parameter
  declarations/dynamic metadata, native parameter panels, and option placement.
- `tests/unit/cli/test_script_command.py`: update help expectations and cover
  common options and their placement.
- `docs/api.md` and `docs/call-input.md`: synchronize the current CLI contract.

Keep this definition in `docs/plans/script-help-layout.md`. Preserve the
historical plan. No runtime, shared Content parser, coercion, persistence,
HTTP, dependency, global help-theme, or example-content changes. Input capture
semantics and output destinations remain unchanged. Width/theme redesign and
new missing-input diagnostics are outside this definition.

Acceptance checks:

1. Root help has an imperative description, complete common options, authored
   or named fallback descriptions, qualified labels, and no trailing hint.
   Both root and runnable options end with `--dev`, then `--help`.
2. Agic and flow help use matching `Run KIND NAME` descriptions; only flows
   include the natural step introduction and outline. All outline text uses
   normal style; consecutive sibling steps have one blank line between them,
   while description/operation pairs remain adjacent and nesting stays aligned.
3. Mixed, primary-only, named-only, optional-only, and empty signatures retain
   correct synopsis and panel visibility. Annotated metadata supplies named
   metavars, type labels, requiredness, and descriptions; INPUT comes last with
   capture guidance in its help. Options and Arguments retain Typer's native
   presentation, including Boolean and required-marker conventions.
4. Verify literal array brackets, absent parameter docs, narrow/wide terminals,
   and monochrome/color rendering without whitespace snapshots.
5. Verify option placement, scalar precedence,
   repeated values, quiet behavior, qualified selectors, and early help without
   stdin reads or execution. Existing input-boundary tests remain green.
6. Run Ruff lint/format, type checks, and the default offline pytest suite.

The main risk is divergence between root and runnable options. Keep help and
accepted syntax consistent without duplicating option definitions. No unresolved
design questions remain.

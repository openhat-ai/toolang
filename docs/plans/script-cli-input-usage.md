# Script CLI Input Usage

## Status

Approved for implementation on 2026-09-08, including the compact help layout.
Work type: feature implementation.
This plan supersedes only the Script command-line capture and help rules in
[Call Input](call-input.md) and the output option's name in
[Script Result Saving](script-result-saving.md).

## Goal and Success Criteria

Make generated runnable help describe the accepted command line accurately:
named inputs are called Arguments, primary input is called Input, and CLI
flags are Options. List named arguments and primary input in one Arguments
panel, after the runnable description and any flow steps. Accept direct line
text or stdin and reject the removed Script `---` marker with a migration
diagnostic.

## Baseline Behavior (9e58043c)

- `script.py` uses the same argument metavar for synopsis syntax and the Rich
  Arguments panel. The primary row therefore repeats `-- INPUT... | - | ---`.
- Required named inputs appear as `count=Number`; optional ones appear as
  `[enabled=Boolean]`. Assignment order is unrestricted before primary input.
- `--` captures remaining shell words; `-` reads stdin through EOF; `---`
  reads a fenced block from stdin. Omitting a marker reads non-TTY stdin.
- A declared primary input is required. Language validation rejects optional
  primary declarations; explicit signatures can omit primary input entirely.
- An explicit empty stream supplies empty input. Empty implicit stdin means
  no input. Missing required inputs show runnable help and exit with status 2.

## Decisions

### Signature Controls Input Visibility

Use the resolved signature (`runnable.input is not None`), including language
defaults, to decide whether to show `INPUT`. For agics and flows, omitted
signatures supply implicit `Part[]` input; `demo(_)` and `demo(_: Text)` accept
explicit primary input. Empty and named-only signatures forbid it.

Primary input is either required or forbidden; optional primary declarations
are invalid. Show required `INPUT` once in the synopsis, without brackets or
ellipsis, or omit it entirely. It represents one logical input, which may span
multiple shell words or come from stdin. Runnables without primary input have
no INPUT row or line/stdin instructions and require only their arguments.

### Script Capture

For runnables that accept primary input, support two forms at the
`too SCRIPT RUNNABLE` command-line boundary:

| Form | Invocation suffix | Behavior |
| --- | --- | --- |
| Line | `TEXT...` or `-- TEXT...` | Require nonempty text; preserve existing word joining, quoted newlines, and include boundaries. |
| Stream | `-` | Read stdin through EOF, including an explicitly empty stream. |

Before line input begins, parse command options and declared `name=VALUE`
arguments. The first ordinary operand starts line input; all following tokens
are content, including assignments and option-looking words. `--` explicitly
starts line input, allowing content to begin with an option-looking word or
`name=value`. Unknown assignments in the header remain usage errors; use `--`
to pass them as content. Shell quoting alone does not change token roles.

Omitting line input and `-` continues to read non-TTY stdin. With TTY stdin,
omission does not start interactive capture. Existing missing-input handling
and downstream type validation remain in force. `-` selects stream capture
only before line input begins and must be the final token. All command options
and arguments must precede input; after line input begins, even `--help` is
content rather than an option.

Recognize a standalone `---` operand before the line boundary and fail with
CLI usage status 2, before reading stdin or starting a run:

```text
fenced input marker '---' is not supported in script mode; use '-' to read stream input from stdin
```

Use this diagnostic whether `---` ends the header or is followed by more
operands, including a later `--`. Do not report an unknown option or an
unclosed fence.

Marker recognition respects argument roles: `---` after line input starts,
within a declared assignment such as `name=---`, or consumed as an option value
is data. Other unknown options in the header retain their usual errors. A
`---` line in stream content does not end the stream. Chat runnable calls,
authored Content, and prompt calls inside Script input retain shared fenced
syntax.

### Synopsis

```text
Usage: too examples/deep_search.too research [OPTIONS] INPUT
Usage: too app.too demo [OPTIONS] [ARGS] INPUT
Usage: too app.too named [OPTIONS] [ARGS]
Usage: too app.too empty [OPTIONS]
```

Include `[ARGS]` exactly once when named parameters exist, without an ellipsis.
The Arguments panel supplies their assignment syntax and requiredness; the
category's brackets do not make required arguments optional. `INPUT` is one
required logical value. Its source forms belong in Arguments, not the synopsis.

### Description and Flow Steps

Immediately after usage, show `name - description`. Use the authored doc
comment, preserving its paragraphs and existing Rich formatting. If absent or
blank, use `An agic.` or `A flow.`; do not infer a description from runnable
content. Show the same description in the top-level runnable listing, without
repeating the name in its description column.

For flows, continue the description with `Flow steps:` and an indented outline,
before Arguments and Options. Do not put the outline in a panel. Preserve
statement docs, ordinal alignment, repeat-body indentation, literal bracket
text, and per-line truncation at terminal width. Do not expand called runnables.
The empty-flow placeholder remains `No statements.`

### Arguments and Options

Show Arguments whenever named parameters or primary input exist. List named
parameters in declaration order as `name=TYPE`: preserve name casing and
uppercase the resolved type (`topic=TEXT`, `items=PART[]`, `enabled=BOOLEAN`).
Retain required indicators and annotations; optional arguments say `Optional.`
There is no separate type column or redundant `Use name=VALUE.` text. Do not
invent parameter descriptions or defaults absent from declaration metadata.

Append one `INPUT` row last, with its uppercase type and required annotation.
A named parameter called `input` remains a distinct assignment row. Include
concise notes in Arguments:

- Named assignments may appear in any order before input; omit `before input`
  for named-only signatures.
- `TEXT...` supplies input; `-- TEXT...` starts it explicitly.
- `-` reads stdin through EOF; omitted command-line text reads piped or
  redirected stdin. The required input may therefore be supplied outside argv.

Omit capture notes when primary input is forbidden. An empty signature omits
Arguments altogether. Options follows Arguments. Result output uses the option
below; other flags retain their existing names and behavior.
No separate Input or Flow outline panel remains. Both explicit help and
missing-input help use this layout, including in monochrome and ANSI color.

### Script Top-Level Help

```text
Usage: too app.too [OPTIONS] RUNNABLE

app.too - Run an agic or flow.
```

Show Runnables before Options. Label public runnables `agic:NAME` or `flow:NAME`,
with their authored or fallback descriptions; omit defaults and generated
internal agics. Qualified labels and bare names are valid selectors. Do not
expand flow steps in the listing. Wrap long labels to retain their full names
and leave descriptions visible. End with `Use RUNNABLE --help for its
arguments and input.` Do not put generic `[ARGS]...` after RUNNABLE: argument
and input requirements depend on the selected signature.

### Result Output

Use `--out PATH` with short form `-o PATH` for result output. Both forms select
the same destination: `-` writes the serialized result to stdout, and a file
path writes it atomically. Omitting the option leaves the result stored without
copying it to stdout. The old `--save` flag is removed and is a usage error.

## Scope and Implementation Touchpoints

- `src/toolang/cli/toolang/commands/script.py`: keep the dynamic command
  factory and hidden collector; generate grouped synopsis pieces independently
  from per-argument panel metavars; render Arguments, descriptions, and flow
  steps locally according to the resolved signature; align script top-level
  help; recognize implicit line input and the removed marker with option-value
  and boundary awareness; expose `--out` / `-o` for the existing output
  destination.
  Stop option/argument parsing once line input starts. Remove the Script
  fenced-capture call and its unused imports, retaining diagnostic recognition.
- `tests/unit/cli/test_script_command.py`: replace fenced-stdin acceptance
  expectations and add parsing, output-flag, and generated-help coverage.
- `tests/integration/cli/test_script_local.py`: exercise stdout and file output
  through the renamed long and short options.
- `tests/integration/up/test_docker_sandbox_lifecycle.py`: align the Script
  invocation with the renamed output option; Docker tests stay opt-in.
- `docs/call-input.md`: replace Script fenced capture with stream examples
  and a migration note; qualify shared diagnostics by surface.
- `docs/input-syntax.md`: distinguish the two Script command-line forms from
  the three shared Chat/prompt forms.
- `docs/api.md`: update Script synopsis, input rules, and help documentation.
- `docs/execution-presentation.md`: update result-output option names.

Keep implementation within these files. No language AST, shared capture
parser, runtime, transport, dependency, global help-renderer,
or named-value coercion changes. Historical approved plans remain historical;
this plan records the narrow supersession.

## Acceptance Tests

1. `research --help` and a missing-input invocation show required `INPUT` once
   in the synopsis and an uppercase `TEXT` type in Arguments. No `[INPUT...]`,
   capture alternatives, or separate Input panel appears.
2. Cover mixed, named-only, primary-only, empty, and implicit signatures for
   both agics and flows. Check `[ARGS]` visibility independently of `INPUT`;
   uppercase scalar/array types, exact name casing, required/optional status,
   declaration order, and the final INPUT row. One optional named parameter
   still shows Arguments. Empty signatures hide it. Named-only help contains
   no line/stdin instructions.
3. Explicit help exits 0. Missing required named or primary input still shows
   help and exits 2 without starting a run. Omitted optional named input works.
   With empty stdin and satisfied arguments, a runnable whose signature forbids
   primary input reaches the run callback without missing-input help.
4. Named assignments bind in either order before capture. The first ordinary
   operand starts input; following assignments, `--help`, and markers remain
   content. Unknown assignments before input fail; `--` permits assignment-like
   and option-like leading content. Missing/duplicate arguments remain errors.
5. Line input with or without `--` and explicit/implicit stream input preserve
   normalization; explicit empty `-` remains distinct from absent implicit
   input. Test quoted newlines, includes, empty line rejection, TTY omission,
   and non-TTY stdin.
6. Standalone `---` exits 2 with the exact migration diagnostic, regardless of
   stdin contents or following operands. A stdin double fails if read; a run
   double fails if invoked. Include `--- -- text` and `--- count=2`.
7. `-- ---`, `text ---`, declared `name=---`, and `---` used as a string option
   value remain data at collection. A `---` line under stream capture is
   preserved through EOF. `----` remains an unknown option in the header.
8. Header `-` followed by another token fails before reading stdin. Inside
   line content, `-` is literal. Options before input retain their existing
   behavior, including their values and explicit help.
9. Verify both entry-point labels, 80/120-column help, and monochrome/ANSI
   rendering. Check description prefixes, authored docs and kind fallbacks,
   unboxed flow steps before Arguments, nested outline alignment and truncation,
   and top-level Runnables before Options with kind-prefixed labels and
   per-runnable help guidance. Verify that displayed qualified labels can run.
   Long runnable names wrap without hiding descriptions in narrow terminals.
   Assert semantic content and boundaries rather than whitespace snapshots.
10. Help shows `--out`, `-o`, and `PATH`, without `--save`. Both aliases reach
    the existing output destination handling, including `-` and marker-like
    values. Verify stdout/file output and early rejection of `--save`.
11. Existing Chat and language fenced-input tests remain green. Updated docs
    use valid examples and links. Run the default offline verification:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

## Risks and Open Questions

- Removing Script fenced capture intentionally breaks existing invocations.
  Migration replaces the opening CLI marker with `-` and removes the closing
  fence from stdin; simply replacing the marker would retain that fence as
  content. Heredocs continue to work with `-` and their shell EOF delimiter.
- The synopsis summarizes categories; it does not encode each argument's
  requiredness. Arguments must label required values clearly. Required INPUT
  denotes one logical value; its stdin source and multi-word line form must
  remain clear in the capture notes.
- Direct line input introduces an implicit boundary: options and assignments
  after its first word become content. Help must make ordering clear; callers
  use `--` when leading content could be mistaken for an option or argument.
- Typer/Rich share argument metadata between synopsis and panels. Keep the
  separation local to Script help arguments and verify the rendered result.

No unresolved design questions.

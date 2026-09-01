# Define Call Input

## Status

Approved for implementation on 2026-09-01; amended to prohibit nested prompt
calls.

## Goal

Define one call-input model for text supplied to runnable and prompt calls. The
model uses the same public vocabulary across Script, Chat, authored runnable
bodies, and prompt calls in runnable input, while leaving each surface
responsible for locating the call header and the end of its current input.

The user-facing summary is:

> Call input supports line, stream, and fenced forms. Line input ends at EOL,
> stream input ends at EOS, and fenced input ends at its closing fence.

## Success Criteria

- `Call Input` is the shared concept for primary text attached to runnable and
  prompt calls.
- The only explicit forms are `line`, `stream`, and `fenced`.
- The forms have identical capture semantics wherever their markers are
  available.
- Arguments precede the input marker and use `name=value` syntax.
- Prompt calls expand to text before the complete runnable input is parsed as
  Content.
- Script, Chat, authored-body, and runnable-contained prompt boundaries are
  explicit and covered by offline tests.
- The old `inline`, `tail`, and backtick-fenced prompt-input syntax has no
  compatibility alias.

## Verified Current Behavior

The current implementation is prompt-specific. `docs/input-syntax.md`,
`docs/plans/chat-input-namespaces.md`, and `toolang.lang.input` describe
`inline`, `tail`, and `fenced` prompt scopes. `--` captures to the current line,
`-` captures the remaining Content, and a backtick fence captures a bounded
block. Prompt expansion currently resolves recursively to `Part[]`.

Script runnable commands separately collect primary command arguments, use a
lone `-` to read standard input, and treat `--` as the command-line option
separator. Chat and execution then parse the collected runnable input as
Content. These independent paths do not yet expose one call-input vocabulary.

## Vocabulary

```text
CallInputForm = line | stream | fenced
```

`Call Input` names the value supplied to a call. A form names how an explicit
call header captures that value:

| Form | Marker | Capture boundary |
| --- | --- | --- |
| line | `--` | end of the current logical line (EOL) |
| stream | `-` | end of the current input stream (EOS) |
| fenced | `---` | a closing `---` on its own logical line |

`inline`, `same-line`, `tail`, and `remainder` are not aliases. In particular,
`inline` remains available for the `Form` cap and is not reused here.

No attached primary input is represented by the absence of a form. `none` is
not a `CallInputForm` value.

## Scope

In scope:

- the three forms and their exact capture rules;
- argument and marker boundaries;
- Script runnable calls;
- Chat runnable calls, including a one-run runnable override;
- prompt calls inside `task`, `chore`, and `agic` bodies;
- prompt calls contained in runnable Call Input;
- rejection of prompt calls inside prompt input or prompt results;
- text-first prompt composition and the final Content parse;
- diagnostics, parser vocabulary, documentation, and offline tests.

Out of scope:

- changing prompt declaration syntax or placeholder validation;
- typed placeholders such as `{{_:Json}}`;
- adding a public prompt return type;
- `with flow owner/repository/path`;
- `recall`, local assignment, `hands`, `handoffs`, `MatchUnion`, or `Stuff`;
- changing runnable selection, execution, or submission semantics beyond call
  input capture and prompt composition.

## Common Syntax

An explicit call header has zero or more named arguments followed by at most one
input marker:

```text
CallHeader = Target (SP Argument)* (SP InputMarker)?
Argument = Name "=" Value
InputMarker = "---" | "--" | "-"
```

The target syntax belongs to the enclosing surface. For example, `$greeting`
is a prompt target and a Script runnable subcommand is a runnable target.

Argument values use the existing POSIX shell-like word rules: quotes and
backslash can preserve whitespace or marker text, and no shell expansion is
performed. The target definition validates argument names and values. Prompt
placeholder declaration and validation remain outside this plan.

Input markers are recognized only as unquoted, standalone tokens after all
arguments. Recognition is longest first: `---`, then `--`, then `-`. Marker
text inside an argument value or captured content is ordinary text unless it
also satisfies the applicable fenced closing rule.

An explicit call header without a marker has named arguments but no attached
primary Call Input:

```text
$greeting name=Bryan
```

## Line Input

`--` captures the nonempty text after its separating whitespace through EOL.
The separator and marker are excluded from the value.

```text
$greeting name=Bryan -- Additional content
```

An empty line input is invalid. A line form never consumes the next logical
line.

## Stream Input

`-` must terminate the call header. It captures from the next logical line
through the EOS owned by the enclosing surface:

```text
$greeting name=Bryan -
Multiple lines continue to the end of this input.
```

EOS means:

- standard-input EOF for a Script command reading standard input;
- the end of the submitted Chat buffer, never a future message;
- the end of the current authored Content/body or enclosing runnable Call Input
  for a prompt call.

Stream input may be empty. Because it consumes to EOS, no sibling outer text can
follow it within the same input scope.

## Fenced Input

`---` must terminate the call header. It captures following logical lines until
an exact closing `---` line:

```text
$greeting name=Bryan ---
Multiple lines are captured here.
---
```

The opening marker and closing line are excluded. The block may be empty. A
closing line contains exactly three hyphens after Toolang structural indentation
has been removed; leading spaces, trailing spaces, a longer run, or hyphens
embedded in another content line do not close the block.

The enclosing input continues after the closing fence. This makes fenced input
the bounded form for a prompt call within runnable input:

```text
Before.

$greeting name=Bryan ---
Only this block belongs to the prompt.
---

After.
```

At a root Script or Chat runnable boundary, only whitespace may follow the
closing fence because there is no enclosing Content consumer. A missing closing
fence is an error at the opening marker. Outside a terminal call-header marker
or an active fenced capture, `---` remains ordinary Content or Markdown text.
Backtick fences no longer introduce prompt call input.

## Surface Integration

All surfaces normalize their input to the common forms before prompt expansion:

| Surface | Call target | Form integration |
| --- | --- | --- |
| Script command | runnable | `--` introduces remaining command-line words as line input; `-` reads stream input from standard input; `---` reads a fenced block from standard input |
| Chat without an override | selected runnable | the complete submitted buffer is an implicit stream supplied by the surface; no marker is required |
| Chat with a runnable override | overridden runnable | the override line is the call header and may use any explicit form |
| `task`, `chore`, or `agic` body | `$prompt` | the prompt header may use any explicit form within the current Content scope |
| runnable Call Input | `$prompt` | the prompt header may use any explicit form within the enclosing Call Input |

For Script fenced input, `---` is the final command-line token, the block is
read from standard input, and the closing fence must be followed only by
whitespace through EOF. The command-line adapter must recognize this token
before generic option rejection. Script named arguments remain before the
marker.

The Chat override grammar owns the runnable target and its named arguments; this
plan only adds the common primary-input boundary after that header. Existing
ordinary Chat submission without an override is unchanged.

The body integration applies to authored Content, not cap directives or cap
settings. Prompt calls are allowed in runnable input only. Prompt input and
prompt results cannot contain another prompt call; prompt calls after a fenced
closing line are siblings in the enclosing runnable input.

## Prompt Composition

A prompt is a text template. Its call substitutes named placeholders and the
special primary placeholder `{{_}}`, producing Text. The prompt declaration does
not expose or accept a return type.

Prompt expansion follows this pipeline:

```text
raw runnable input
-> locate call-input boundaries
-> expand each runnable-level prompt call as Text
-> combine the complete runnable input Text
-> parse Content once
-> produce Part[] for runnable execution
```

The expanded prompt may be only one fragment of the final input, with outer text
before or after it. Content markers introduced by a prompt are therefore
interpreted according to their position in the final combined text, not parsed
inside an isolated prompt result. A prompt call discovered inside prompt input
or an expanded prompt result is rejected rather than expanded recursively.

This text-first rule replaces the current behavior that independently resolves
each prompt result to `Part[]` before combining it with its surroundings.

## Errors

Parsing rejects:

- more than one input marker in a call header;
- an input marker followed by another argument;
- empty line input;
- non-whitespace text after a root fenced closing line;
- an unclosed fenced input;
- a prompt call nested inside prompt input or a prompt result;
- surface combinations that cannot provide the selected source, such as Script
  stream or fenced input without readable standard input.

Diagnostics identify the form by `line`, `stream`, or `fenced`, name the call
target when known, and point to the opening marker or invalid trailing token.

## Form Lifetime and Compatibility

The form describes capture syntax, not the captured value. Parsers use
`line | stream | fenced` while locating boundaries and reporting errors, then
discard it. `CallInput`, public requests, and durable records store only primary
text presence/content and named arguments.

An absent primary and an explicitly empty primary remain distinct without a
form field: the former has no primary Call Input and the latter has primary text
equal to `""`. Prompt provenance drops the old prompt-only `input_scope` field.
Existing records that still contain it remain readable because record decoding
ignores the obsolete field. No RunStore schema bump or data migration is needed.

This definition supersedes the prompt-input-form sections of
`docs/input-syntax.md` and `docs/plans/chat-input-namespaces.md` when implemented.
Those documents must link to the canonical `docs/call-input.md` user guide
instead of restating the grammar.

## Design Touchpoints

Likely implementation files are:

- `src/toolang/lang/input.py` for common markers, capture boundaries, prompt
  text expansion, and diagnostics;
- `src/toolang/lang/types.py` for shared call-input vocabulary if a public type
  is required;
- `src/toolang/cli/toolang/commands/script.py` for Script source normalization;
- `src/toolang/cli/toolang/commands/chat/input.py` and
  `src/toolang/execution/calls.py` for Chat and runnable integration;
- `src/toolang/execution/records.py` for form-free authored prompt-call facts;
- `docs/call-input.md`, `docs/input-syntax.md`, and affected runnable/prompt
  guides for user documentation;
- focused language, Script, Chat, record-round-trip, and end-to-end tests.

No `tree-sitter-toolang` grammar change is expected. Authored cap bodies already
enter the language layer as raw Content, while Script and Chat are outside the
`.too` grammar. If implementation discovers that the concrete syntax tree, not
the language input parser, currently owns one of these boundaries, that finding
requires a definition amendment before changing the grammar.

## Acceptance Tests

Offline tests cover at least:

1. A prompt call with arguments and line input captures only through EOL.
2. A prompt call with stream input captures through the current authored or
   enclosing EOS, including an empty stream.
3. A fenced prompt call captures an empty or multiline block, ignores embedded
   and non-exact hyphen lines, and allows outer Content after its closing fence.
4. Missing and malformed fences produce source-located diagnostics.
5. Quoted or escaped `-`, `--`, and `---` remain argument values, and marker
   recognition is longest first.
6. A Script runnable call normalizes line, standard-input stream, and fenced
   standard-input sources to the same forms.
7. A Chat runnable override accepts all three forms; ordinary Chat input remains
   an implicit complete-buffer stream.
8. Prompt calls work in `task`, `chore`, and `agic` bodies and when contained in
   runnable Call Input; nested prompt calls are rejected.
9. Prompt output with outer text is combined as Text and parsed once into the
   final `Part[]`.
10. Request and record round trips preserve absent versus explicit empty primary
    input without persisting a Call Input form.
11. The former `inline`, `tail`, and backtick-fenced prompt syntax is rejected or
    treated as ordinary text according to its surrounding grammar.
12. The default offline verification suite remains green.

## Risks

- `-`, `--`, and `---` overlap with command-line conventions. Surface adapters
  must normalize them before generic option handling without changing unrelated
  CLI arguments.
- Fenced input can resemble Markdown thematic breaks. Requiring a terminal
  call-header marker and an active capture prevents standalone Markdown from
  being reclassified.
- Text-first prompt expansion changes when Content markers become structural.
  End-to-end tests must cover indentation, surrounding text, includes, sibling
  prompts, and nested-prompt rejection.
- Removing the old prompt scope must remain backward-readable without turning
  parser-only form vocabulary into durable state.

## Open Questions

None.

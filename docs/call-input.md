# Call Input

`CallInput[T]` is the complete input supplied to a prompt, script invocation,
or runnable. It uses one immutable flat mapping throughout parsing, execution,
HTTP, and persistence. `_` holds primary input; other keys hold arguments.

```python
CallInput[str]({"_": "Review this change.", "count": "2"})
CallInput[Value]({"_": "Review this change.", "count": 2})
RunnableInput: TypeAlias = CallInput[Value]
```

`CallInput[str]` contains unresolved Content sources. Resolution evaluates
Content once and coerces values against the runnable signature. `RunnableInput`
is only an alias for evaluated values, with no separate class or serialization.
Direct-value calls never interpret strings as Content. Prompt expansion binds
textual placeholders using the same flat input shape.

## Terminology

Use these short names in documentation, authored prose, and CLI help:

| Full name | Short name | Binding name |
| --- | --- | --- |
| Primary input | Input | `_` |
| Named input | Argument | The declared parameter name |
| Named inputs | Arguments | The declared parameter names |

CLI synopses use `ARGUMENTS` for named inputs and `INPUT` for primary input. The
**Arguments** help panel lists named assignments first and primary input last.

A parameter is a signature declaration; an argument is a value supplied for a
named parameter. Use the full names when needed to distinguish the two input
roles. These short names do not rename schema fields: Call Input and an API's
`input` container can still refer to the complete input-and-arguments bundle.

## Flat Input Mappings

Locals and APIs that accept an input-value dictionary use sibling keys: `_`
for input and each declared name for its argument. For a runnable with the
signature `demo(_: Text, arg1: Text, arg2: Number)`, the dictionary is:

```json
{
  "_": "Review this change.",
  "arg1": "security",
  "arg2": 2
}
```

The `_toolang/run` and `_toolang/execute` tools accept this flat dictionary in
their `input` field. Omit `_` when the signature forbids primary input, and
omit unsupplied optional arguments. Supplied values must satisfy the target
signature; arguments do not sit inside a nested `arguments` or `named` object.

Runtime locals use the same flat names, with `Local` values carrying type and
provenance metadata. They can also contain other flow bindings and reserved
runtime context. The `_` local can be empty when no input was supplied and can
later hold a flow statement's result; it is not always the original call input.

The constructor accepts one mapping, with no `primary`/`named` compartments.
Omit `_` for absent input; `"_": ""` and empty typed arrays are supplied values.
Primary `null` is invalid. Optional arguments are omitted when not supplied;
argument nullability follows the target signature. Collectors reject duplicate
assignments before constructing a mapping; HTTP rejects duplicate input keys.
Canonical names, required arguments, unknown arguments, and type checks remain
enforced at their input boundaries. Prompt parameter names may contain hyphens;
runnable boundaries retain their stricter identifier rule.

Execution records use `CallInput[Value | TypedRef]`. Each stored entry retains
the self-describing value codec, without an input-only `Local` wrapper. Input
references use `payload/input/_` or `payload/input/argumentName`. Nested paths
follow the value codec: a boxed array item uses `payload/input/items/!/0`.
Outputs use `Output(local=Local(value=..., dim=0), binding="_")`. `Local`
contains only the value and dimension; the enclosing local map or output
binding supplies its name. `binding=None` leaves a result unbound.

This format replaces the old source compartments, resolved compartments, HTTP
`args` sibling, and persisted local arrays. RunStore schema 43 rejects older
stores without modifying them. HTTP clients must send the flat format; no
compatibility adapter or migration is provided.

## Input Forms

Chat runnable overrides and prompt calls can capture primary text in three
forms:

| Form | Marker | Boundary |
| --- | --- | --- |
| line | `--` | end of the current line (EOL) |
| stream | `-` | end of the current input stream (EOS) |
| fenced | `---` | an exact closing `---` line |

Named arguments use `name=value` and precede the marker. On these Content
surfaces, markers are recognized only as unquoted standalone tokens. Quoting
or escaping a marker keeps it in an argument value. Script command headers
support only line and stream input, as described below; shell quoting does not
change a standalone token's role after the shell removes its quotes.

The form is capture syntax, not part of the resulting Call Input. It is used by
the parser for boundaries and diagnostics and is discarded after capture. An
absent primary input and an explicitly empty primary input remain distinct:

```text
CallInput()              -> no primary input
CallInput({"_": ""})       -> explicit empty primary input
CallInput({"_": "text"})   -> nonempty primary input
```

## Line Input

`--` captures the nonempty remainder of the current logical line:

```text
$greeting name=Bryan -- Additional content
```

The marker and separating whitespace are excluded. The next line remains in the
enclosing input. Empty line input is invalid.

## Stream Input

`-` terminates the call header and captures through the current EOS:

```text
$greeting name=Bryan -
Multiple lines continue to the end of this input.
```

EOS is the submitted Chat buffer, Script standard-input EOF, or the end of the
current enclosing Content. Stream input may be empty and cannot be followed by
sibling content in the same scope.

## Fenced Input

`---` terminates the call header and captures until an exact closing `---` line:

```text
$greeting name=Bryan ---
Only this block belongs to the prompt.
---

This text remains outside the prompt call.
```

The fences are excluded and the block may be empty. Leading or trailing spaces,
longer hyphen runs, and hyphens embedded in another line do not close the block.
At a root runnable boundary, only whitespace may follow the closing fence.
Backtick fences do not introduce Call Input.

## Chat Runnable Calls

A runnable override is a runnable call header and accepts every explicit form:

```text
:agic review focus=security -- Review this API
```

```text
:agic review focus=security -
Review this API and its tests.
```

```text
:agic review focus=security ---
Review this API.
Include concurrency risks.
---
```

An ordinary Chat submission without a runnable override remains an implicit
whole-buffer stream. The existing separated override form also remains an
implicit stream:

```text
:agic review focus=security

Review this API.
```

Prompt calls inside the submitted runnable input use the same line, stream, and
fenced syntax.

## Authored Runnable Input

`task`, `chore`, and `agic` bodies are runnable Content surfaces. They can call
prompts with the same three forms. A runnable Call Input supplied through Chat
or Script can do the same. These are prompt calls contained by runnable input,
not prompt calls nested inside another prompt call.

## Script Runnable Calls

Runnable help summarizes the available input categories:

```text
Usage: too app.too demo [OPTIONS] [ARGUMENTS] INPUT
```

`[ARGUMENTS]` appears when the signature declares at least one named parameter.
`INPUT` appears once, without brackets or an ellipsis, when the signature
requires primary input. This includes the implicit `Part[]` input of a runnable
without a signature. Empty and named-only signatures omit it. It denotes one
logical input, which may span multiple shell words or come from stdin.
Required named arguments remain required despite the optional `[ARGUMENTS]` group.

Below usage, `Run KIND NAME.` describes execution. An authored doc comment
changes this to `Run KIND NAME - DESCRIPTION`. Flows continue with
`The flow proceeds as follows:`, a blank line, and an outline aligned with the
description text before the help panels. All outline text uses normal style,
with one blank line between sibling steps; each step's doc and operation
description remain adjacent.

The **Arguments** panel lists named parameters in signature order as
`name=<ARGUMENT>`, without a separate type label. A `*` marks required parameters,
and parameter doc comments appear in the help column. Missing docs use
`Named input, or simply argument` for named parameters and
`Primary input, or simply input` for INPUT. The INPUT row is last when primary
input is accepted, and appends
`- from stdin, -- starts input` to its description. Arguments may be supplied
in any order, interspersed with command options, before input.

| Form | Behavior |
| --- | --- |
| `TEXT...` | The first ordinary operand starts input; the remaining shell words are its text. |
| `-- TEXT...` | Explicitly starts input, including text beginning with an option or assignment. |
| `-` | Reads stdin through EOF, including an empty stream. |
| Omitted | Reads piped or redirected stdin; an empty stream means input is absent. |

When primary input is forbidden, its row and instructions are absent. Empty
signatures omit Arguments entirely. **Options** follows Arguments. Top-level
Script help says `Run runnables from SCRIPT.` and lists **Runnables** before
Options, using `agic:NAME` and `flow:NAME` labels with authored descriptions or
`Agic NAME.` / `Flow NAME.` fallbacks. Both qualified labels and bare names are
valid runnable selectors.

Root and runnable help show the same common options, with `--dev` immediately
before `--help`. Common options may appear on either side of RUNNABLE, before
input. Runnable-level scalar values override root values when explicitly set;
repeated `--allow` and `--limit` values accumulate in command-line order.
`--quiet` at either level enables quiet mode, and `--help` describes that level.

Both line forms accept the same text:

```bash
toolang agent.too review focus=security Review this API
toolang agent.too review focus=security -- Review this API
```

After input starts, all remaining words are content, including `name=value`,
`--help`, `-`, and `---`. Before input starts, an undeclared `name=value`
assignment is an error; use `-- name=value` for literal input. Shell quoting
alone does not make such an assignment literal. Remaining shell words are
joined with spaces, while quoted newlines and include items retain their
Content boundaries. An explicit `--` requires nonempty line input.

The standalone `-` marker must be the final command-line token:

```bash
toolang agent.too review focus=security - < request.md
```

Omitted input does not read an interactive terminal. Missing required input or
arguments displays runnable help with exit status 2; explicit `--help` exits
with status 0. Neither starts a run.

A standalone `---` in the command header is rejected before reading stdin or
starting a run, with this diagnostic:

```text
fenced input marker '---' is not supported in script mode; use '-' to read stream input from stdin
```

Replace the old header marker with `-` and remove the closing fence:

```bash
toolang agent.too review focus=security - <<'EOF'
Review this API.
EOF
```

Stream input continues through EOF, including any `---` lines. `---` remains
literal as a declared argument value, an option value, or part of line input.
Prompt calls inside Script input retain all three Content capture forms.

## Prompt Expansion

A prompt binds named placeholders and the primary `{{_}}` placeholder, then
returns Text. Prompt calls in runnable input expand before the complete runnable
input is parsed as Content:

```text
raw runnable input
-> locate prompt Call Input
-> expand prompt templates to Text
-> combine the complete runnable input Text
-> parse Content once
-> produce Part[]
```

This allows a prompt to produce only one fragment of a larger input. Includes
and other Content markers become structural according to their position in the
final combined text. A runnable Call Input may contain multiple prompt calls,
but prompt input and prompt results cannot contain another prompt call. A fenced
prompt call returns to the runnable input, where sibling prompt calls may follow.

## Errors

Chat and prompt Call Input parsing rejects:

- empty line input;
- `-` or `---` followed by another header token;
- an unclosed fenced input;
- non-whitespace content after a root fenced closing line;
- a prompt call nested inside prompt input or a prompt result.

Script command headers reject empty explicit line input, tokens after the
standalone `-` marker, the standalone `---` marker, and unknown or duplicate
argument assignments. Input resolution still validates Content and coerces
values against the runnable signature.

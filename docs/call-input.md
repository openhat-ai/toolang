# Call Input

Call Input is the unresolved primary text and named arguments supplied to one
call. Runnable calls retain their existing public raw-input name, while prompt
calls use the shared value directly:

```text
CallInput = PrimaryText? + NamedArgument*

RunnableInputRaw : CallInput
```

Runnable resolution later evaluates Content and coerces values against a
runnable signature. A prompt instead binds textual placeholders and expands to
Text. A separate prompt-input type would add no behavior or stored information.

## Input Forms

An explicit call header can capture primary text in three forms:

| Form | Marker | Boundary |
| --- | --- | --- |
| line | `--` | end of the current line (EOL) |
| stream | `-` | end of the current input stream (EOS) |
| fenced | `---` | an exact closing `---` line |

Named arguments use `name=value` and precede the marker. Markers are recognized
only as unquoted standalone tokens. Quoting or escaping a marker keeps it in an
argument value.

The form is capture syntax, not part of the resulting Call Input. It is used by
the parser for boundaries and diagnostics and is discarded after capture. An
absent primary input and an explicitly empty primary input remain distinct:

```text
no Call Input primary       -> no primary input
CallInput(primary="")       -> explicit empty primary input
CallInput(primary="text")   -> nonempty primary input
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

Script named arguments precede the Call Input marker:

```bash
toolang agent.too review focus=security -- Review this API
```

`--` is both the command-line option boundary and the line-input marker. The
remaining shell words are joined with spaces, while quoted newlines and include
items retain their Content boundaries.

`-` reads stream input from standard input:

```bash
toolang agent.too review focus=security - < request.md
```

`---` reads fenced input from standard input:

```bash
toolang agent.too review focus=security --- <<'EOF'
Review this API.
---
EOF
```

Unmarked command-line words are not primary input. Omitted input continues to
read non-interactive standard input as an implicit stream for compatibility.

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

Call Input parsing rejects:

- empty line input;
- `-` or `---` followed by another header token;
- an unclosed fenced input;
- non-whitespace content after a root fenced closing line;
- a prompt call nested inside prompt input or a prompt result;
- Script command-line primary text without `--`, `-`, or `---`.

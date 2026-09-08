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

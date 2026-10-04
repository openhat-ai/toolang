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

Source input and model content are decoded at their boundaries. A native string
supplied to a `Json` parameter or returned by a flow remains a string: `"false"`
does not become Boolean, and `"null"` does not become null. Internal calls bind
existing values without parsing these strings again.

## Terminology

Use these short names in documentation, authored prose, and CLI help:

| Full name | Short name | Binding name |
| --- | --- | --- |
| Primary input | Input | `_` |
| Named input | Argument | The declared parameter name |
| Named inputs | Arguments | The declared parameter names |

CLI synopses use `NAME=VALUE` for named inputs and `INPUT` for primary input. The
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

The `_toolang/run` and `_toolang/exec` tools accept this flat dictionary in
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

HTTP clients use this flat format. Incompatible stores are rejected unchanged;
see [record compatibility](run-step-records.md#persistence).

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

## Runnable Override Calls

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

Use `too run FILE [RUNNABLE]` for a local `.too` file; `too FILE [RUNNABLE]`
remains a shorthand. Omitting the selector executes the module's unnamed entry.
Without an unnamed entry, omitting arguments shows file help. Bare words in the selector position
are runnable names, so default-entry text uses `--`, `-`, or redirected stdin:

```sh
too run app.too -- "Handle this request"
too run app.too topic=demo -- "Handle this request"
too run app.too review -- "Review this request"
```

`too run FILE --help` shows file help; `too run FILE _ --help` shows the
unnamed entry's signature. `_`, `agic:_`, and `flow:_` select that entry explicitly;
`-` remains stdin input. Help does not read stdin or prepare an agent. Explicit `run`
can select a runnable whose name is a CLI command, such as `serve`.
Global `--root` / `-r` overrides are not supported in Script mode.

The command-line collector accepts declared `NAME=VALUE` assignments until
primary input starts. An ordinary operand or `--` begins line input; remaining
shell words become content, including words that look like flags or assignments.
`--` requires nonempty input. Unknown or duplicate assignments before that point
are errors; shell quoting alone does not make an assignment literal.

A final standalone `-` reads stdin through EOF, even when empty. Omitted input
reads piped/redirected stdin but not an interactive terminal; an empty omitted
stream means absent input. Standalone `---` in the command header is rejected
before reading stdin. It remains literal after input starts and remains valid
for prompt capture inside Content. Missing required inputs show runnable help
and exit 2; explicit help exits 0. Neither prepares or starts a run.

Common options can occur before or after RUNNABLE, before input. Scalar
runnable-level values override root values, repeated workspace/allow/limit
options accumulate, and workdir may be specified only once. See
[CLI routing](cli.md) and [script projects](script-projects.md) for the owning
command and filesystem contracts.

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

## Content

```text
InputContent = NonEmptyContent
Content      = ContentItem*
ContentItem  = Text | IncludeRef | PromptCall

evaluate(InputContent | Content) -> Part[]
```

`$` and `@` are special only as the first character of a `Content` line. `:` is
special only in the policy prefix, and `/` is special only when Chat classifies
a complete command. Ordinary Markdown code fences suspend special-line
recognition. Double a leading marker where its single form would be special to
produce literal text:

```text
//help           -> /help
$$review         -> $review
::model gpt-5    -> :model gpt-5
@@README.md      -> @README.md
```

### Includes

```text
IncludeRef  = "@" ResourceRef
ResourceRef = Path | QuotedPath | UploadRef
```

Examples: `@README.md`, `@"path with spaces/image.png"`, and
`@upload:abc123`. An include occupies its complete line and resolves to one
`Part`. Leading whitespace makes it text. Each caller defines allowed
resources; a UI file picker inserts the same syntax.

Prompt calls and their capture boundaries are defined above. On Content
surfaces a slash is ordinary text; Chat's command classification is a separate
boundary. Shell callers must quote `$` to prevent shell expansion.

## Evaluation

```text
Text        -> text Part
IncludeRef  -> one Part
PromptCall  -> Text
Content     -> Part[]
```

`Part` and `Part[]` remain parts. Other values become canonical text;
structured values use compact JSON. The declared primary type is then applied:

```text
Part[]      preserve all parts
Part        require exactly one part
Text        require text-only content
Number      parse one canonical number
Boolean     parse true or false
Json/S/T[]  parse JSON and validate the declared type
```

Conversion never discards non-text parts. Invalid input is rejected before the
run starts. Output uses the same declared-type validation; structured model
output may also be one Markdown code block labeled `json`.

Run preparation persists both authored and effective facts. The authored
policy and `CallInput[str]` sources retain `$prompt` syntax for transcript and
history views. Ordered prompt provenance records canonical arguments, cap ref,
and definition hash. Resolved locals drive conversation recall and retry/rerun,
while model steps retain the exact normalized `ModelCall` sent to the adapter.

## Shared run overrides

Chat, script, task and chore input can pair a sparse `RunOverride` with
`CallInput[str]`. Leading colon lines use POSIX quoting and escaping without
shell expansion. Blank lines after overrides are structural. These are caller
input envelopes, not agic/flow source directives.

| Form | Parsed value |
| --- | --- |
| `:model [REF] [effort=VALUE] [max_output=VALUE]` | Exact model selection and/or sparse call parameters; at least one value required. |
| `:runnable REF`, `:agic NAME`, `:flow NAME` | Runnable selection; may carry named arguments and a capture marker. Only the generic form treats `default` as a reset. |
| `:workdir PATH` | Exactly one path, quoted when needed. |
| `:allow FIELD=QUERY...` | Models, tools, psyches, skills, services or prompts; queries, `all` or `none`. |
| `:limit FIELD=VALUE...` | `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost` or `time`; nonnegative values or `none`. |

Model, runnable and workdir may each appear once. Repeated allow queries
accumulate and deduplicate; `all`/`none` cannot combine with other values for
that field. Limit fields may span lines but cannot repeat. Cost must be finite
and nonnegative; other limit values are nonnegative integers.

`effort` accepts a recognized level, canonical unsigned token budget or `auto`;
`max_output` accepts a canonical unsigned integer or `auto`. Model-specific
validation and policy precedence belong to [execution](execution.md#policy-resolution).
A syntactically valid override is not permission to broaden the resource base.

An explicit runnable selection can invoke with an empty argument map, subject
to its signature. `:flow research -` instead supplies explicit empty `_`.
Other overrides alone are invalid; they require primary or named input. Plain
run-only parsing can accept no input when the selected signature allows it.
Invalid override, argument, include, prompt or coercion rejects the complete
submission before acceptance. Slash commands and `:?` belong only to [Chat](chat.md).

Example caller input, assuming a `review` runnable with a `focus` parameter:

```text
:model effort=high
:allow tools=shell/*
:workdir lab://src
:agic review focus=security

Review the API and its tests.
```

## Implementation and verification

- [Input parsing/coercion](../src/toolang/lang/input.py) owns Content and typed boundaries.
- [Policy parsing](../src/toolang/execution/policy.py) owns shared run overrides;
  [policy tests](../tests/unit/execution/test_policy.py) cover empty/duplicate inputs and layering.
- [Language tests](../tests/unit/lang/) cover capture forms and coercion;
  [typed template regression](../tests/unit/execution/test_execution_template.py)
  checks that resolved values retain their types.

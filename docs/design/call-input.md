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

Every new runnable invocation binds supplied values to the selected target's
signature and fails if conversion is unavailable. Child-call coercion creates
child inputs without rewriting parent frame locals; only the statement's normal
result binding updates a parent local. Model-requested `exec` stores the bound
values in its control record and replacement locals, while its Tool Step retains
the raw call for inspection. Existing corrupt historical exec inputs are not
repaired; invoke the affected work again.

`rerun` restores native source inputs and binds them to the target in its captured
fresh State before creating a new Run. Convertible values follow the new
signature; newly required, removed, unknown, or incompatible arguments reject
admission without modifying the source Run. It does not parse native Json strings
again. Direct `RunSpec` inputs must already satisfy their contract.

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

The `runtime/run` and `runtime/exec` tools accept this flat dictionary in
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
Outputs use `Output(value=..., binding="_")` without a Local wrapper.
The binding supplies the destination name; `binding=None` leaves a result
unbound. Output references use `output/value`.

This format replaces the old source compartments, resolved compartments, HTTP
`args` sibling, and persisted local arrays. Execution-store compatibility follows
[Durable Truth](execution.md#durable-truth). HTTP clients must send the flat
format; no compatibility adapter or migration is provided.

## Input Forms

Root runnable calls support line and stream input; prompt calls additionally
support fenced input:

| Form | Marker | Boundary |
| --- | --- | --- |
| line | `--` | end of the current line (EOL) |
| stream | `-` | end of the current input stream (EOS) |
| fenced (prompts only) | `---` | an exact closing `---` line |

Named arguments use `name=value` and precede the marker. On these Content
surfaces, markers are recognized only as unquoted standalone tokens. Quoting
or escaping a marker keeps it in an argument value. Script command headers
support only line and stream input; [Scripts](scripts.md#runnable-input-and-help)
owns that boundary after shell quoting has been removed.

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
enclosing input for a prompt call. A root runnable call rejects any following
nonblank content. Empty line input is invalid.

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
Root runnable calls reject fenced input; use stream input instead. Backtick
fences do not introduce Call Input.

## Text runnable calls

A slash directive selects the root runnable and accepts arguments before input:

```text
/review focus=security -- Review this API
```

```text
/agic:review focus=security -
Review this API and its tests.
```

A root header followed by a body without a marker uses implicit stream input;
separator blank lines before that body are structural. Plain input without an
explicit call goes to the default runnable. `/review` alone leaves `_` absent;
`/review -` supplies an empty `_`. Requiredness and types follow the
[runnable signature](program.md#runnable-signatures).

Prompt calls inside runnable input support all three explicit forms. A closing
prompt fence returns to the same root input:

```text
/review -
$summarize ---
Source material.
---
Also check security.
```

## Requests and overrides

Text request parsing produces `RunOverride` and `CallInput[str]` without loading
an agent. Chore input and interactive submissions use this invocation envelope;
[Chat](chat.md#session-commands) additionally recognizes standalone commands.
Authored Agic and prompt bodies are [Content](content.md), not caller envelopes.

Leading `/model`, `/allow`, `/limit`, and `/workdir` lines affect one submission.
They do not update Chat defaults. Overrides occupy complete leading lines and
use POSIX quoting without shell expansion. Model and workdir may each occur
once; allow queries can accumulate; duplicate limit fields fail. A root runnable
call ends prefix parsing and following lines become primary input:

```text
/model effort=high
/allow skills=reviewer tools=shell/*
/review focus=security

Review this API.
@./api.md
```

An override prefix requires at least one supplied primary or named input key,
including explicit empty values where permitted. A direct call without an
override can have no input when its signature permits it. Missing, duplicate,
unknown, or incompatible arguments reject before Run admission. A runnable with
`()` can be called directly but cannot accept an override prefix.

Allow overrides add ceilings and cannot broaden an inherited base. Limits use
`agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`, and `time`; nonnegative
values enable a limit and `none` disables that field. [Setup](setup.md#policy)
owns defaults and precedence; [Execution](execution.md#run-execution) owns budget
enforcement. [Models](models.md#runtime-configuration) owns model expressions.
One-run model `default` restores the surface model; `unset` removes the inherited
binding. `none` is invalid. Selecting an identity clears unmentioned explicit
model parameters; parameter-only changes retain the identity.

Clients resolve session-relative paths and materialize their policy before
submission. Execution captures Setup/State, evaluates Content once, coerces the
flat mapping to the selected signature, and constructs a concrete RunSpec.
Direct-value requests bypass Content interpretation, including for native Json
strings. [API](api.md#run-and-thread-endpoints) owns transport schemas.

## Coercion boundary

Content evaluates to `Part[]` before signature binding:

```text
Part[]      preserve all parts
Part        require exactly one part
Text        require text-only content
Number      parse one canonical number
Boolean     parse true or false
Json/S/T[]  parse JSON and validate the declared type
```

Conversion does not discard non-text input Parts. Structured model output may
also use one Markdown code block labeled `json`. Native internal values are
bound without parsing their strings again. [Program](program.md#output) owns
output contracts.

Script headers have a distinct shell-token boundary; [Scripts](scripts.md#runnable-input-and-help)
owns their line/stdin forms and generated help. Prompt expansion and nested-call
restrictions belong to [Content](content.md#expansion-and-evaluation).

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

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

## Chat Runnable Calls

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

Leading `/model`, `/allow`, `/limit`, or `/workdir` overrides require at least
one supplied primary or named input key; naming a runnable alone does not count.
Direct calls without overrides may omit all input when the signature permits it.
Unknown, duplicate, or mistyped arguments and positional arguments reject.

Prompt calls inside runnable input support all three explicit forms. A closing
prompt fence returns to the same root input:

```text
/review -
$summarize ---
Source material.
---
Also check security.
```

## Authored Runnable Input

`chore` and `agic` bodies are runnable Content surfaces. They can call
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

Runnable help summarizes the available input categories:

```text
Usage: too run app.too demo [OPTIONS] [ARGUMENTS]
```

`[ARGUMENTS]` appears when the signature accepts named or primary input, including
implicit `Part[]` input. Empty signatures omit it. The Arguments panel describes
each input's assignment or capture syntax and marks required values with `*`.
The category brackets do not make required signature inputs optional.

The opening description uses `Run KIND NAME.` or `Run KIND NAME - DESCRIPTION`
when an authored doc comment exists. Usage and the help panels follow. Flows
end with an epilog: `The flow proceeds as follows:`, a blank line, and an outline
in normal style with one blank line between sibling steps; each step's doc and
operation description remain adjacent.

The **Arguments** panel lists named parameters in signature order with per-name
metavars such as `begin=<BEGIN>`, without a separate type label. A `*` marks required
parameters, and parameter doc comments appear in the help column. Missing docs use
the authored type, for example `Named input (Text)` or `Primary input (Part[])`.
The INPUT row is last when primary input is accepted, and appends
`reads stdin with - or when input is omitted` to its description. Arguments may be
supplied in any order, interspersed with command options, before input.

| Form | Behavior |
| --- | --- |
| `TEXT...` | The first ordinary operand starts input; the remaining shell words are its text. |
| `-- TEXT...` | Explicitly starts input, including text beginning with an option or assignment. |
| `-` | Reads stdin through EOF, including an empty stream. |
| Omitted | Reads piped or redirected stdin; an empty stream means input is absent. |

When primary input is forbidden, its row and instructions are absent. Empty
signatures omit Arguments entirely. **Options** follows Arguments. Top-level
Script file help says `Execute a runnable from FILE.` and lists **Runnables** before
Options, without a redundant Arguments panel. Its three columns are name, kind,
and description. The unnamed `_` comes first with its `<entry:LINE>` identity and
authored comment, then named agics and flows in source order within each kind.
Named entries use authored descriptions or `Agic NAME` / `Flow NAME` fallbacks.
Qualified selectors and bare names are both accepted. RUNNABLE is optional in
Usage only when the file has an unnamed entry.

Root and runnable help show the same common options, ordered as `-q` / `--quiet`,
`-o` / `--out`, `--model`, `-w` / `--workspace`, `-d` / `--workdir`,
`--sandbox`, `--allow`, `--limit`, `--dev`, then
`-h` / `--help`. Common options may appear on either side of RUNNABLE, before
input. Runnable-level scalar values override root values when explicitly set;
repeated `--workspace`, `--allow`, and `--limit` values accumulate in command-line
order. `--workdir` may be specified only once across both levels. See
[Scripts](scripts.md) for workspace and path rules.
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

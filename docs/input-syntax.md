# Input Syntax

This document defines runnable input, Chat session settings and run overrides,
shared `Content` syntax, and runnable-boundary coercion.

## Terminology

A **command** is one standalone Chat interaction. A **run directive** describes
one run submission. Commands use slash or colon syntax; run directives use
slash, dollar, or at syntax. A **root runnable call** selects the submitted
run's target; a **prompt call** expands Content inside its input.

Code names follow these roles: `ChatCommand` is a parsed command,
`CommandSpec` is its registry entry, and `CommandOutcome` is its result.
`RunOverride` and `CallInput` are parsed execution values, not command categories.
`PromptInvocation` records resolved prompt-call provenance. Parameter, argument,
and primary-input terms follow [Call Input](call-input.md#terminology).

## Input Layers

The layers are independent values with separate parsers:

```text
CallInput[T] = {"_": Input?, argumentName: Argument, ...}
RunnableInput = CallInput[Value]

ChatInput = ChatCommand | tuple[RunOverride, CallInput[str]]
```

- `CallInput[str]` contains source text under `_` and argument names. Prompt,
  script, and runnable input share the same structure. The value specialization
  holds evaluated input; see [Call Input](call-input.md).
- `SessionSetting` holds Chat defaults for subsequent runs: model, runnable,
  workdir, resource ceilings, and limits.
- `RunOverride` supplies changes for one run; it never updates the session.
- `ChatCommand` represents a standalone slash or colon command and occupies
  the complete submission.
- **Run directives** describe one submitted run: **slash directives** specify
  overrides or a root runnable call, **dollar directives** expand prompts, and
  **at directives** include resources.
- A direct runnable call may omit input if its signature permits it. Any
  override prefix requires at least one primary or named input key, including
  explicit empty values when their types accept them.

Script and chore surfaces parse the run-only pair of `RunOverride` and
`CallInput[str]`; they do not parse `ChatCommand`. Agic, flow, and prompt
bodies are `Content` sources rather than caller-input envelopes.

Parsing and resolution are separate. Parsing produces `RunOverride` and
`CallInput[str]` without loading an agent. Chat overlays the immutable surface
baseline, `SessionSetting`, and `RunOverride` into a self-contained `RunRequest`.
Execution then validates the concrete model and runnable, evaluates content,
coerces named inputs, and constructs `RunSpec`.

## Session Settings and Run Overrides

Colon commands change session defaults; slash directives override one run.
The setting bodies are shared:

| Session command | Run directive |
| --- | --- |
| `:model BODY` | `/model BODY` |
| `:allow BODY` | `/allow BODY` |
| `:limit BODY` | `/limit BODY` |
| `:workdir PATH` | `/workdir PATH` |

`:workdir` alone queries the session directory. `default` or `unset` resets it;
relative paths retain the existing session-relative resolution.

Model bodies contain an optional identity followed by assignments:

```text
:model openai/gpt-5
:model effort=high
:model effort=4096
:model openai/gpt-5 effort=high
:model max_output=8192
:model effort=auto max_output=auto
```

`effort` is input convenience. A canonical unsigned integer becomes
`reasoning.budget_tokens`; a recognized level becomes `reasoning.effort`; and
`auto` removes explicit reasoning so the model or provider chooses its default
reasoning behavior. `max_output` is a canonical unsigned integer ceiling for one
model call, or `auto` to use the model's native output allowance. Omitting a
control inherits the value in effect; passing `auto` cancels an inherited value.
An effort, budget, or output ceiling is validated against the effective model
before run acceptance. Parameter-only input retains the model identity. Selecting
an identity clears unmentioned explicit model parameters. Bare one-run model
`default` restores the surface model, while `/model unset` removes the inherited
model binding for that run. `:model default` selects the effective default inside
the session's current `allow.models` result. `:model unset`, `:model none`, and
`/model none` are invalid.

Session runnable commands select defaults: `:runnable REF`, `:agic NAME`, and
`:flow NAME`. Only `:runnable default` resets to the surface default; kind-specific
commands treat `default` as a name. They accept no runnable arguments.

These runnable defaults belong to the current Chat session. Chat startup
`--default runnable=REF` initializes only that session's entry. Input-local
overrides affect one submission; resetting the session entry restores automatic
Chat handler resolution. Other sessions, Talk, and Chore are unaffected.
Root/agent `[default].runnable`, `TOOLANG_DEFAULT_RUNNABLE`, and runtime startup
runnable defaults are unsupported; use a named driver handler instead.

A root call selects a public runnable for this submission:

```text
/review [name=value...]
/agic:review [name=value...]
/flow:review [name=value...]
```

Names are case-sensitive, with no space after `/`. Exact built-in names win;
invalid built-in arguments never fall back to runnable lookup. Other refs use
the valid State's final public bindings. Kind or module qualification constrains
that binding without exposing private targets or repeating shadow resolution.
For a runnable named `help` or `model`, use `/agic:help` or `/flow:model`.

`/runnable NAME`, `/agic NAME`, and `/flow NAME` are not selector forms.
`runnable`, `agic`, `flow`, `default`, and `cd` are ordinary runnable names.
`/default` calls that named runnable; it does not select the surface default.

Allow and limit bodies accept one or more assignments:

```text
:allow models=openai/* tools=shell/* skills=reviewer
/allow models=openai/* tools=shell/*

:limit tokens=20000 cost=2.5 time=120
/limit tokens=4000 time=30
```

Allow fields are `models`, `tools`, `psyches`, `skills`, `services`, and
`prompts`. Values accept a query, `all`, or `none`. Repeated queries for one
field accumulate and deduplicate; `all` and `none` cannot combine with another
value. A colon assignment replaces that session field. A slash assignment adds
another ceiling and cannot broaden the session ceiling.

Limit fields are `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`, and
`time`. Integer fields require non-negative integers; cost requires a finite
non-negative decimal. `none` disables one limit. Duplicate limit fields in a
submission are invalid.

Run overrides occupy complete leading lines and use POSIX quoting without shell
expansion. Blank lines between prefix lines and implicit input are structural.
Model and workdir may each appear once; allow may span lines; limit fields must
remain unique. A root call ends prefix parsing; following lines become primary
input. A standalone command cannot be combined with this prefix.

```text
/model effort=high
/allow skills=reviewer tools=shell/*
/review focus=security

Review this API.
@./api.md
```

## Terminal Chat Input

Slash and colon commands are terminal-chat interactions and must occupy the complete
normalized submission:

```text
/help                     /?                       /keys
/models [-a] [QUERY]       /tools [-a] [QUERY]
/caps [-a] [QUERY]         /agics                    /flows
/output [RUN]             /show [RUN]
/exit                     /quit

:model [MODEL] [effort=VALUE] [max_output=VALUE]
:runnable RUNNABLE
:agic AGIC                :flow FLOW
:allow FIELD=QUERY...
:limit FIELD=VALUE...
:workdir [PATH]
:?
```

Bare `:model`, `:runnable`, `:agic`, `:flow`, `:allow`, and `:limit` show focused
usage and examples. `:runnable`, `:agic`, and `:flow` share focused help.
`:workdir` queries the directory, and `:?` shows session settings. These forms
never create a run. An editor completion popup only edits the draft.

`/models`, `/tools`, and `/caps` inspect resources allowed by the current
session. Their optional body is one collection query, including any spaces; it
is intersected with the allowed collection rather than added to its allow
queries. `-a` changes the base to all available resources and adds an `ALLOWED`
column. `/caps` evaluates session ceilings independently for psyches, skills,
services, and prompts before combining the results. `/agics` and `/flows` list
all available runnables of their kind and do not accept a query.

Every submitted command that keeps Chat open writes its outcome to
scrollback. State changes use concrete confirmations such as `Model set to ...`
or `Allowed 2 models`; resource summaries distinguish `allowed` from
`available` and include the base count when a query is present. Missing
required bodies use focused help and command failures use `Error:`. Successful
setting changes also refresh the status bar's current session values.

When the session's model ceiling changes, Chat preserves the complete current
model request if it remains available. Otherwise it selects the configured
default when present in the ordered result, then the first result. Only an empty
model collection leaves the session model unset.

`/help` and `/?` describe commands and run directives. `/keys` lists interactive
shortcuts. Known command help and errors enter scrollback and clear the input.
Unknown colon commands and rejected run submissions retain the draft and show
an error in the status bar. Unreserved slash names are resolved as runnables.

Enter sends the complete normalized input. It starts a runnable request when
Chat is idle and queues that request while a run is starting or active;
standalone slash and colon commands still execute during
a run. Meta+Enter sends the draft as literal steer text to the active run and
does not apply any input namespace. Ctrl+J inserts a newline; Shift+Enter also
does so when supported by the terminal.

Queued runnable inputs appear expanded above the prompt without taking focus.
Tab and Shift+Tab move focus between Queue and Input unless an input completion
menu is active. In Queue, ↑ and ↓ select an item, e moves it to an empty
prompt for editing, Meta+Enter steers with it, and d or Del removes it.
Editing or steering only removes an item after the local action is accepted.

`/queue`, `/q`, `/steer`, and `/s` are not commands. Use the Queue panel and
Meta+Enter instead. Esc Esc, Ctrl+C, and Ctrl+D are restricted to Input focus.
Esc never moves focus or cancels a run from Queue; Ctrl+L and Ctrl+Q remain
global. `/keys` lists Input, Queue, and global shortcuts separately.
The Queue area disappears when it becomes empty.

Chat removes leading and trailing blank lines, then removes horizontal
whitespace from the end of the final line. It preserves indentation on the
first nonblank line and all internal runnable-input whitespace. Removed forms
have no aliases: `:cd`, `:default`, collection shortcuts such as `:models`,
and positional model effort are invalid. Migrate `/cd` to `:workdir`, old
slash session settings to colon commands, and old colon run input to slash
directives. Stored history is not rewritten.

## Runnable Input

`CallInput[str]` stores an optional `Content` source under `_` and each
argument source under its parameter name. Source order is preserved; argument
binding uses names.
Chat obtains named sources from root runnable calls; script obtains them from
its generated CLI. Resolution evaluates each source and coerces it against the
selected runnable signature. Missing, duplicate, or unknown named inputs are
rejected before a run is accepted.

Root runnable calls support line (`-- TEXT`), stream (`-`), and implicit
following-body input. Line input must be nonempty; only whitespace may follow
a root line-input call. Fenced input (`---`) belongs only to prompt calls.
See [Call Input](call-input.md) for boundaries and signature requirements.
Script CLI command headers keep their existing line and stdin forms.

Bare `/review` leaves primary input absent; `/review -` supplies empty `_`.
Without overrides, the signature alone decides whether empty Call Input is valid.
With an override prefix, at least one input key must be supplied and the complete
signature must still be satisfied. Thus a runnable with `()` can run directly,
but cannot accept a run override prefix. Optional omitted arguments stay absent.
Parsing and admission are atomic: invalid arguments, boundaries, or types reject
the submission without executing a command or accepting a run.

## Content

```text
InputContent = NonEmptyContent
Content      = ContentItem*
ContentItem  = Text | IncludeRef | PromptCall

evaluate(InputContent | Content) -> Part[]
```

`$` and `@` are special only as the first character of a `Content` line. `:` is
special at a command boundary, and `/` introduces commands or run directives
before primary input. Slash lines within captured Content are text. Ordinary
Markdown code fences suspend special-line recognition. Double a leading marker
where its single form would be special to produce literal text:

```text
//help           -> /help
$$review         -> $review
::model gpt-5    -> :model gpt-5
@@README.md      -> @README.md
```

### Includes

```text
IncludeRef  = "@" ResourceRef
ResourceRef = Path | QuotedPath
```

Examples: `@README.md` and `@"path with spaces/image.png"`. An include occupies
its complete line and resolves to one `Part`. Leading whitespace makes it text.
The built-in resolver reads filesystem paths against the caller's explicit base;
Chat/script clients may materialize attachments before HTTP submission. There
is no built-in upload registry or file-picker contract. See [Scripts](scripts.md#file-inputs).

### Prompts

Prompt arguments use `name=value`. A prompt call may omit primary input or use
the shared line (`--`), stream (`-`), or fenced (`---`) Call Input forms. The
complete syntax, boundaries, Script and Chat integration, and examples are in
[call-input.md](call-input.md).

Prompt templates expand to Text before the complete runnable input is parsed
once as Content. A prompt result can therefore be one fragment of a larger
input. Runnable input may contain prompt calls, but prompt input and prompt
results cannot contain another prompt call. The capture form is not persisted
with prompt provenance.

Prompt calls use dollar directives. Shell commands must single-quote
dollar-prefixed input, for example:

```sh
too run review.too main -- '$review focus=security'
```

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
and definition hash. Resolved locals drive conversation recall and rerun,
while model steps retain the exact normalized `ModelCall` sent to the adapter.

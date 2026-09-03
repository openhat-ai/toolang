# Input Syntax

This document defines runnable input, Chat session settings and run overrides,
shared `Content` syntax, and runnable-boundary coercion.

## Input Layers

The layers are independent values with separate parsers:

```text
CallInput = PrimaryInput? + NamedInput*
RunnableInputRaw : CallInput

ChatInput = QuickCommand | RunOverrideHelp | RunOverride + RunnableInputRaw
```

- `CallInput` is language-owned syntax-valid input with one optional primary
  source and zero or more named sources. `RunnableInputRaw` preserves the
  public runnable boundary; prompt calls use `CallInput` directly.
- `SessionSetting` is Chat's concrete model, runnable, allow, and limit default
  for subsequent runs in the current session.
- `RunOverride` is a sparse model, runnable, allow, and limit change attached to
  one runnable input.
- `QuickCommand` and the `ChatInput` classification are terminal-chat
  concepts. Only one `QuickCommand` is accepted, and it must occupy the whole
  chat input.
- `RunOverrideHelp` is the terminal-chat-only `:?` interaction. It describes
  colon syntax without creating a run.
- The runnable branch is valid only when it contains primary or named input. A
  colon override without runnable input is invalid and never changes the
  session. Slash setting commands change `SessionSetting` without creating a
  run.

Script, task, and chore surfaces parse the run-only pair of `RunOverride` and
`RunnableInputRaw`; they do not parse `QuickCommand`. Agic, flow, and prompt
bodies are `Content` sources rather than caller-input envelopes.

Parsing and resolution are separate. Parsing produces `RunOverride` and
`RunnableInputRaw` without loading an agent. Chat overlays the immutable surface
baseline, `SessionSetting`, and `RunOverride` into a self-contained `RunRequest`.
Execution then validates the concrete model and runnable, evaluates content,
coerces named inputs, and constructs `RunSpec`.

## Session Settings and Run Overrides

The command body is shared. `/` changes the Chat session; a leading `:` changes
only the run in the same submission:

```text
/model BODY       :model BODY
/runnable BODY    :runnable BODY
/agic NAME        :agic NAME
/flow NAME        :flow NAME
/allow BODY       :allow BODY
/limit BODY       :limit BODY
```

Model bodies contain an optional identity followed by assignments:

```text
/model openai/gpt-5
/model effort=high
/model effort=4096
/model openai/gpt-5 effort=high
/model effort=auto
```

`effort` is input convenience. A canonical unsigned integer becomes
`reasoning.budget_tokens`; a recognized level becomes `reasoning.effort`; and
`auto` removes explicit reasoning so the model or provider chooses its default
reasoning behavior. An effort or budget is validated against the effective model
before run acceptance. Parameter-only input retains the model identity. Selecting
an identity clears unmentioned explicit model parameters. Bare one-run model
`default` restores the surface model, while `:model unset` removes the inherited
model binding for that run. `/model default` selects the effective default inside
the session's current `allow.models` result. `/model unset`, `/model none`, and
`:model none` are invalid.

The generic runnable form accepts an exact kind-qualified ref or a uniquely
resolvable unqualified ref. `agic` and `flow` are exact shorthand:

```text
:runnable agic:review      :agic review
:runnable flow:research    :flow research
:runnable review
```

`default` resets only the generic runnable form. Kind-specific commands treat it
as a normal name. Colon runnable commands may carry named runnable input, such
as `:agic review focus=security`; slash runnable commands may not.

Allow and limit bodies accept one or more assignments:

```text
/allow models=openai/* tools=shell/* skills=reviewer
:allow models=openai/* tools=shell/*

/limit tokens=20000 cost=2.5 time=120
:limit tokens=4000 time=30
```

Allow fields are `models`, `tools`, `psyches`, `skills`, `services`, and
`prompts`. Values accept a query, `all`, or `none`. Repeated queries for one
field accumulate and deduplicate; `all` and `none` cannot combine with another
value. A slash assignment replaces that session field. A colon assignment adds
another ceiling and cannot broaden the session ceiling.

Limit fields are `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`, and
`time`. Integer fields require non-negative integers; cost requires a finite
non-negative decimal. `none` disables one limit. Duplicate limit fields in a
submission are invalid.

Colon overrides occupy complete leading lines and use POSIX shell quoting and
backslash escaping without expansion. Blank lines between overrides and the
primary input are structural. Model and runnable may each appear once; allow
may span lines; limit fields may span lines but remain unique.

```text
:model effort=high
:allow skills=reviewer tools=shell/*
:agic review focus=security

Review this API.
@./api.md
```

## Terminal Chat Input

Slash commands are terminal-chat interactions and must occupy the complete
normalized submission:

```text
/help                     /?                       /keys
/model [MODEL] [effort=VALUE]
/runnable RUNNABLE
/agic AGIC                /flow FLOW
/allow FIELD=QUERY...
/limit FIELD=VALUE...
/models [-a] [QUERY]      /tools [-a] [QUERY]
/caps [-a] [QUERY]
/agics                    /flows
/output [RUN]             /show [RUN]
/exit                     /quit
:?
```

Every setting body is required. Submitting bare `/model`, `/runnable`, `/agic`,
`/flow`, `/allow`, or `/limit` shows focused usage, purpose, and examples; it
does not list values or open a picker. `/runnable`, `/agic`, and `/flow` share
one focused help result. An editor completion popup may insert text into the
draft, but selection never submits input or changes the session. A slash
command cannot be combined with a colon override or runnable input.

`/models`, `/tools`, and `/caps` inspect resources allowed by the current
session. Their optional body is one collection query, including any spaces; it
is intersected with the allowed collection rather than added to its allow
queries. `-a` changes the base to all available resources and adds an `ALLOWED`
column. `/caps` evaluates session ceilings independently for psyches, skills,
services, and prompts before combining the results. `/agics` and `/flows` list
all available runnables of their kind and do not accept a query.

Every submitted slash command that keeps Chat open writes its outcome to
scrollback. State changes use concrete confirmations such as `Model set to ...`
or `Allowed 2 models`; resource summaries distinguish `allowed` from
`available` and include the base count when a query is present. Missing
required bodies use focused help and command failures use `Error:`. Successful
setting changes also refresh the status bar's current session values.

When the session's model ceiling changes, Chat preserves the complete current
model request if it remains available. Otherwise it selects the configured
default when present in the ordered result, then the first result. Only an empty
model collection leaves the session model unset.

`/help` and its `/?` alias are the main input guide. They group session,
inspection, and other commands, identify aliases, and direct users to `:?` for
one-run colon directives. `/keys` lists Toolang-owned interactive shortcuts.
These are submitted read-only interactions and remain in scrollback. A bare or
unknown slash command and any rejected runnable submission remain editable in
the input box; their diagnostic appears in the status bar. Known slash-command
help and errors are completed command outcomes, so they enter scrollback and
clear the input.

Enter sends the complete normalized input. It starts a runnable request when
Chat is idle and queues that request while a run is starting or active;
recognized immediate interactions such as slash commands still execute during
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
have no aliases: settings-only colon input, `:default`, collection shortcuts
such as `:models`, and positional `/model REF EFFORT` are invalid.

## Runnable Input

`RunnableInputRaw._` is an optional `Content` source.
`RunnableInputRaw.named` is an ordered set of unique `Name=Content` sources.
Chat obtains named sources from runnable shortcuts; script obtains them from
its generated CLI. Resolution evaluates each source and coerces it against the
selected runnable signature. Missing, duplicate, or unknown named inputs are
rejected before a run is accepted.

Runnable calls use the shared line, stream, and fenced capture syntax defined
in [call-input.md](./call-input.md). The capture form is parser-only state and
is not stored in `RunnableInputRaw`.

Plain run-only parsing permits an empty `RunnableInputRaw` when the selected
runnable accepts no primary or named input. A colon override still requires
primary or named runnable input. Parsing is atomic: any invalid run override,
named input, include, prompt, or coerced value rejects the complete input.

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

### Prompts

Prompt arguments use `name=value`. A prompt call may omit primary input or use
the shared line (`--`), stream (`-`), or fenced (`---`) Call Input forms. The
complete syntax, boundaries, Script and Chat integration, and examples are in
[call-input.md](./call-input.md).

Prompt templates expand to Text before the complete runnable input is parsed
once as Content. A prompt result can therefore be one fragment of a larger
input. Runnable input may contain prompt calls, but prompt input and prompt
results cannot contain another prompt call. The capture form is not persisted
with prompt provenance.

Slash-prefixed prompt calls are not supported. In terminal Chat, a leading
slash belongs to the interaction namespace; on other `Content` surfaces it is
ordinary text. Shell commands must single-quote dollar-prefixed input, for
example:

```sh
too run alice '$review focus=security'
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
policy and `RunnableInputRaw` sources retain `$prompt` syntax for transcript and
history views. Ordered prompt provenance records canonical arguments, cap ref,
and definition hash. Resolved locals drive conversation recall and retry/rerun,
while model steps retain the exact normalized `ModelCall` sent to the adapter.

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
/help                     /show [RUN_ID]
/?                        /queue [ACTION]
/model MODEL? effort=VALUE
/steer MESSAGE
/agic AGIC                /quit
/flow FLOW                /exit
/runnable RUNNABLE
/allow FIELD=QUERY...
/limit FIELD=VALUE...
/models [QUERY]           /tools [QUERY]
/caps [QUERY]             /keys
:?
```

Every setting body is required. Submitting bare `/model`, `/runnable`, `/agic`,
`/flow`, `/allow`, or `/limit` neither lists values nor opens a picker. An
editor completion popup may insert text into the draft, but selection never
submits input or changes the session. A slash command cannot be combined with a
colon override or runnable input.

`/models`, `/tools`, and `/caps` inspect effective base resources. Their
optional body is one collection query, including any spaces; it is not split
into positional arguments. With no query they list the complete collection.
`/caps` applies the query independently to psyches, skills, services, and
prompts before combining the results.

Every submitted slash command that keeps Chat open writes its outcome to
scrollback. State changes use concrete confirmations such as `Model set to ...`
or `Allowed 2 models`; read-only commands use summaries such as `Found 2
models`. Missing required bodies use `Usage:` and command failures use `Error:`.
Successful setting changes also refresh the status bar's current session
values.

When the session's model ceiling changes, Chat preserves the complete current
model request if it remains available. Otherwise it selects the configured
default when present in the ordered result, then the first result. Only an empty
model collection leaves the session model unset.

`/?` explains slash commands, `:?` explains one-run overrides, and `/keys`
lists Toolang-owned interactive shortcuts. These are submitted read-only
interactions and remain in scrollback. A bare or unknown slash command and any
rejected runnable submission remain editable in the input box; their diagnostic
appears in the status bar. Known slash-command usage and errors are completed
command outcomes, so they enter scrollback and clear the input.

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

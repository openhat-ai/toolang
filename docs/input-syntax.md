# Input Syntax

This document defines runnable input, execution policy commands, terminal-chat
input, shared `Content` syntax, and runnable-boundary coercion.

## Input Layers

The layers are independent values with separate parsers:

```text
RunnableInputRaw = PrimaryInput? + NamedInput*

ChatInput
    = QuickCommand
    | RunOverride+
    | RunOverride* + RunnableInputRaw
```

- `RunnableInputRaw` is language-owned syntax-valid input. It contains only one
  optional primary source and zero or more named sources.
- `RunOverride` is execution-owned policy input. It changes `allow`,
  `default`, or `limit` fields but contains no runnable input.
- `QuickCommand` and the `ChatInput` classification are terminal-chat
  concepts. Only one `QuickCommand` is accepted, and it must occupy the whole
  chat input.
- The runnable branch is valid only when it contains a primary or named input.
  A policy-only branch updates later chat runs; the same policy prefix on a
  runnable branch applies only to that run.

Script, task, and chore surfaces parse the run-only pair of policy commands and
`RunnableInputRaw`; they do not parse `QuickCommand`. Agic, flow, and prompt
bodies are `Content` sources rather than caller-input envelopes.

Parsing and resolution are separate. Parsing produces `RunOverride` and
`RunnableInputRaw` without loading an agent. Execution resolution later overlays
the current `AgentSetup`, session policy, and run policy; selects the runnable;
evaluates content; coerces named inputs; constructs `RunnableInput`; and then
constructs `RunSpec`.

## Policy Commands

Canonical commands map directly to execution policy fields:

```text
:allow COLLECTION=QUERY
:default FIELD=VALUE
:limit FIELD=VALUE
```

Supported fields are:

| Group | Fields |
| --- | --- |
| `allow` | `models`, `tools`, `psyches`, `skills`, `services`, `prompts` |
| `default` | `model`, `runnable` |
| `limit` | `agic_model_calls`, `agic_tool_calls`, `tokens`, `cost`, `time` |

`allow` query values may use standalone `all` or `none`. `all` removes that
field's restriction; `none` permits no value in the resulting collection. They
cannot be mixed with a query. The four cap-kind fields remain separate through
policy resolution, matching setup config and process overrides.
`default ...=none` clears an explicit binding, while
`limit ...=none` disables that limit. These values have group-specific meanings
and are not accepted as ordinary names.

The common default shortcuts are:

```text
:model MODEL
:agic AGIC [NAME=VALUE ...]
:flow FLOW [NAME=VALUE ...]
:runnable RUNNABLE [NAME=VALUE ...]
```

They map to `default model=...` or `default runnable=...`. `:agic` and `:flow`
qualify the runnable kind. The reserved value `default` clears that explicit
binding and returns to the surface binding. Named values on a runnable
shortcut belong to `RunnableInputRaw`, so the line starts a run even when there
is no primary input.

The allow shortcuts are:

```text
:models QUERY       :psyches QUERY
:tools QUERY        :skills QUERY
                    :services QUERY
                    :prompts QUERY
```

Each command occupies one complete line and uses POSIX shell word quoting and
backslash escaping without expansion, substitution, or globbing. Repeated
`allow` commands for one field accumulate and deduplicate queries. Repeated
`default` or `limit` fields in one input are invalid. Blank lines may appear
between policy commands and between the policy prefix and primary input; they
are structural and are not part of the primary source.

```text
:model openai/gpt-5
:skills reviewer
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
/model [MODEL [EFFORT|auto]]
/steer MESSAGE
/agic [AGIC]              /quit
/flow [FLOW]              /exit
/runnable [RUNNABLE]
```

In the interactive TUI, `/model` opens a searchable model picker and, when
supported, a second reasoning-effort picker. Scripted Chat lists models and
their efforts instead. `/model MODEL` selects Auto effort, `/model MODEL
EFFORT` selects one catalog-advertised effort, and `/model MODEL auto` clears
it. There is no `/models` command. The runnable commands use
the same no-argument listing and one-argument selection rule. A slash command
cannot be combined with policy or runnable input. Chat removes leading and
trailing blank lines, then removes horizontal whitespace from the end of the
final line. It preserves indentation on the first nonblank line and all
internal runnable-input whitespace.

Colon policy directives remain canonical. Former colon quick-command spellings
are not aliases: use `/help`, `/model`, and the other slash interactions.
`:models QUERY` remains an allow-policy shortcut, while no-argument
`:models` is invalid.

## Runnable Input

`RunnableInputRaw._` is an optional `Content` source.
`RunnableInputRaw.named` is an ordered set of unique `Name=Content` sources.
Chat obtains named sources from runnable shortcuts; script obtains them from
its generated CLI. Resolution evaluates each source and coerces it against the
selected runnable signature. Missing, duplicate, or unknown named inputs are
rejected before a run is accepted.

Run-only parsing permits an empty `RunnableInputRaw` when the selected runnable
accepts no primary or named input. Parsing is atomic: any invalid policy
command, named input, include, prompt, or coerced value rejects the complete
input.

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

```text
PromptCall = PromptHeader | TailPrompt | InlinePrompt | FencedPrompt(n)
PromptHeader = "$" PromptName (Space Argument)*

TailPrompt
    = PromptHeader Space "-" LineBreak RemainingContent

InlinePrompt
    = PromptHeader Space "--" Space InlineText

FencedPrompt(n)
    = PromptHeader Space Fence(n) LineBreak FencedContent(n) FenceLine(n)

Fence(n)     = exactly n backticks, n >= 3
FenceLine(n) = Fence(n) followed by LineBreak or EOF
```

No prompt input; later lines remain in the enclosing content:

```text
$common a=b c=d
```

Use all remaining content in the current scope:

```text
$common a=b c=d -
Review this file:
@./api.md
```

Use the nonempty remainder of the current line as literal text input:

```text
$common a=b c=d -- Review this file
```

Use only a matching fenced block:

````text
$common a=b c=d ```
Review this file:
@./api.md
```

Continue outside the prompt.
````

`RemainingContent` ends with the current content scope. Inline input consumes
only its current line, requires non-whitespace text, and does not recognize
embedded markers. `FencedContent(n)` is the exact text before the first
complete line equal to `Fence(n)` and may be empty. The `-`, `--`, or opening
fence delimiter must be an unquoted standalone token after all `name=value`
arguments. Tail and fenced bodies recursively evaluate nested `$` and `@`
lines. Fence lines are excluded; an unclosed fence is invalid. Prompt calls
evaluate from the innermost call outward.

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
PromptCall  -> Part[]
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
history views. Ordered prompt provenance records canonical arguments, input
scope, nesting parent, cap ref, and definition hash. Resolved locals drive
conversation recall and retry/rerun, while model steps retain the exact
normalized `ModelCall` sent to the adapter.

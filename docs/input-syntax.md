# Submission And Input Syntax

This document defines the shared text syntax for chat, script, task, chore,
agic, flow, and prompt input.

## Submission

```text
Submission   = QuickCommand | SettingCommand* | RunnableCall
RunnableCall = RunOverride* + InputContent
```

- `QuickCommand` performs one immediate chat operation.
- `SettingCommand` changes settings used by later chat runs.
- `RunnableCall` supplies overrides and content for one run.

| Source | QuickCommand | SettingCommand* | RunnableCall |
| --- | ---: | ---: | ---: |
| chat TUI/WebUI | yes | yes | yes |
| script | no | no | yes |
| task/chore | no | no | yes |

Agic, flow, and prompt bodies are `Content`, not `Submission`.

### Lines And Resolution

```text
LineBreak = "\n" | "\r\n"
Space     = (" " | "\t")+
BlankLine = (" " | "\t")* LineBreak
```

Discard at most one final `LineBreak`, then resolve the remaining body:

1. A complete quick-command line, followed only by blank lines, is a
   `QuickCommand`.
2. Read consecutive setting-shaped lines from the start.
3. If only blank lines remain, parse each line independently as a
   `SettingCommand`.
4. Otherwise parse each line independently as a `RunOverride`. Discard the
   line break after the last override and at most one following `BlankLine`;
   the exact remainder is `InputContent`.
5. With no leading setting-shaped lines, the complete body is `InputContent`
   and there are no `RunOverride` values.

On a run-only source, selector lines with no following content are
`RunOverride` values with empty content, not `SettingCommand` values. The call
is valid only when the selected runnable accepts no primary input.

`InputContent` must contain a non-whitespace item. Until its first item, an
unescaped `:` at line start enters the command namespace. A quick command
cannot be combined with other lines; an unknown or malformed command is an
error, not text. Use `::` to start input with a literal colon. After content
starts, `:` is ordinary text.

Parsing is atomic. Any invalid command, override, argument, include, prompt,
or value rejects the complete submission.

### Quick Commands

```text
:help                 :show [RUN_ID]
:?                    :queue [ACTION]
:model                :steer MESSAGE
:models               :quit
:agic                 :exit
:flow
```

`:model`, `:agic`, and `:flow` without a value are quick commands. With a
value, they are settings or overrides.

### Setting Commands

```text
SettingCommand
    = ":model" Space (ModelSelector | "auto")
    | ":agic" Space ("auto" | AgicName (Space Argument)*)
    | ":flow" Space FlowName (Space Argument)*

Argument = Name "=" Value
```

### Runnable Calls

```text
RunOverride
    = ":model" Space (ModelSelector | "auto")
    | ":agic" Space ("auto" | AgicName (Space Argument)*)
    | ":flow" Space FlowName (Space Argument)*
```

`SettingCommand` and `RunOverride` are intentionally separate definitions.
The first affects later chat runs; the second belongs only to one
`RunnableCall`.
Each occupies one complete line. Consecutive values are separated by
`LineBreak`; the final override and `InputContent` are separated as specified
by the resolution rules above.

Both enforce:

- at most one model line and one runnable line
- mutual exclusion of `:agic` and `:flow`
- no arguments after `:agic auto`; no `:flow auto` form
- unique, signature-checked arguments, including every required argument
- no primary input on a setting or override line

Lines use POSIX shell word quoting and backslash escaping, without expansion,
substitution, or globbing. Trailing horizontal whitespace is ignored.

```text
:model openai/gpt-5
:agic review focus=security

Review this API.
@./api.md
```

Inline primary input, such as
`:agic review focus=security -- Review this API.`, is invalid.

## Content

```text
InputContent = NonEmptyContent
Content      = ContentItem*
ContentItem  = Text | IncludeRef | PromptCall

evaluate(InputContent | Content) -> Part[]
```

`/` and `@` are special only as the first character of a line. Ordinary
Markdown code fences suspend special-line recognition. Double a leading marker
to produce literal text:

```text
::model gpt-5    -> :model gpt-5
//review         -> /review
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
PromptCall = PromptHeader | TailPrompt | FencedPrompt(n)
PromptHeader = "/" PromptName (Space Argument)*

TailPrompt
    = PromptHeader Space "-" LineBreak RemainingContent

FencedPrompt(n)
    = PromptHeader Space Fence(n) LineBreak FencedContent(n) FenceLine(n)

Fence(n)     = exactly n backticks, n >= 3
FenceLine(n) = Fence(n) followed by LineBreak or EOF
```

No prompt input; later lines remain in the enclosing content:

```text
/common a=b c=d
```

Use all remaining content in the current scope:

```text
/common a=b c=d -
Review this file:
@./api.md
```

Use only a matching fenced block:

````text
/common a=b c=d ```
Review this file:
@./api.md
```

Continue outside the prompt.
````

`RemainingContent` ends with the current content scope. `FencedContent(n)` is
the exact text before the first complete line equal to `Fence(n)` and may be
empty. The `-` or opening fence must be the final header token. Fence lines are
excluded; an unclosed fence is invalid. Prompt calls evaluate from the
innermost call outward.

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

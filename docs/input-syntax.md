# Input Syntax

This document defines how interactive submissions and authored content become
content and run input.


## Structure

A submission is either a command or content:

```text
Submission = Command | Content
Command    = Slash | Shell
Content    = Text | Prompt | Include
```

A command occupies the first non-empty line and may own the content that
follows it. Content produces `Part[]` input.

`Part[]` is ordered multimodal content. Plain multiline text produces one
`TextPart` containing the complete text; line breaks alone do not create more
parts.


## Applicable Sources

The same content parser is used across input surfaces, but submission commands
and runtime template variables are source-specific:

| Source | Slash | Shell | Prompt/include | Template variables |
| --- | --- | --- | --- | --- |
| CLI chat input box | yes | yes | yes | no |
| WebUI chat input box | yes | no | yes | no |
| `task.md` / `chore.md` content | no | no | yes | no |
| agic message or content | no | no | yes | yes |
| flow statement or `let` content | no | no | yes | yes |

CLI and WebUI command registries may expose different built-in commands, but a
shared command keeps the same syntax and semantics. A WebUI must reject a
shell-command line rather than execute it on the server or send it to the
model as ordinary input.

Task and chore documents contain authored content. Prompts are expanded
when the job creates a run, and include paths are resolved relative to the job
document. The content itself does not interpolate run locals; `{{name}}` remains
literal text unless it belongs to an expanded prompt template.

Agic and flow content is program-authored. Include paths are resolved
relative to the `.too` source. Template variables may read `_`, named locals,
and the documented runtime template context. Inline flow content uses the same
rules as the generated inline agic; `let` content uses them without making a
model call.

For browser input, includes resolve only uploaded or otherwise
authorized file references. They never expose arbitrary server paths.


## Executable Input

Agics and flows use `_` as their primary input parameter:

```too
agic chat:                         # implicit _: Part[]
agic ping():                       # no primary input
agic review(_):                    # explicit _: Part[]
agic parse(_: Json, mode: Text):

flow research(_: Text) -> Report:
```

Omitting the parameter list implies `_ : Part[]`. An explicit empty list `()`
means the executable accepts no primary input. An untyped parameter defaults to
`Part[]`.

Interactive and authored content is parsed first, producing `Part[]`. The
calling surface then coerces that value to the declared primary input type:

```text
Part[]     preserve ordered parts
Part       require exactly one part
Text       require text-only content and join it without losing authored lines
Number     parse one canonical number
Boolean    parse true or false
Json       parse one JSON value
Struct/T[] parse JSON and validate the declared type
```

No implicit coercion may discard a non-text part. Invalid input is rejected
before the executor emits `run_begin`.

Type and flow shape are independent. A primary input whose type is `Part[]` or
`T[]` initializes one local with `shape=item`; it becomes `shape=list` only
through a flow operation that explicitly creates a collection.


## Commands

Commands are recognized only on the first non-empty line.

### Slash

Built-in commands use reserved slash names:

```text
/help
/run review --mode strict
/steer
```

Each slash declares one content policy:

```text
none   following content is rejected
input  following content is parsed as Part[]
```

For example, `/help` accepts no content, while `/run` and `/steer` may accept
input content.

### Shell

A shell command starts with `!`:

```text
!git status
```

The first line is the shell command. Following content is passed as literal
stdin and is not parsed as content. Shell commands do not create execution
records.


## Content

Content consists of text, prompts, and includes. Prompts and includes are
recognized only on standalone lines and are disabled inside fenced code blocks.

### Text

Ordinary multiline input remains one text block. Newlines do not create parts
by themselves.

### Prompt

A registered custom slash name is a prompt:

```text
/review src/app.py "only errors"
```

It renders a prompt template and replaces the prompt line with the rendered
content. Prompts may appear anywhere in content, including the first
line.

Attached content uses the same inline or indented syntax as agic content:

```text
/review src/app.py: Focus on cancellation.

/review src/app.py:
  Focus on cancellation and event ordering.
  @docs/executor.md
```

Without a colon, the prompt has only parameters. With a colon, the parsed
content becomes its `_ : Part[]` input. The prompt owns only that explicit
content and never implicitly consumes the rest of the submission.

Rendered prompt content may contain includes, but it is not
dispatched again as a built-in or shell command.

### Include

An include references one file on a standalone line:

```text
@README.md
@"path with spaces/image.png"
```

It replaces the line with the corresponding part. Includes may appear anywhere
in content and do not own attached content.


## Dispatch

The first non-empty line is resolved in this order:

```text
reserved slash name   -> slash
!                     -> shell
registered slash name -> prompt within content
otherwise             -> content
```

After dispatch, content is parsed in source order:

```text
text
prompts
includes
```

Built-in slash names are reserved and cannot be registered as prompt names.
Unknown command lines are errors rather than implicit model input.


## Escapes

At a position where a prefix would otherwise be recognized:

```text
//text  literal /text
!!text  literal !text on the first line
@@text  literal @text
```

Escaping removes one prefix character before content parsing.


## Content Processing

Interactive chat first performs command dispatch. Other sources start with
content processing directly:

```text
parse text, prompts, and includes
render runtime template variables when the source permits them
expand registered prompts and their declared parameters
resolve includes with the source-specific file resolver
produce ordered Part[]
```

Prompt templates always render their own declared parameters. This does not
enable runtime-local interpolation in the surrounding chat, task, or chore
content. Content produced by template rendering is never redispatched as a
built-in or shell command.


## Run Output

An agic without `-> T` returns normal assistant content. A declared output type
selects validation and script rendering:

```text
Text                 raw text
Number               canonical decimal
Boolean              true or false
Json, struct, T[]     compact JSON
Part                  one JSON object
Part[]                one JSON array
```

Model messages are assembled from `Part[]`, but `Message` is not a Toolang
input or output type. The executor decodes the terminal assistant parts into
the declared value before updating the primary local.

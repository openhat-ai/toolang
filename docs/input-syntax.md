# Input Syntax

This document defines how interactive and authored text becomes content and
typed run input.


## Model

```text
Submission  = Command | Content
Command     = Slash | Shell
Content     = ContentItem+
ContentItem = Text | Prompt | Include
```

- `Submission` is one interactive chat action. Only submissions dispatch
  commands.
- `Content` is an ordered tree that renders to `Part[]`.
- `ContentBody` is source text owned by a declaration or document and parsed as
  `Content` under that source's profile.
- Executable arguments are typed runtime data: one optional primary value plus
  named parameters. They contain no source syntax and are not a standalone
  execution object.

The source terms describe different stages:

- Chat input is a `Submission`.
- Task and chore inputs are document `ContentBody` values that later become
  executable arguments.
- An agic body is model-call content evaluated inside a run.
- A flow body is `FlowStmt[]`, not content text.
- A flow statement body is either a `ContentBody` or nested `FlowStmt[]`, as
  defined by that statement.

`Part[]` is ordered multimodal content. Plain multiline text remains one
`TextPart`; line breaks alone do not create parts. A prompt has slash-like
spelling but is a content item, not a command.


## Source Profiles

| Source | Entry | Built-in slash | Shell | Template scope | Produces |
| --- | --- | --- | --- | --- | --- |
| CLI chat | Submission | yes | execute | none | command or executable arguments |
| WebUI chat | Submission | yes | reject | none | command or executable arguments |
| task/chore body | ContentBody | no | text | none | executable arguments |
| agic body/message | ContentBody | no | text | `_` and declared params | ModelCall content |
| flow inline agic | ContentBody | no | text | `_` and declared params | generated AgicDecl |
| flow `let` | ContentBody | no | text | locals and runtime | Local |
| flow `ask` | ContentBody | no | text | locals and runtime | HumanCall content |
| prompt template | ContentBody | no | text | declared params and `_` | expanded Content |

`text` means that a leading `!` is ordinary content. A WebUI rejects shell
syntax instead of executing it on the server or sending it to the model.

Includes resolve relative to the owning task, chore, `.too` source, or prompt
definition. Browser sources may resolve only uploaded or otherwise authorized
references. Prompt templates see only declared parameters and explicit `_`
input. Instruct and context templates see only the flat runtime variables
supplied by the executor.


## Submission

Only the first non-empty line is dispatched:

```text
reserved slash name   -> Slash
!                     -> Shell
registered slash name -> Prompt within Content
otherwise             -> Content
```

Built-in slash names are reserved. Unknown command lines are errors rather than
implicit model input.

### Slash

A built-in slash declares whether following content is rejected or parsed as
input:

```text
/help
/run review --mode strict
/steer
```

For example, `/help` accepts no content, while `/run` and `/steer` may accept
`Part[]` input.

### Shell

A CLI shell command starts with `!`:

```text
!git status
```

The first line is the command. Following text is literal stdin, not `Content`.
Shell commands do not create execution records.

### Escapes

At a position where a prefix would otherwise be recognized:

```text
//text  literal /text
!!text  literal !text on the first line
@@text  literal @text
```

Escaping removes one prefix character.


## Content

Prompts and includes are recognized on standalone lines and are disabled inside
fenced code blocks. Text may be interleaved with any number of them.

### Text

Ordinary multiline input remains one text item with its authored line breaks.

### Include

An include replaces one standalone line with a file part:

```text
@README.md
@"path with spaces/image.png"
```

It does not own attached content.

### Prompt

A registered custom slash invokes a prompt template:

```text
/review src/app.py "only errors"
```

It may appear anywhere in `Content`. Optional input has an explicit boundary:

```text
PromptInput = ":" InlineText | ":" NEWLINE IndentedContent
```

The delimiter is the first unquoted colon after the arguments. Without it, the
prompt receives only its declared parameters. Inline input ends with the line;
indented input ends at the first non-empty dedented line.

```text
/review src/app.py: Focus on cancellation.

/review src/app.py:
  Review event ordering.
  @docs/executor.md
  /rules strict:
    Ignore formatting issues.

This text is outside the prompt input.
```

Indented input is `Content`, so prompts may contain includes and nested prompts.
A prompt never consumes following sibling content implicitly.

Prompt composition follows these rules:

- expansion is depth first
- rendered output is parsed in content-only mode
- rendered output may contain prompts and includes, but not dispatch commands
- each prompt has its own parameter and `_` scope
- direct and indirect cycles are errors; repeated sibling calls are allowed
- errors report the prompt call stack

Implementations apply cumulative limits to one expansion. Recommended defaults
are depth 16, 64 prompt calls, 256 output parts, and 1,000,000 text characters.
Include resolvers enforce separate access and byte limits.


## Processing

Interactive chat dispatches its submission first. Every content source then
uses the same pipeline under its source profile:

```text
parse Content
render permitted template variables
expand prompts depth first in content-only mode
resolve includes
coalesce adjacent text into Part[]
```

The consumer determines what happens next:

```text
chat/task/chore Content -> Part[] -> signature coercion -> executable arguments
agic ContentBody        -> Part[] -> Message -> ModelCall
flow inline agic        -> generated AgicDecl -> child run
flow let ContentBody    -> Part[] -> Local
flow ask ContentBody    -> Part[] -> HumanCall
prompt ContentBody      -> expanded Content -> enclosing pipeline
```

Prompt expansion does not enable local interpolation in chat, task, or chore
content and never redispatches built-in or shell commands.


## Executable Arguments

Agics and flows use `_` as their primary input parameter:

```too
agic chat:                         # implicit _: Part[]
agic ping():                       # no primary input
agic review(_):                    # explicit _: Part[]
agic parse(_: Json, mode: Text):

flow research(_: Text) -> Report:
```

Omitting the parameter list implies `_ : Part[]`. `()` accepts no primary
input. An untyped parameter defaults to `Part[]`.

Content first renders to `Part[]`, then the calling surface coerces it to the
declared type:

```text
Part[]     preserve ordered parts
Part       require exactly one part
Text       require text-only content and preserve authored lines
Number     parse one canonical number
Boolean    parse true or false
Json       parse one JSON value
Struct/T[] parse JSON and validate the declared type
```

No coercion may discard a non-text part. Invalid input is rejected before
`run_begin`.

The executor initializes `_` from the primary value and named locals from the
parameters. `Message` belongs to model calls and is not part of executable
arguments.

Value type and flow shape are independent. A `Part[]` or `T[]` value starts as
one `shape=item` local and becomes `shape=list` only through an explicit flow
operation.


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
input or output type. The executor decodes terminal assistant parts before
updating `_`.

# Content

Content is Toolang's authored text format for messages, prompts, and caller
input. It expands reusable prompts and file references into ordered message
Parts. [Call Input](call-input.md) owns invocation envelopes and coercion;
[Chat](chat.md) owns session commands. Content evaluation does not execute a
runnable or change session settings.

## Syntax

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

Expansion order and nesting restrictions are defined below.

Prompt calls use dollar directives. Shell commands must single-quote
dollar-prefixed input, for example:

```sh
too run review.too main -- '$review focus=security'
```

## Expansion and evaluation

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

Text becomes a text Part; an include contributes one Part. Prompt expansion
happens before the combined source is parsed as Content, so the final position
of a marker determines whether it is structural. Native `Part` and `Part[]`
values keep their parts; other interpolated values render as canonical text or
compact JSON. [Program](program.md#structured-types) owns template lookup rules.

Authored sources and prompt provenance remain available in Run controls, while
resolved Parts drive recall and rerun. Model Steps retain the normalized
ModelCall. Evaluating an attachment supplies content, not workspace authority;
[file inputs](scripts.md#file-inputs) owns path origins and client capture.

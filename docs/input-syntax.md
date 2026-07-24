# Input Syntax

This document defines how interactive and authored input becomes a canonical
percept, how that percept is coerced to an executable input type, and how an
execution result is coerced to its declared output type.


## Terms

```text
ContentBody = ContentItem+
ContentItem = TextChunk | PromptCall | IncludeRef
```

- `ContentBody` is an ordered sequence of authored `ContentItem` values owned by
  a declaration, document, or caller.
- `TextChunk` is literal authored text.
- `PromptCall` invokes one authored prompt template.
- `IncludeRef` resolves one authorized resource to a `PerceptPart`.
- `Input perceiving` interprets supported input as one ordered `Percept`.
- `Input coercion` converts a `Percept` into the primary Toolang value declared
  by an executable signature.
- `Output coercion` converts the final executable value into the output type
  declared by that signature.
- Executable arguments are typed runtime data: one optional primary value plus
  named parameters. They contain no source syntax and are not a standalone
  execution object.

The package-level protocol vocabularies are:

```text
PerceptPart = TextPart | ImagePart | AudioPart | DocumentPart
Percept     = PerceptPart[]
MessagePart = PerceptPart | ToolCallPart | ToolResultPart
Message     = { role: MessageRole, parts: MessagePart[] }
```

The Toolang language deliberately keeps the shorter structural types:

```text
Part    = one PerceptPart
Part[]  = one Percept
```

`lang` AST nodes therefore continue to carry names such as `Part` and
`Part[]`, and an omitted executable signature still implies `_ : Part[]`.
Packages outside `toolang.lang` use `PerceptPart` and `Percept` for the same
runtime values. `MessagePart` belongs to model messages, tool interaction,
execution events, and durable conversation history. Tool calls and tool results
cannot be authored in a `ContentBody`.

The canonical message roles constrain that wider union:

```text
user      -> PerceptPart*
assistant -> (PerceptPart | ToolCallPart)*
tool      -> ToolResultPart+
```

Canonical `Message` validation rejects a role/part combination that does not
match this model. An adapter never silently converts a tool call or tool result
to text.

The four provider-neutral percept parts are:

- `TextPart` carries text.
- `ImagePart` carries an image URL or provider file id plus optional display
  metadata.
- `AudioPart` carries base64 data and its format. Provider output may also set
  `transcript` on the same part.
- `DocumentPart` carries document data, a document URL, or a provider file id
  plus optional filename and media type.

There is no `JsonPart`. A language `Json` value used in a `ContentBody` becomes
canonical JSON text, while a JSON file is a `DocumentPart` with
`media_type="application/json"`. Tool JSON remains structured inside
`ToolCallPart` or `ToolResultPart`.

Canonical percept parts may carry inline binary data. This design does not
introduce a blob store: callers, execution, and adapters preserve the supplied
values, while binary extraction and deduplication remain a separate future
storage concern.

The same source form appears at different runtime boundaries:

- Chat, task, and chore inputs are `ContentBody` values that later become
  executable arguments.
- An agic body is model-call content evaluated inside a run.
- A flow body is `FlowStmt[]`, not content text.
- A flow statement body is either a `ContentBody` or nested `FlowStmt[]`, as
  defined by that statement.

At runtime, one language `Part[]` value is one ordered multimodal `Percept`.
Plain multiline text remains one `TextPart`; line breaks alone do not create
parts. A `PromptCall` uses slash-prefixed syntax inside a `ContentBody`.

Already constructed `Percept` values are canonical data rather than source
text. Input perceiving preserves them as supplied. For example, a structured
`TextPart("@README.md")` remains literal text rather than becoming an
`IncludeRef`.


## Source Profiles

| Source | Available values | Produces |
| --- | --- | --- |
| CLI/TUI input | none | primary `Percept` |
| WebUI input | none | primary `Percept` |
| task/chore body | none | primary `Percept` |
| agic body/message | `_` and declared params | ModelCall content |
| flow inline agic | current flow locals | generated AgicDecl |
| flow `let` | current flow locals | Local |
| flow `ask` | current flow locals | HumanCall content |
| prompt template | declared params and `_` | `Percept` segment |

Sources with no available runtime values treat `{{name}}` as literal authored
text. Agic bodies, applicable flow statement bodies, and prompt bodies resolve
the values listed in their source profile while being perceived.

`IncludeRef` values resolve relative to the owning task, chore, `.too` source,
or prompt definition. Browser sources may resolve only uploaded or otherwise
authorized references. Prompt templates see only declared parameters and
explicit `_` input. Prompt parameters are `Text`; prompt `_` input is `Part[]`.

Instruct and context declarations are text templates rather than `ContentBody`
values. They see only the flat runtime variables supplied by the executor and
accept text-compatible values only.


## Content Body

`PromptCall` and `IncludeRef` items are recognized on standalone lines and are
disabled inside fenced code blocks. `TextChunk` values may be interleaved with
any number of them.

### Text Chunk

Ordinary multiline input remains one text item with its authored line breaks.

### Include Reference

An `IncludeRef` replaces one standalone line with one resolved `PerceptPart`:

```text
@README.md
@"path with spaces/image.png"
```

The resolver classifies the referenced content by trusted media metadata, with
the extension as a fallback:

- text-like content becomes `TextPart`
- image content becomes `ImagePart`
- audio content becomes `AudioPart`
- supported text and document files become `DocumentPart`
- unsupported binary, archive, and video content is rejected until it has a
  dedicated percept part

An `IncludeRef` does not own attached sibling content. The resolver decides
whether the resulting percept part carries inline data, an authorized URL, or
a provider file id.

### Prompt Call

A `PromptCall` invokes a named prompt template:

```text
/review src/app.py "only errors"
```

It may appear at any item position in a `ContentBody`. An optional `:` attaches
either the rest of the current line or an indented `ContentBody`. The delimiter
is the first unquoted colon after the arguments. Without it, the prompt receives
only its declared parameters. Inline input ends with the line; indented input
ends at the first non-empty dedented line.

```text
/review src/app.py: Focus on cancellation.

/review src/app.py:
  Review event ordering.
  @docs/executor.md
  /rules strict:
    Ignore formatting issues.

This text is outside the prompt input.
```

Indented input is a `ContentBody`, so prompts may contain `IncludeRef` and
nested `PromptCall` items. A prompt never consumes following sibling content
implicitly.

Prompt composition follows these rules:

- nested prompts are resolved depth first
- a prompt body may contain nested prompt calls and include references
- each prompt has its own parameter and `_` scope
- direct and indirect cycles are errors; repeated sibling calls are allowed
- errors report the prompt call stack

Implementations apply cumulative limits to one composition. Recommended
defaults are depth 16, 64 prompt calls, 256 output parts, and 1,000,000 text
characters. Include resolvers enforce separate access and byte limits.


### Escapes

At a position where a content prefix would otherwise be recognized:

```text
//text  literal /text
@@text  literal @text
```

Escaping removes one prefix character.


## Input Perceiving

Input perceiving gives every supported input source the same result: one
ordered `Percept`. Each `ContentBody` follows the source profile that owns it.

```text
plain text          -> Percept
ContentBody         -> Percept
structured parts    -> Percept
```

A `ContentBody` may contain prompt calls, include references, and references to
values allowed by its source profile. A language `Part` or `Part[]` value
retains its percept parts and its position among surrounding text. Scalar and
structured language values use their canonical textual forms.

For example, using a language `Part[]` value—represented at runtime as a
`Percept` containing `TextPart("this image")` and one `ImagePart`—in:

```text
Review {{_}} carefully.
```

produces the conceptual sequence:

```text
TextPart("Review this image")
ImagePart(...)
TextPart(" carefully.")
```

Text already stored inside a supplied `TextPart` is literal canonical data.
It is not parsed again for prompt calls, include references, or template tags.


### Typed Values

Input perceiving follows the declared Toolang type rather than guessing from
the Python representation of a runtime value:

| Toolang type | Representation in the resulting Percept |
| --- | --- |
| `Text` | Text in the surrounding `TextPart` |
| `Number` | One canonical number as text |
| `Boolean` | `true` or `false` as text |
| `Json` | Compact JSON text |
| `Part` | Exactly one `PerceptPart` |
| `Part[]` | One ordered `Percept` |
| `T[]` | Compact JSON text when used directly |
| declared struct `S` | Compact JSON text when used directly |
| missing optional value | Empty text |

Mustache-style sections preserve declared element and field types:

- iterating `T[]` evaluates each item as `T`
- accessing a struct field uses the field type from its StructDecl
- using a struct or ordinary array directly produces compact JSON text
- iterating `Part[]` exposes its language `Part` values

Declaration wins over value shape. A value declared as `Json` remains JSON text
even when it is an object containing `"type": "image"`. Only a value declared
as `Part` or `Part[]` can become canonical percept parts.

Input perceiving therefore receives values together with their Toolang type
names. Type information comes from:

- executable primary input and named Parameter declarations
- the type carried by a flow Local
- fields in StructDecl
- prompt parameters, which are `Text`
- prompt `_` input, which is `Part[]`

An untyped executable parameter defaults to `Part[]`. MessagePart-only values
such as ToolCallPart and ToolResultPart are not Toolang values and cannot
appear in an authored `ContentBody`.


## Ownership

- `toolang.base` owns canonical `PerceptPart`, `Percept`, `MessagePart`, and
  `Message` values, role/part validation, and their data representation.
- `toolang.lang` owns pure `ContentBody` parsing, input perceiving, input
  coercion, and output coercion. It receives prompt and include resolvers rather
  than reading catalogs or arbitrary files.
- Model adapter plugins map canonical messages to provider payloads and back.
  They do not parse or perceive a `ContentBody`.
- `toolang.execution` preserves canonical percepts through run inputs,
  controls, locals, events, records, model calls, and outputs. It does not
  reinterpret a `Percept` as source text.
- CLI, API, TUI, task, chore, and file callers own input acquisition and supply
  the source profile and resolver authority appropriate to their environment.


## Runtime Use

The consumer determines what happens after input perceiving:

```text
chat/task/chore body    -> Percept -> RunSpec.input
agic ContentBody        -> Percept -> Message -> ModelCall
flow inline agic        -> generated AgicDecl -> child run
flow let ContentBody    -> Percept -> Local(type=Part[])
flow ask ContentBody    -> Percept -> HumanCall
prompt ContentBody      -> Percept -> enclosing Percept
```

Prompt invocation does not expose local values from a chat, task, or chore
body. Each invoked prompt receives only its own declared `Text` parameters and
explicit `_ : Part[]` input; it does not inherit values from the enclosing
source.

The same model applies to interactive and non-interactive callers:

| Caller | Input behavior | Include authority |
| --- | --- | --- |
| CLI/TUI | Perceive the supplied ContentBody | Explicitly allowed local paths |
| WebUI raw text | Perceive the supplied ContentBody | Uploaded or otherwise authorized references only |
| WebUI structured input | Preserve the supplied Percept | Already resolved |
| task/chore | Perceive the document ContentBody | Paths relative to the owning document |
| file inbox | Construct a PerceptPart or perceive an accompanying ContentBody | The accepted inbox file |
| agic or flow statement | Perceive its ContentBody | Paths relative to the owning `.too` source |
| prompt | Perceive its body with prompt values | Paths relative to the prompt definition |

Each caller supplies an Include resolver that enforces its own authority and
byte limits. Input perceiving does not read arbitrary filesystem paths by
itself.


## Input Coercion

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

Input perceiving first produces a `Percept`, which is the runtime
representation of language `Part[]`. After resolving the runnable, input
coercion converts that percept to the declared primary type:

```text
Part[]     preserve ordered parts
Part       require exactly one part
Text       require text-only content and preserve authored lines
Number     parse one canonical number
Boolean    parse true or false
Json       parse one JSON value
S/T[]      parse JSON and validate the declared type
```

No coercion may discard a non-text part. Invalid input is rejected before
`run_begin`.

The original `Percept` remains the durable start input and user-message
content.
The executor initializes `_` from the coerced primary value and named locals
from the validated arguments. Locals retain their Toolang type names so later
flow statement bodies can perceive them without inspecting value shape.
`Message` and `MessagePart` belong to model calls and are not executable
argument types.

Value type and flow shape are independent. A language `Part[]` value backed by
one `Percept`, or an ordinary `T[]` value, starts as one `shape=item` local and
becomes `shape=list` only through an explicit flow operation.


## Output Coercion

Output coercion converts an agic or flow's final value to its declared output
type. An agic without `-> T` keeps its normal assistant `Percept`.

```text
Text                 require text
Number               parse or validate one number
Boolean              parse or validate one boolean
Json                  parse or validate one JSON value
S/T[]                 parse or validate the declared structured type
Part                  require exactly one PerceptPart
Part[]                preserve one ordered Percept
```

For an agic, output coercion applies to terminal assistant content. For a flow,
it applies to the final primary local. The coerced result becomes `_` for its
caller.

`Message` is not a Toolang input or output type. Language `Part` and `Part[]`
outputs remain `PerceptPart` and `Percept` values internally. CLI and protocol
callers serialize the coerced value for their own external boundary.

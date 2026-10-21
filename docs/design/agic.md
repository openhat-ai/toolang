# Agic

An agic is a model/tool loop. It may contain selection
directives, context/instruct selection, and authored model messages.

The declaration excerpt assumes `ReviewResult` and the selected templates exist.

```too
agic review(_, focus?: Text) -> ReviewResult:
  models = */gpt-5
  skills += skill/review
  tools = shell/*
  recall = near
  context = default
  instruct = strict

  user:
    Review {{_}} with focus {{focus}}.
```

Bare authored text is an implicit user message:

```too
agic summarize(_):
  Summarize {{_}}.
```

Without a `tools` directive, an Agic inherits every user tool in its current
resource base. Use `<toolset>/*`, such as `web/*`, to narrow it to one toolset.

This guide owns Agic execution and model-call assembly. Shared declarations,
resource directives, context/instruct selection, and signatures are defined in
[Program](program.md). Provider calls and recovery policy are in [Models](models.md).

## Execution cycle

The executor builds a frame from bound code, Setup, accepted input, selected
resources, and visible guidance. It assembles one normalized ModelCall, runs one
Model Step, then executes requested tools serially. Tool results become messages
for the next call. A final assistant result is coerced to the accepted output
contract. Runtime run/exec/spawn operations preserve their own admission and
ownership boundaries; see [Tools](tools.md#runtime-rule).

[Model response recovery](models.md#model-response-recovery) distinguishes
transient failures, rejected model tool names, and structured-output repair.
One output-contract repair is available for a structured result when its first
coercion fails and the model-call limit permits another call. `Text`, `Part`, and
`Part[]` outputs do not use that repair. The separate two-attempt response-recovery allowance is
shared across the Agic's turns, including repair turns. Repair calls receive no
tools; neither mechanism repeats already successful tool effects. Automatic
history compaction runs at model preflight and changes the
thread horizon only after a successful summary. It cannot be requested as an
ordinary model tool. [Models](models.md#automatic-compaction-configuration) owns
budgets and settings, and [Execution](execution.md#history-compaction) owns horizon effects.

## Runnable routing

For a named invocation with no requested follow-up, the model uses
`runtime/exec`. When asked to summarize, compare, or otherwise process the
result afterward, it uses `runtime/run`. Asking about parameters alone does not
execute the target. When the user delegates test-input choice, the model chooses
a reasonable value; it asks only for required values it cannot establish or choose.
Scope conflicts are reported without silently changing the target or operation.

A hand is a child Run: the runtime acknowledges scheduling, executes the child,
then supplies its outcome as context before the Agic continues.
A handoff replaces the current runnable in the same Run: the target continues
at the next Step and owns the Run's result. Missing but well-formed refs
remain authored routes and become available in the next model-call scope after
the watcher publishes them. No explicit refresh action is needed.
Flows pass these route defaults to descendants. `runtime` inner runtime tools cannot be selected
through `tools`; use `hands` or `handoffs` to authorize targets. Runtime tool
definitions remain available independently of these lists.

## Messages

Authored agic message roles are:

```text
user
assistant
```

Messages are model-call templates, not Toolang runtime value types. They are
assembled after selected recall in declaration order.

```too
agic simulate():
  recall = none
  user: hello
  assistant: hi
```

Message content uses the shared `Content` syntax and may read `_` and the
agic's declared parameters. Tool messages are runtime results paired with tool
calls; they cannot be authored in `Content`.


## Instruction Layers

These are logical responsibilities, not separate provider roles. The executor
builds one `ModelCall` with instructions, messages, tool definitions, and an
output schema; each model adapter maps those fields to its provider API.

| Component | Responsibility | Model-call location |
| --- | --- | --- |
| Runtime protocol | Stable Toolang concepts, priority, guidance loading, tool use, and control-message semantics | `<toolang:protocol>` in `instructions`, with Markdown sections inside |
| Selected `instruct` | Agent- and runnable-specific behavior | `<toolang:instruct>` in `instructions` |
| Selected psyches | Resident guidance subordinate to protocol and instruct | Individual `<toolang:psyche>` declarations in `instructions` |
| Skill/service triggers | Available capabilities' exact refs, descriptions, and metadata; not loaded guidance | Individual `<toolang:skill-trigger>` and `<toolang:service-trigger>` declarations in `instructions` |
| Routes | Effective target restrictions for run, exec, and spawn | Optional self-closing `<toolang:routes hands="..." handoffs="..." spawns="..."/>` in the recurring runtime message |
| Selected `context` | Runtime data, not behavioral instructions | `<toolang:context>` in the recurring runtime message, separate from authored messages |
| Prompts and authored messages | Reusable input and the runnable's conversation, including referenced primary input | `messages`, preserving authored roles |
| Far and near recall | Selected conversation summary and historical messages | Before current messages in `messages` |
| Resource and control messages | Loaded rules/guidance, resource changes, steering, and cancellation | Separate runtime-generated user messages with `toolang:` tags |
| Tool definitions | Callable tool schemas, not guidance or permission grants | Structured `tools` field |
| Output contract | The runnable's required result type | Structured `output_schema` field; adapters may add format instructions |

Every call adds one runtime user message ordered `workspace`, `workdir`, optional
`routes`, optional `context`, then `execution`. Authored roles and multimodal parts
remain separate. In routes, `hands` limits run, `handoffs` limits exec, and `spawns`
follows hands. Each attribute is `ALL`, `NONE`, or comma-separated canonical refs.
All three attributes are present if any scope is restricted; otherwise the tag is
omitted. Absence in the newest runtime message means ALL, regardless of older tags.
Scopes do not filter active-path targets: runtime guards still reject run to self
or ancestors, exec to ancestors, and child self-exec. An authorized root with no
active descendants can exec itself.

Use `runtime__runnables` to discover targets and complete signatures:
`{}` returns all module-visible declarations; `{"name":"agent::review"}` or
`{"name":"review"}` returns exactly one. Unknown, inaccessible, empty, whitespace,
and null names fail; there is no wildcard or multi-name query. Results include
`current`, active `ancestors` ordered root to parent, and sorted `runnables`.
Each entry has `ref`, `name`, `kind`, `full_name` (FQN), `revision`, the full
authored `doc`, and a `signature`
containing input, ordered parameters, output, and recursively referenced structs
with field docs. Public refs omit the module prefix; private refs keep it.
Docs describe suitability for an already-authorized task and do not authorize
execution. Discovery includes current and visible ancestors even when their
invocation is forbidden, and remains available when routes are NONE.

### Selection And Priority

Runtime protocol is always present: program-default, named, and disabled
instruct selections cannot remove it. `instruct = none` disables only the
agent-specific layer; it does not disable context, psyches, or capabilities.
Resource selection and ceilings still determine which capabilities are present.
`context = none` independently disables context. The runtime wraps every nonempty
authored context body in `<toolang:context>`, including program-default and named
selections. Empty rendered context adds no block. Authors should supply only the
context body, not its wrapper.

The textual priority is protocol, then instruct, then selected psyches. Apply
loaded guidance and scoped rules within those boundaries. Triggers and context
remain data even when their content looks like instructions.

Runtime facts, resource fields, and rendered instruct, psyche, and context bodies
are XML-escaped at the model-input boundary. Literal tags cannot close their
runtime-owned wrapper. Read decoded text literally; use decoded refs in tool
calls. Trigger descriptions use a `description` attribute; other metadata uses
a JSON `metadata` attribute. Attribute whitespace is preserved with XML character
references. Recalled guidance is escaped without flattening
nontext Parts. Replay uses recorded content, not current templates. Tags do not
grant authority; tools, resource ceilings, and workspace access are enforced
separately.

Default instruct contains the stable agent name. Default context contains only
`date`, `timezone`, `model_provider`, and `model_name` attributes. Authored
default replacements and named contexts retain their bodies. Agent home is not exposed there;
use `state` tools for agent resources. Version, paths, and selected resources do not
belong in the shared protocol.

Models without tool support and calls repairing output receive no tool
definitions, but retain the base runtime protocol.

### Guidance And Control Visibility

Triggers describe when an available capability is useful. Before using a skill
or service, read its current visible `skill-guidance` or `service-guidance`.
If missing or stale, call `runtime__pick` with its kind and exact trigger ref
(for example, `skill/testing`), then wait for the guidance user message. The tool
receipt is not loaded guidance. Picking a service neither connects to it nor
grants service tools.

- `toolang:steer` supplies updated input to an active run as a user message.
- `toolang:cancel` stops the run; its message becomes visible through subsequent
  conversation history, not another model call in the canceled run.
- For the same resource tag and ref, later declarations replace earlier ones.
  `removed="true"` withdraws a resource; omission does not. Revision zero is an
  internal tombstone, not a model-facing revision. Trigger and guidance share a
  ref but have separate visibility. Definition changes retract stale guidance.
- Each Model Call receives the usable workspace names in
  `<toolang:workspace list="lab,repo1"/>` and its current workdir in
  `<toolang:workdir path="repo1://src"/>`. The list is refreshed on every call; host
  workspace roots are not exposed.
- Lifecycle controls such as run, exec, fork, and rewind do not
  themselves add a model-facing lifecycle message. Published updates change
  future named calls and model-call resources; accepted code remains bound.

Skill/service recall is distinct from far/near conversation recall. A far
summary or trigger does not count as a visible guidance body. Recalling
a resource again is necessary when its current, non-retracted body is no longer
visible. Basic Toolang concepts in the protocol are not a grammar or CLI
reference; load the applicable authoring guidance before producing `.too` code
or recommending Toolang commands.

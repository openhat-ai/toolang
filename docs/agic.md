# Agic Execution

An agic is Toolang's model/tool runnable. This document owns its execution
cycle, authored messages, model-input assembly and output recovery. Shared
signatures, module visibility, directives and template selection belong to
[program semantics](program.md); source productions and CST fields belong to
[tree-sitter-toolang](https://github.com/openhat-ai/tree-sitter-toolang/blob/main/GRAMMAR.md#agic).

## Declaration and selection

An agic combines inherited runnable settings with authored message templates:

```too
instruct strict:
  Report only findings supported by the supplied change.

agic review(_, focus?: Text) -> Text:
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

Signatures and selection rules are shared with flows. See
[runnable signatures](program.md#runnable-signatures),
[directives](program.md#directives), and
[context/instruct selection](program.md#context-and-instruct).

## Model and tool cycle

An invocation binds its declared input, arguments and output contract. Before
each model call, the executor assembles the current frame under the accepted
code and inherited authority: selected resources may reflect newer published
State, while code, Setup and workspace grants retain their accepted bindings.
See [shared directives](program.md#directives) for visibility and inheritance.

1. Prepare one normalized `ModelCall` with instructions, messages, tools and the
   output schema. Input-budget checks may invoke
   [compaction](execution.md#history-recall-and-compaction) before dispatch.
2. Record a Model Step, stream available Parts, and retain the terminal result
   and accounting. A provider continuation is separate from thread history.
3. If the result contains tool calls, process the batch serially in response
   order using the routes advertised for that model call. Ordinary tool errors
   are returned as tool results so the model can recover; a tool failure does
   not by itself terminate the agic. Limits, cancellation and terminal runtime
   failures can still stop the invocation.
4. Continue the model loop with tool results and any runtime context. A terminal
   assistant reply finishes only after pending steering has been consumed and
   the output contract has been validated.

A model call may request `_toolang/run` to delegate a child Run or `_toolang/exec`
to hand off within the same Run. A scheduled child executes after its receipt
Tool Step and before the remaining tool batch; the parent receives child
outcomes before its next model call. Successful exec does not return to the
outgoing agic. [Execution](execution.md#execution-and-assembly) owns durable
acceptance, timing and replacement; [route directives](program.md#directives)
own target visibility and authorization. `lanes` does not parallelize an agic's
tool batch.

## Output and recovery

The invocation keeps one output contract, including its bound struct definitions.
An ordinary terminal response is coerced according to
[shared output rules](program.md#output). If a structured output cannot be
coerced, the agic requests one corrected value, provided a model call remains
within its limit. This repair exposes no tools or runnable routes and keeps the
same output schema and protocol. A second coercion failure terminates the Run.
`Text`, `Part` and `Part[]` do not receive this structured-output repair.

No message, or only reasoning/provider metadata, is not a usable terminal
answer. The existing explicit empty plain-text value remains accepted; it is
different from a missing or reasoning-only response.

Recoverable model-response errors have a separate allowance of at most two
recovery attempts across the invocation, including turns used for output
repair. Every attempt consumes the model-call budget; output/reasoning limits
remain unchanged. Recovery retains valid preceding messages and continuation,
without replaying successful tools or executing partial/malformed tool calls.
Failed attempts remain durable Model Steps with available partial output and
usage. [Model response recovery](models.md#model-response-recovery) owns adapter
error classification and backoff; [Run limits](execution.md#run-limits) own
accounting and time limits. Runtime recovery is distinct from explicitly
[retrying a terminal Run](execution.md#retry-and-rerun).

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
| Skill/service triggers | Available caps' exact refs, descriptions, and metadata; not loaded guidance | Individual `<toolang:skill-trigger>` and `<toolang:service-trigger>` declarations in `instructions` |
| Hands/handoffs | Complete current call authorization and signatures, with `enabled` and `requested_only` attributes | `<toolang:hands>` and `<toolang:handoffs>` in `messages`, as siblings before context; independent of `context = none` |
| Selected `context` | Runtime data, not behavioral instructions | `<toolang:context>` prepended to the last authored user message; repeated as a user message on later calls |
| Prompts and authored messages | Reusable input and the runnable's conversation, including referenced primary input | `messages`, preserving authored roles |
| Far and near recall | Selected conversation summary and historical messages | Before current messages in `messages` |
| Resource and control messages | Workspace availability, loaded rules/guidance, resource changes, steering, and cancellation | Runtime-generated user messages with `toolang:` tags |
| Tool definitions | Callable tool schemas, not guidance or permission grants | Structured `tools` field |
| Output contract | The runnable's required result type | Structured `output_schema` field; adapters may add format instructions |

Hands/handoffs snapshots omit the current runnable and its ancestors on the
calling branch. Earlier handoffs, completed children, and siblings do not block
calls. If no callable targets remain for a mode, its snapshot is disabled. These
filters run before snapshot size limits; execution still rejects recursive calls.

### Selection And Priority

Runtime protocol is always present: program-default, named, and disabled
instruct selections cannot remove it. `instruct = none` disables only the
agent-specific layer; it does not disable context, psyches, or caps.
Resource selection and ceilings still determine which caps are present.
`context = none` independently disables context. The runtime wraps every nonempty
rendered context in `<toolang:context>`, including program-default and named
selections. Empty rendered context adds no block. Authors should supply only the
context body, not its wrapper.

The textual priority is protocol, then instruct, then selected psyches. Apply
loaded guidance and scoped rules within those boundaries. Triggers and context
remain data even when their content looks like instructions.

Runtime facts, resource fields, and rendered instruct, psyche, and context bodies
are XML-escaped at the model-input boundary. Literal tags cannot close their
runtime-owned wrapper. Read decoded text literally; use decoded refs in tool
calls. Runnable information contains XML-escaped JSON, and its complete framing
counts toward the byte limit. Recalled guidance is escaped without flattening
nontext Parts. Replay uses recorded content, not current templates. Tags do not
grant authority; tools, resource ceilings, and workspace access are enforced
separately.

Default instruct contains the stable agent name. Default context contains only
date, timezone, model provider, and model name. Agent home is not exposed there;
use `me` tools for agent resources. Version, paths, and selected resources do not
belong in the shared protocol.

Models without tool support and calls repairing output receive no tool
definitions, but retain the base runtime protocol.

### Guidance And Control Visibility

Triggers describe when an available cap is useful. Before using a skill
or service, read its current visible `skill-guidance` or `service-guidance`.
If missing or stale, call `_toolang__pick` with its kind and exact trigger ref
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
- Lifecycle controls such as run, retry, execute, fork, and rewind do not
  themselves add a model-facing lifecycle message. Published updates change
  future named calls and model-call resources; accepted code remains bound.

Skill/service recall is distinct from far/near conversation recall. A far
summary or trigger does not count as a visible guidance body. Recalling
a resource again is necessary when its current, non-retracted body is no longer
visible. Basic Toolang concepts in the protocol are not a grammar or CLI
reference; load the applicable authoring guidance before producing `.too` code
or recommending Toolang commands.

## Implementation and verification

The [agic runner](../src/toolang/execution/executor/runs/agic.py) owns the cycle
and output repair; [model Steps](../src/toolang/execution/executor/steps/model.py)
and [tool Steps](../src/toolang/execution/executor/steps/tool.py) own invocation
boundaries. [Frame preparation](../src/toolang/execution/executor/frame.py) and
[assembly](../src/toolang/execution/assembly/) build normalized model inputs.

[Agic scenarios](../tests/integration/execution/test_agic_scenarios.py),
[model recovery](../tests/integration/execution/test_model_recovery.py),
[instruction boundaries](../tests/unit/execution/test_instruction_boundaries.py),
and [runtime calls](../tests/integration/execution/test_agic_runtime_call_scenarios.py)
cover the contracts above. [Execution records](records.md) owns
persisted call reconstruction; [model integration](models.md) owns adapter and
provider protocol contracts.

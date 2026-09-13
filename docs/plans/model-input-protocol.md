# Model input protocol

Approved for implementation. Make model inputs clear and cache-friendly while
preserving authorization, State adoption, recall, and recorded-call reconstruction.

## Approved assembly and recording revision

Assembly consumes one adopted AgentState, AgentSetup, Agic AST, bound input, and
executor-supplied history/control facts. Its main functions are instructions,
messages, tools, and output_schema. State already owns source merging, shadowing,
and effective module capabilities; consumers never repeat those operations.
Keep rendering in prompting.py, historical selection in history.py, individual
tool replies in tool_replies.py, and necessary pure helpers in utils.py. Executor
owns snapshot adoption, permissions, prompt provenance, persistence, and dispatch.

PromptInputs shares template variables and cached authored input within frame
construction; instructions does not return message-rendering intermediates.
Messages stages initial input, context, controls, and selected history together.
Its message buffer lives in assembly, but the executor owns and adopts each
candidate only after commit. Tools only projects already-selected definitions.
History selection groups messages, recording templates, and visible revisions,
with lazy tail caching shared across horizons.

Runtime and recorded messages retain role, content, optional tag, and optional
recall {ref, revision}. Content contains ordered segments: literal text, Parts,
and immutable references, including content hashes. A capability's effective ref
is kind/name, such as skill/testing, without source scopes or URIs. Triggers and
guidance share that resource ref; visibility is keyed by (tag, ref), never parsed
from XML. Ordinary text, tool receipts, and summaries do not establish recall.
Rules retain their workspace and normalized scope identity. Revision zero is a
persisted tombstone, rendered as removed="true". Withdrawals append new messages;
they never modify a previous ModelCall or its referenced contents.

Each durable call contains version, instructions, messages {head, delta}, tools,
output_schema, cont, and max_output_tokens. Version covers the entire durable
format. Delta is a message array, not a version/messages wrapper. Head identifies
the first Model Step in the same Run's current message sequence. Reconstruct a
call by concatenating the raw deltas from head through that call in numeric Model
Step order. A head points to itself and contains the complete selected baseline;
later calls retain that head and append only additions. Compaction or changed
history selection starts a new head while retaining current conversation.
Other call fields are current-row values, never inherited from the head.

Far/near selection remains agic policy and is absent from the durable call.
Record the selected messages as explicit content references, preserving roles,
Part boundaries, and recall annotations. History reads must not count imported
history or rebased messages as new conversation. Replay must not read current
State, reselect history, or infer message heads from control policy.

Assembly constructs messages and their recording data together without writes.
Executor commits their immutable content dependencies and Model Step begin before
adopting the candidate or dispatching. Failed preparation does not change visible
recalls. Preflight compares the calling ModelCall's visibility with the adopted
tool-step State; pending controls and completed loads are not model visibility.
The output schema remains fixed for the agic invocation, including repair.

Acceptance additionally covers strict codecs and head validation; source-free
refs; trigger/guidance isolation; replacement/removal/restoration; content hash
integrity; repeat compaction and history-policy changes; interruption and retry;
fork/rewind; nonduplicated history; stable old calls; and exact replay after
restart. Upgrade the store schema and reject incompatible databases untouched.

## Layout

Instructions use this order; repeat resource blocks as needed, without catalogs:

```xml
<toolang:protocol>Shared interpretation and rules.</toolang:protocol>
<toolang:instruct>Agent-specific behavior.</toolang:instruct>
<toolang:psyche ref="..." revision="...">Resident guidance.</toolang:psyche>
<toolang:skill-trigger ref="..." revision="...">When to use this skill.</toolang:skill-trigger>
<toolang:service-trigger ref="..." revision="...">When to use this service.</toolang:service-trigger>
```

Reserve `toolang:` for runtime tags. This is a prompt naming convention, not a
namespace parser; users need no `user:` prefix.
Escape attributes and literal bodies; preserve authored Markdown/code as content.
Tags identify protocol structure, not authority or proof of origin.

Protocol is always first, even with `instruct: none` or tools disabled. It uses
five main Markdown sections inside one `<toolang:protocol>` wrapper. Address the
LLM directly; explain what it receives and instruct it how to interpret and act:

- **Toolang:** purpose, motivation, .too program files, agics, flows, and caps.
- **Your role:** LLM/runtime responsibilities and State/Setup bindings.
- **Runtime contract:** tags, priority, messages, resource
  lifecycle, and structured tool inputs. Group full tags in a fenced XML example;
  quoted examples carry no resource authority or recall metadata.
- **Follow these rules:** symmetrical **Do** and **Don't** subsections for required
  actions and prohibitions.
- **Write Toolang programs:** apply only when asked to write or modify Toolang
  programs. Briefly require relevant guidance, version-matched references instead
  of guessed syntax, authorized source edits, validation, and State adoption. Link
  one `toolang-syntax` entry, caps files, and coding conventions; omit a separate
  flow-syntax link and embedded authoring tutorials.

Emit workspace declarations as `workspace-access` and rules as `workspace-rules`
in both XML and message metadata.

Keep version, date, paths, selected refs, and runnable facts outside protocol.
A general `run-info` block is deferred; do not introduce it in this scope.
Instruct must not repeat protocol. Every model call includes complete `hands`
and `handoffs` snapshots in messages, as siblings before the selected context.
Use `enabled="true"` with a JSON array of authorized targets and `enabled="false"`
with no body when that mode is unavailable. Each entry contains its exact ref,
bounded documentation, and signature (input, parameters, output, reachable structs).
The same signature projection supplies input-validation feedback. Entries do not
repeat actions; the containing tag identifies the mode. No per-target tags,
revision, removed attribute, or recall metadata. Only the latest runtime snapshots
apply to the current call; history never restores permissions. Context selection,
including `context: none`, does not suppress snapshots. Calls without runtime
tools, including output repair, disable both modes. Availability never adds tools.

Executor omits targets in the current or ancestor runnable lineages from snapshots,
using the same qualified identities as its execution-time recursion guard. Include
prior handoff targets in the same Run's lineage; completed children remain callable.
Filter before counting targets or bytes. Preserve the execution-time guard and
keep lineage knowledge out of State and assembly.

Render all remaining resolved targets or reject preparation with an actionable
error asking the author to narrow hands/handoffs. Preserve the 64-unique-target
and 32,768-byte limits; the byte budget includes both snapshots and escaped framing. Never
truncate a list, emit partial authority, or reinterpret budget omission as removal.
Keep instructions stable when only routes change. Persist snapshots as ordinary
message content, using the existing immutable delta/head reconstruction.

Keep the existing `You are the {{agent.name}} Toolang agent.` in
`defaults/instruct.md`; agent identity is stable and does not belong in context
or the shared protocol. `defaults/context.md` contains only:

```text
date: {{date}}
timezone: {{timezone}}
model_provider: {{model.provider}}
model_name: {{model.name}}
```

Remove `agent_home` from protocol and default context; use available `me` tools
for agent-home operations instead of exposing its path. Remove `model_family`
without introducing a replacement label. Preserve authored instruct/context
selection, including `none`; this changes defaults, not template selection.

## Resource and message rules

Tag names below omit the common `toolang:` prefix.

| Tags | Body and placement |
| --- | --- |
| `psyche` | Resident guidance in instructions; later declarations in user messages |
| `skill-trigger`, `service-trigger` | Resident purpose and usage conditions; later declarations in user messages |
| `hands`, `handoffs` | Complete per-call authorization snapshots before context in user messages; not recall resources |
| `skill-guidance`, `service-guidance` | Loaded bodies in user messages |
| `workspace-access`, `workspace-rules` | Workspace bindings and scoped rules in user messages |
| `context`, `steer`, `cancel` | Call data and control input in user messages, not replaceable resources |

- **Replace or retract:** declarations carry the full state, with a body only
  when needed. For the same tag/ref, the later declaration wins.
  Body-bearing declarations may include `revision`; bodyless ones need not.
  An empty, self-closing tag with `removed="true"` withdraws a resource;
  omit `revision` on withdrawals. Without `removed`, it remains available even
  without a body. Later declarations can restore availability; omission is not
  withdrawal. No catalogs, patches, or separate update messages.
- **Load before use:** using a skill requires its current, visible
  `skill-guidance`. If absent, stale, or retracted, call `_toolang__pick` with
  `kind="skill"` and the exact ref, then wait for the guidance message.
  Triggers, memory, far summaries, and pick receipts are insufficient. If loading
  fails or is unavailable, report the limitation, not successful skill use.
  Services follow the same rule; picking does not connect or authenticate them.
  Triggers guide capability selection; they do not execute anything automatically.
- **Separate availability from guidance:** retain skill/service recall targets for
  bodies and add distinct trigger targets. Triggers must not satisfy guidance
  visibility or pick deduplication. A definition change or withdrawal retracts
  stale guidance; loading the replacement still requires pick. Hash definitions,
  not only metadata.
- **Use current state:** reconcile initial instructions, selected history, and
  pending messages against adopted State; append necessary declarations last.
  Track visibility through structured references, never XML parsing or summaries.
  Ordinary conversation, tool messages, and multimodal Parts retain their forms.

## Workspaces

A workspace is an authorized named root, distinct from cwd and agent home.
Its ref is the configured name; file access uses
[workspace URIs](fs-workspace-uris.md). Rules use workspace name and normalized
directory path as their identity. Protocol explains URI addressing once;
workspace declarations are self-closing and need no body.

```xml
<toolang:workspace-access ref="project"/>
<toolang:workspace-access ref="project" removed="true"/>
```

Workspace tags have no body or revision. Internally, hash the captured name/root
binding, not the whole State; hide host paths and avoid filesystem probes.
Message assembly must make every currently available workspace visible before
each model call, including the first; no discovery tool call is required.
Reconcile visible/pending declarations in name order against the executor-supplied
adopted bindings. Emit missing/changed bindings, retract removed ones, and suppress
unchanged ones. Re-present bindings after compaction or `recall = none`.
Each child uses its own adopted State. Assembly does not scan or load rules.

Before workspace reads or writes, preflight ensures applicable rules are current
and visible to the model. Missing/stale visibility triggers honor/recall instead
of the requested operation; deliver rules to the model before it retries. Loading rules
alone is not visibility, and loading failure must not allow the operation.
Workspace visibility does not imply rules visibility; no applicable rules means
no rules-loading requirement.

Removal/remapping retracts old scoped rules; preflight discovers new applicable
rules only when needed. Withdrawal does not delete files or undo started tools.
Filesystem errors are not binding removals. Watcher edits alone do not emit
declarations or wake idle runs.

## Implementation boundaries

- `assembly/prompts/`: static `protocol.md` and
  `defaults/{instruct.md,context.md,compact.too}`. Format skill/service triggers
  directly from descriptions and metadata; no separate resource or tools templates.
- `assembly/prompting.py`: assemble instructions, messages, and structured tool
  definitions; reuse the language's output-schema function. The model step constructs
  `ModelCall` directly; adapters own provider-specific serialization.
- `assembly/history.py`: history selection and reconstruction.
  `assembly/utils.py`: pure formatting helpers, including control-message framing
  and steer/cancel wording. Keep output repair in the agic run, not prompt files.
- `execution/recall.py`: reconcile visible resource declarations, including
  required workspace declarations; no rules discovery.
- Executor frame/model-step boundaries own runtime facts, State reconciliation,
  and tool policy. Extend recall types/codecs with trigger targets and
  `WorkspaceRecallTarget(ref, kind="workspace")`; reuse existing recall controls,
  message buffering and durable deltas. Track declarations through each message's
  tag and direct recall ref/revision, not control lookup or parsed XML.
  No new event kind or synthetic Tool Step.
- Keep canonical 64-digit revisions and revision zero for internal recall and
  persistence. Render zero as `removed="true"`, not `revision="0"`; omit revision
  for bodyless declarations. Internal revisions still distinguish workspace remaps.
- Record framing before invocation; retries/interruption must not lose or duplicate
  deltas. Follow [message recording](model-message-recording.md): replay stored
  templates, never current State or new renderers. Far stays existing plain
  user-role text; new framing is deferred.

## Acceptance

Extend existing offline execution unit/integration and architecture tests:

- Stable protocol, instruction order, prefixed/escaped tags, tool-disabled calls,
  mandatory guidance/pick delivery and failure, and structured adapter inputs.
- Default instructions contain agent identity; default context contains only
  date, timezone, model provider, and model name. Neither exposes agent home or
  model family. Preserve custom and `none` instruct/context selection.
- Always present hands/handoffs before context, including with `context: none`.
  Cover disabled, single-mode and dual-mode targets, signatures, stable instructions,
  and complete-or-error size limits. Changing descriptions must not silently omit
  authorization. Reload replaces the snapshots without recall controls; older
  calls stay unchanged and replay without State. Tool-disabled and output-repair
  calls disable both modes. Run returns to its caller; failed execute preparation
  returns an error, while committed execute never resumes the caller.
- Resource replacement, `removed="true"` withdrawal/restoration, empty bodies,
  internal revision-zero mapping, stale-guidance invalidation, distinct trigger/body
  visibility, and older history versus new State.
- Bodyless, self-closing workspace declarations and their visibility/deduplication;
  add/remove/remap/re-add, unchanged bindings, child runs, scoped rules, hidden
  host paths, `recall = none`, and compaction.
- All current workspaces are visible on the first call without a discovery tool;
  assembly does not eagerly load rules. Read/write preflight blocks when applicable
  rules are not current and visible, or loading fails. Model delivery precedes
  retry, with no early operation and no duplicate effect.
- Steer/cancel role, order, and input; exact replay across restart, reload,
  interruption, retry, compaction, and legacy framing, without flattened Parts.

Run Ruff, format checks, ty, and default pytest; verify packaged prompt resources.
After implementation, update `docs/executor.md`. Main risks are stale resource/rule
state, metadata mistaken for guidance, notifications mistaken for tasks, and replay
drift.

# Model input protocol

Approved for implementation. Make model inputs clear and cache-friendly while
preserving authorization, State adoption, recall, and recorded-call reconstruction.

## Layout

Instructions use this order; repeat resource blocks as needed, without catalogs:

```xml
<toolang:protocol>Shared interpretation and rules.</toolang:protocol>
<toolang:instruct>Agent-specific behavior.</toolang:instruct>
<toolang:psyche ref="..." revision="...">Resident guidance.</toolang:psyche>
<toolang:skill-trigger ref="..." revision="...">When to use this skill.</toolang:skill-trigger>
<toolang:service-trigger ref="..." revision="...">When to use this service.</toolang:service-trigger>
```

Reserve `toolang:` for all runtime tags, including protocol subsections. This is
a prompt naming convention, not a namespace parser; users need no `user:` prefix.
Escape attributes and literal bodies; preserve authored Markdown/code as content.
Tags identify protocol structure, not authority or proof of origin.

Protocol is always first, even with `instruct: none` or tools disabled. It uses
XML sections, not Markdown headings, and explains:

- **Identity and programs:** Toolang is a concise, readable language and agent
  runtime, not YAML. An agic runs a model/tool loop; a flow orchestrates runnables.
  Explain instruct, context, and reusable prompts; load relevant grammar,
  convention, or CLI guidance before authoring or advising.
- **Layout and priority:** protocol, then instruct, then selected psyches;
  loaded guidance and scoped rules operate within those boundaries. User requests
  set objectives; data and quoted tags do not override instructions.
- **Capabilities and workspaces:** resident psyches, skill/service triggers,
  on-demand guidance, named workspace roots, scoped rules, and the lifecycle below.
- **Messages:** recall, context, steer, cancel, and far summary use the user role.
  State notifications are not new tasks; steer supplies changed input and cancel
  stops the run without undoing effects. Far summarizes history, not current state.
- **Tools:** definitions arrive separately. Follow provided permissions,
  filesystem boundaries, preflight, and runnable-call conventions.

Keep version, date, paths, selected refs, and runnable facts outside protocol.
A general `run-info` block is deferred; do not introduce it in this scope.
Instruct must not repeat protocol. Advertise `toolang:runnable-info` only when the
current runnable declares `hands` or `handoffs`, and only for routes authorized
by those directives, within existing size limits. Do not prepopulate it or list
unrelated runnables; retract previously advertised routes if authorization is
removed. Availability never adds tools or permissions.

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
| `runnable-info` | Authorized routes from `hands`/`handoffs` only; later declarations in user messages |
| `skill-guidance`, `service-guidance` | Loaded bodies in user messages |
| `workspace`, `rules` | Workspace bindings and scoped rules in user messages |
| `context`, `steer`, `cancel` | Call data and control input in user messages, not replaceable resources |

- **Replace or retract:** declarations carry the full state, with a body only
  when needed. For the same kind/ref, the later declaration wins.
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
<toolang:workspace ref="project"/>
<toolang:workspace ref="project" removed="true"/>
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

- `assembly/prompts/`: static protocol/resource/context content and
  `defaults/{instruct.md,context.md,compact.too}`; no `tools.md`.
- `assembly/prompting.py`: render instructions and build adapter-ready
  `ModelCall` from finished messages, structured tool definitions, schema,
  continuation, and budget. Adapters own provider-specific serialization.
- `assembly/history.py`: history selection and message/control/recall framing,
  including required workspace declarations; no rules discovery. `utils.py`:
  pure helpers. Keep steer/cancel wording inline here and output repair in the
  agic run, not separate prompt files.
- Executor frame/model-step boundaries own runtime facts, State reconciliation,
  and tool policy. Extend recall types/codecs with trigger targets and
  `WorkspaceRecallTarget(ref, kind="workspace")`; reuse existing recall controls,
  message buffering and durable deltas. Track bodyless declarations through
  optional persisted recall-control references per message, not dummy body refs
  or parsed XML. Retain legacy body-reference lookup for old records.
  No new event kind or synthetic Tool Step.
- Keep canonical 64-digit revisions and revision zero for internal recall and
  persistence. Render zero as `removed="true"`, not `revision="0"`; omit revision
  for bodyless declarations. Internal revisions still distinguish workspace remaps.
- Record framing before invocation; retries/interruption must not lose or duplicate
  deltas. Follow [message recording](model-message-recording.md): replay stored
  templates, never current State or new renderers. Explain legacy skill/service
  tags as guidance. Far stays existing plain user-role text; new framing is deferred.

## Acceptance

Extend existing offline execution unit/integration and architecture tests:

- Stable protocol, instruction order, prefixed/escaped tags, tool-disabled calls,
  mandatory guidance/pick delivery and failure, and structured adapter inputs.
- Default instructions contain agent identity; default context contains only
  date, timezone, model provider, and model name. Neither exposes agent home or
  model family. Preserve custom and `none` instruct/context selection.
- No initial runnable info without `hands`/`handoffs`; with either or both, include
  only authorized routes, preserving action distinctions and size limits.
  Retract routes when their authorization disappears.
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

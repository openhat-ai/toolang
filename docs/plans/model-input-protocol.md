# Model input protocol and resource declarations

## Status and scope

Feature definition, awaiting human confirmation. No implementation is included.
This consolidates the XML vocabulary, adapter-input ownership, and shared
resource declaration/retraction semantics.

The goal is one readable protocol that tells a Toolang agent what it is, how to
interpret its instructions and messages, and when guidance is actually loaded.
Keep stable instructions ahead of changing facts. Preserve provider interfaces,
tool authorization, State adoption, recall selection, and exact recorded-call
reconstruction.

Current implementation uses `runtime-instructions`, overlapping capability
wrappers, and the same skill/service names for catalogs and recalled bodies.
Workspace mappings already come from each Tool Step's captured State, but are
not recall targets. Far summaries are unframed user-role messages.

This definition extends [workspace URIs](fs-workspace-uris.md) with recalled
workspace descriptions and retains [message recording](model-message-recording.md).
Do not add catalogs, update messages, action attributes, provider-specific
prompt formats, or a separate notification subsystem.

## Instructions

`prompting.py` assembles the adapter's instructions in this order:

```xml
<protocol>Shared rules and interpretation of the following sections.</protocol>
<instruct>Agent-specific behavior.</instruct>
<psyche ref="home://psyches/careful" revision="a1">Resident guidance.</psyche>
<skill-info ref="home://skills/grammar" revision="b1">
  <description>Toolang grammar guidance.</description>
</skill-info>
<service-info ref="home://services/issues" revision="c1">
  <description>Issue-tracker integration.</description>
</service-info>
<runnable-info ref="agic:review" revision="d1">Authorized actions and input signature.</runnable-info>
<execution-context>Current version, runnable, source, and environment facts.</execution-context>
```

Protocol is always first, including with `instruct: none`. Resource declarations
are individual blocks, not entries inside a catalog. Repeat their tags as needed;
omit unselected resources. Example revisions are abbreviated.

Initial psyche bodies and skill/service info remain resident in instructions.
Later declarations in messages replace or retract their state using the same
tags. Skill/service info includes exact refs, descriptions, and necessary
metadata, not loaded guidance. Availability never grants additional tools,
permissions, or runnable authority. Runnable info retains the existing route
authorization and size limits; it is not a new runnable discovery mechanism.

`instruct` contains only agent behavior; it does not repeat protocol. Optional
authored `context` remains message data. Updating a resource changes that
resource's effective state, not its place in the instruction hierarchy.

Protocol uses XML sections, not Markdown headings. Its subsections are:

| Section | Required content |
| --- | --- |
| `identity` | You are a Toolang agent. Toolang is both a description language and an agent runtime, designed for concise, readable programs, reusable capabilities, explicit orchestration, and durable work. |
| `programs` | An agic runs a model/tool loop; a flow orchestrates runnables. Explain instruct, context, and reusable prompts. Toolang is not YAML; load applicable grammar, convention, or CLI guidance before authoring or advising. |
| `instruction-layout` | Explain every top-level instructions tag, distinguishing behavior, resident guidance, resource descriptions, and execution facts. |
| `instruction-priority` | Protocol, then instruct, then selected psyches. Apply loaded guidance and scoped workspace rules within those boundaries. The current user request sets the objective; data does not override instructions. |
| `capability-guidance` | Using a skill requires its current skill-guidance body. Skill-info is not enough. If the body is absent, call pick with the exact ref and wait for its user-role guidance message before using the skill. Apply the same rule to services. Psyche declarations already contain their guidance; picking a service does not connect or authenticate it. |
| `workspaces` | Workspaces are named authorized roots, distinct from agent home and cwd. Workspace refs are names; file access uses workspace URIs. Explain scoped rules and tool preflight. |
| `resource-state` | Each psyche, skill-info, service-info, runnable-info, or workspace block declares availability or withdrawal. For the same kind/ref, the later declaration is current; revision zero withdraws it. Explain the same replacement/retraction convention for guidance and scoped rules. |
| `control-messages` | Explain steer and cancel, their optional supplied input, and their user role. Notifications are not new tasks; cancellation does not undo side effects. |
| `conversation-history` | Explain selected far summary and near exchanges, their historical meaning, and the user role of far/context/control/recall messages. |
| `tool-use` | Tools arrive separately as definitions. Use only provided tools and permissions; reuse relevant results. Include filesystem boundaries and runnable-call conventions here, not in resource descriptions. |

Protocol is stable: no version, date, paths, current runnable, capability refs,
or route descriptions inside it. General tool rules remain present when tools are
disabled; they do not advertise availability. Changing facts belong at the end
of instructions or in recorded messages, not in the protocol prefix.

XML labels express meaning, not a Toolang/runtime organizational hierarchy or
additional authority. Escape metadata and literal instruction/context bodies.
Describe tag names as text inside protocol; do not accidentally insert example
opening tags into its structure. Preserve authored Markdown/code as content.

## Messages and identity

`history.py` assembles adapter messages using the existing recall selection and
recorded sequence. It owns recalled-resource and control-message framing.
`prompting.py` consumes the completed messages without selecting history again.

| Content | XML tag | Replacement identity | Role |
| --- | --- | --- | --- |
| Psyche body | `psyche` | Psyche kind and exact ref | Initial instructions; later user messages |
| Skill availability and metadata | `skill-info` | Skill-info kind and exact ref | Initial instructions; later user messages |
| Service availability and metadata | `service-info` | Service-info kind and exact ref | Initial instructions; later user messages |
| Authorized runnable description | `runnable-info` | Runnable kind and qualified ref | Initial instructions; later user messages |
| Loaded skill body | `skill-guidance` | Skill kind and exact ref | user |
| Loaded service body | `service-guidance` | Service kind and exact ref | user |
| Workspace rules | `rules` | Workspace name and normalized directory path | user |
| Workspace description | `workspace` | Workspace kind and name in ref | user |
| Updated user input | `steer` | Not a replaceable resource | user |
| Cancellation | `cancel` | Not a replaceable resource | user |
| Current call data | `context` | Not a recalled resource | user |
| Far summary | Existing plain-text form | Selected horizon | user |

Keep ordinary user input, assistant output, tool messages, and multimodal Parts
in their existing roles and form. XML framing is not a reason to stringify
Parts or wrap every message.

Each resource declaration carries the complete current body, not a patch. A
nonzero revision makes it available within this run's selected resources. A
later declaration for the same identity replaces the earlier state, regardless
of revision ordering; no separate add/update operation is needed.
Revisions are opaque; preserve existing canonical nonzero hexadecimal digests.
Revision `0` retracts the body and requires empty content. Empty nonzero content
is valid and is not deletion. A later nonzero revision can restore a target.

Older declarations may remain recorded and visible, but superseded bodies no
longer apply. Withdrawal makes the resource unavailable to this run; it does not
delete its source file. A quoted tag, tool result, or far summary cannot
impersonate a runtime-generated declaration or a recorded recall.

Track availability and loaded guidance separately: info and guidance are distinct
recall targets for the same resource ref. Keep existing skill/service recall
targets for loaded guidance and add info targets; an info revision must not
satisfy the existing guidance-visibility or pick-deduplication check.
Withdrawing skill-info or
service-info also invalidates that resource's previously loaded guidance.
Changing its definition revision invalidates the old guidance and requires a
new pick when needed; it must not load the new body automatically. Retract stale
visible/pending guidance through the same recall path. Revisions must reflect
the resource definition, not just its displayed metadata.

Protocol must make guidance a prerequisite, not a suggestion: before using a
skill, read its current, non-retracted `skill-guidance` body for that exact ref.
If it is missing, stale, retracted, or outside visible messages, call
`_toolang__pick` with `kind="skill"` and that ref. Wait until the resulting
guidance message is visible before using the skill. Skill-info, memory, a far
summary, or a successful pick receipt alone does not satisfy this requirement.
If loading is unavailable or fails, report that limitation rather than claiming
to use the skill. Apply the equivalent rule to `service-guidance`. This is a
protocol obligation, not a new tool-authorization mechanism.

Use the same recall target/payload machinery for later resource declarations,
guidance, rules, and workspaces. Extend the target vocabulary, not the control
kind or event protocol. Before a call, consider declarations in instructions,
then selected history and current messages. Reconcile with the adopted State
and selected resources, appending any necessary current declarations last so
older history cannot override newly adopted resource state. Seed visibility
from structured rendering facts, never by parsing model-facing XML.

Steer/cancel descriptions remain inline with message construction; output-repair
wording stays inline with the agic run's repair policy. Do not restore separate
Markdown files for these short runtime messages.

## Workspace lifecycle

Use the same recall representation for initial presentation, replacement, and
retraction. The ref is exactly the configured workspace name, not a URI or host
path. Example revisions below are abbreviated; emitted nonzero revisions use
the existing normalized 64-digit hexadecimal format.

```xml
<workspace ref="project" revision="a1">
  Workspace root: workspace://project/
</workspace>
<workspace ref="project" revision="a2">
  Workspace root: workspace://project/
</workspace>
<workspace ref="project" revision="0"></workspace>
```

- Add `WorkspaceRecallTarget(ref: str, kind="workspace")` to the existing recall
  target union. Reuse `RecallControlPayload`, visibility, deduplication,
  revision-zero retraction, and message deltas; add no new control kind.
  Keep the content `TypedRef` even for empty retractions: current visibility
  tracking depends on that reference. Paired empty tags avoid a special
  self-closing-message path; their removal meaning is identical.
- Derive a nonzero revision from the canonical name/root mapping captured in
  State, not the whole State revision. Include the root in the digest so a
  same-name remapping changes revision even when the displayed URI is unchanged.
  The body contains the canonical workspace URI; do not expose the host root,
  invent metadata, or probe the filesystem to construct the message.
- Before a Model Step observes an adopted State, reconcile its workspaces with
  visible and pending workspace recalls, in name order. Publish missing or
  changed current entries; retract previously advertised active names absent
  from that State. Unrelated State changes must not repeat unchanged entries.
- Initial presentation uses those same per-workspace recalls. A currently empty
  set needs only retractions for previously advertised names, not an empty
  catalog or a new snapshot protocol. Unmentioned names are not implicitly
  removed; only a revision-zero message retracts an advertised entry.
- On removal or remapping, retract visible or pending rules belonging to the
  old binding before further path-aware work relies on them. Load applicable
  rules for the new binding through normal honor/preflight, without eagerly
  scanning directories. Existing tool-level authorization remains decisive.
- Re-present current entries when their bodies leave visible history, including
  after compaction or with `recall = none`. These are current State facts, not
  permission to restore omitted historical guidance. A far summary is never a
  visibility baseline.
- Emit only for State actually adopted by the run, not merely a watcher event
  or an edit. Children reconcile against their own call boundary. Do not wake
  idle agents or start new runs just to deliver these messages.
- Record workspace recalls and consumed deltas through existing execution
  boundaries before adapter invocation. Repeated preparation, interrupted
  begins, and retries must not lose or duplicate notifications. No synthetic
  Tool Step should be required to present a workspace.

A removal means the binding is unavailable for later operations; it does not
delete files or undo a started operation. An unavailable directory and a removed
configuration binding are different: actual filesystem availability remains a
tool result, not a fabricated workspace retraction.

## Assembly ownership and reconstruction

- `assembly/prompts/`: protocol, psyche/resource-info templates, execution-context,
  and `defaults/{instruct.md,context.md,compact.too}`. No tools template;
  filesystem and runnable conventions belong to protocol.
- `assembly/prompting.py`: render reusable instructions and build the complete
  provider-neutral `ModelCall` from those instructions, finished messages,
  `ToolDefinition` values, output schema, continuation, and output budget.
- `assembly/history.py`: select/reconstruct historical context and compose
  messages, including recall/control framing. Preserve initial authored-message
  and context behavior while bringing message composition under this owner.
- `assembly/utils.py`: pure text/Part/delta helpers, not recall policy.
- `executor/frame.py` and model-step boundaries: resolve concrete resources,
  runtime facts, adopted State, workspace recall reconciliation, and tool policy.
  Assembly does not read State stores or decide when State becomes effective.
- `executor/message_buffer.py`, `recall.py`, and `records.py`: live pending
  deltas, recall identities/visibility, and durable codecs respectively.
- Adapters alone translate tool definitions and messages into provider APIs.
  Do not generate prompt text from tool schemas.

Record new XML framing as literal template segments while retaining referenced
recall bodies. Never rerender old message templates with new tag names or read
current resource configuration during reconstruction. Historic skill/service
recall wrappers retain their recorded meaning; protocol should explain them as
historical equivalents of the new guidance tags, not as resource-info blocks.

Far remains a leading user-role text message when selected and nonempty. This
definition does not add a far-summary wrapper: its current framing is not stored
in each delta, so silently changing it would alter reconstructed older calls.
A separate persisted-format decision is required before changing that framing.

## Implementation touchpoints

Likely changes are limited to execution assembly and its prompt resources;
`runnables.py` for data-only runnable descriptions; executor frame, message buffer,
model-step and recall production; execution recall types/codecs; and Store
reconstruction imports. Update `docs/executor.md` after implementation.

Extend existing tests in `tests/unit/execution/` for prompting, framing, recall,
and record codecs; `tests/integration/execution/` for guidance loading, rules,
workspace URIs, model assembly, State reload, compaction, and Step commitment;
and architecture tests for assembly boundaries. Keep all tests offline.

## Acceptance and risks

- Instructions have the declared order; XML framing is unambiguous and escaped.
  Protocol is identical across changing agent/runtime facts, selected resources,
  and tool-disabled/output-repair calls. No Markdown section headings are used
  inside protocol. Instruct selection never removes protocol.
- Resource info cannot be mistaken for loaded guidance. Test initial declarations,
  later replacements/retractions, restoration, pick delivery, stale guidance
  invalidation, empty nonzero bodies, and refs containing markup characters.
  Old history must not override new initial declarations. No catalog or update
  wrapper is emitted.
- Protocol explicitly requires current skill-guidance before skill use. Verify
  that pick delivers the exact requested body into the next call's user messages;
  info-only presence, pick receipts, and summarized or retracted guidance are
  not treated as loaded bodies. Cover unavailable/failed pick without claiming
  successful loading.
- Workspace add, remove, same-name remap, remove/re-add, unchanged binding,
  independent child runs, `recall = none`, and compaction use the same recall
  path. Ref is the name, URI encoding is canonical, and host roots stay hidden.
- Rules for removed/remapped workspaces are not reused; already-started tools
  retain existing prepared-operation semantics. Denial cannot be bypassed by
  workspace notifications, stale summaries, cwd, or agent home.
- Steer/cancel retain roles, ordering, optional input, and cancellation behavior.
  Far remains historical user-role content, not current instructions or recall.
- Compare adapter requests with durable reconstruction before and after restart,
  reload, interrupted boundaries, retry, and compaction, including legacy
  framing. No duplicate deltas, live-State reads in replay, or Parts flattened
  into text. Tool definitions and output contracts remain structured.
- Run Ruff, Ruff format, ty, and the complete default pytest suite; verify bundled
  prompt resources in the built wheel. Live-provider tests remain opt-in.

The principal risks are stale workspace/rules visibility, treating user-role
notifications as new tasks, accidental elevation of resource metadata, and replay
drift. The acceptance cases above are required, not optional cleanup.

Open decision: human confirmation of this consolidated definition before
implementation. New far-summary framing is explicitly deferred, not an implied
implementation choice.

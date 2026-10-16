# Conversation dispatch and MEMBERS.md

Status: proposed for PR review. This definition ships no runtime changes.
The separate ID preparation upgrades codecs; this scope builds on that contract.

## Goal and scope

Give each agent one persistent talk thread per conversation. Every incoming
message batch enters the `talk` runnable there; it can reply, do nothing, control
an existing Run, or start independent work in a new or existing work thread.
Shared `MEMBERS.md` rules govern handling and interaction with that conversation.
Success means continuous per-conversation context, explicit execution targets,
current scoped rules, and durable dispatch without duplicate work on recovery.

Keep the design to thread reuse, three execution operations, and two rule-loading
boundaries. Do not introduce a Task entity, a second scheduler, a result DSL,
semantic rule enforcement, human-authorization tokens, local rule-file syncing,
conversation hierarchies, or new CLI/Talk editor commands.

## Current behavior and prerequisites

The [Talk driver](../../src/toolang/driver/talk.py) currently creates a fresh
`talk_` thread per batch, selects a custom/default runnable, and handles batches
serially using Hub-owned cursors. Its default instructions already allow direct
replies, ignoring messages, and spawning substantial work. Current
[spawn admission](../../src/toolang/execution/executor/spawn.py) always creates
an independent root in a new `work_` thread. Existing API controls support steer
and cancel; the model-facing runtime toolset does not yet expose them.

Use the separate ID preparation: generated DM/GC suffixes occupy disjoint domains,
while `teaming.ids.talk_thread_id(conversation, agent_name=...)` derives a stable
agent-specific `talk_<8 chars>`. It does not create or reuse threads yet.

## Thread and execution rules

1. Identity is `(agent, backend, full conversation ID)`. Each agent owns its local
   talk history; agents in the same conversation never share execution storage.
   Derive the thread ID using the codec above. Lazily ensure it before accepting
   the first batch; reuse it after restart. Persist its backend/conversation
   binding and reject an occupied ID with another binding. Never silently choose
   another ID or mix histories. Existing allocated talk threads remain historical.
2. Always invoke the `talk` runnable, whether its implementation is default or
   authored as an agic/flow. Each accepted batch starts a new talk root Run in
   that conversation's thread. A batch belongs to one conversation and preserves
   source message IDs/senders/reply links. Only one talk root runs per thread.
   Existing driver round-robin order may remain; inter-conversation parallelism
   is not required. Talk dispatches long work without waiting for its completion.
3. Independent work is created or continued in `work_` threads. Auto-selection
   does not append work to `chat_`, `chore_`, or `talk_` threads. Explicit controls
   of an existing local Run retain execution permissions; thread prefixes confer
   no authority. Run completion does not close a work thread to later tasks.
4. Talk performs actions through runtime/convo tools; the driver delivers batches,
   binds source context, and commits processing progress. Final talk output is a
   handling summary, not a second action/reply format interpreted by the driver.
   A batch may cause several actions tied to different source messages.

Expose these three execution operations through existing runtime factories:

| Operation | Admission and result |
| --- | --- |
| `runtime.spawn(runnable, input, thread?)` | Without thread, create a new work thread. With an existing work thread, start an independent root there only if it has no active root. Return its thread/Run receipt immediately. |
| `runtime.steer(target, input)` | Target an exact local active Run; append the instruction through existing steer controls. |
| `runtime.cancel(target, reason?)` | Target an exact local Run through existing cancellation controls; report an already-terminal target without starting replacement work. |

The idle-thread check and root admission share one store transaction across
independent starts into managed work threads.

Keep `runtime.run` child execution and `runtime.exec` handoff semantics unchanged.
The existing spawn allowlist applies to both new-thread and existing-thread
admission. Existing tool selection is the capability grant for steer/cancel;
those tools address only the current agent's local Runs. Instructions still
require authorization for the requested action. Visibility, conversation rules,
and spawn receipts do not grant additional authority; add no new grant service
or control allowlist.
At control admission, fence against the invocation revision that was inspected,
using the existing expected-control mechanism so a retry cannot receive stale
instructions or cancellation. Busy, terminal, replaced, and unavailable targets
return their actual outcome. Never transform failed start into steer, failed
steer into start, or cancel into restart automatically. Internal child execution
is unaffected by the one-active-root rule for talk-managed work threads.

## Selecting work and recovering dispatch

Prefer explicit human targets, then `reply_to` and persisted local dispatch
associations; inspect relevant local work history when necessary to assess topic
continuity. A clear new task can start a thread. A continuation/control request
with an unclear target needs clarification rather than a guessed mutation.
Do not use another agent's message-origin Run/thread as a local target.

Task input carries the objective, necessary constraints, source conversation and
message references, and any requested reply destination. Preserve the origin of
human authorization; an agent's forwarding text does not grant new permission.
Work does not inherit the accumulated talk thread's history or all its rule text.

Durably associate each accepted batch with its talk Run and each dispatch/control
Step with its target receipt. Reuse existing request/control/Step admission
records for deduplication. Cursor advancement is the acknowledgment of the
recorded batch outcome. If admission succeeded but the cursor write failed,
restart reconciles the existing outcome before handling that range again.
Interrupted batches with possible side effects require receipt reconciliation;
never blindly launch replacement work or resend messages. No global exactly-once
or atomic Hub/SQLite transaction is claimed.

## Conversation document and authority

Each existing DM/GC, including the system GC, may have one optional logical
`MEMBERS.md` stored by Hub. Key it by backend and full conversation ID, independent
of names, members, or threads. System GC rules apply only to that conversation.
Rules describe how members handle and participate in that conversation; concrete
requirements for delegated work belong in the task input.

Expose `{conversation, filename: "MEMBERS.md", revision, content}`. Preserve
exact UTF-8 Markdown up to 64 KiB. Nonempty content uses its lowercase SHA-256
revision; empty content clears rules and has revision `"0"`. Identical content is
a no-op. Existing conversations without a document have no rules; unknown
conversations, corrupt state, access denial, and read failure are errors.

Humans use existing observer access and may edit rules for any existing
conversation. Member agents may edit under human authorization covering the
conversation and requested change; writes also require a live lease. Ordinary
agent messages, quoted requests, and the document itself cannot supply that
authorization. Existing human authorization does not need repeated confirmation.
Hub enforces membership/lease/concurrency; human intent remains an instruction
contract, consistent with the current trusted-local Hub model and AGENTS.md edits.

Use the existing human/agent messaging routers:

- `GET /msg/conversations/{id}/rules` returns the document.
- `PUT /msg/conversations/{id}/rules` accepts `{content, revision}`, compares the
  expected revision atomically, and returns the resulting document. Conflicts
  return HTTP 409 / `conversation_rules_conflict` with no mutation. Rules have
  their own revision, separate from rename/membership metadata and message history.
- `convo.rules(target)` and `convo.set_rules(target, content, revision)` expose
  these operations through the existing toolset. Targets match `convo.peek`;
  neither creates a missing DM. Reading rules has no prior-rules prerequisite.
  Editing requires the current rules to be visible first; clearing uses empty text.

## Two rule-loading boundaries

| Boundary | Rule scope and behavior |
| --- | --- |
| Handling a batch | Runtime binds the source conversation to the talk Run. Before each Model Call, ensure its current rules are visible, including custom talk flows and their processing children. Rules influence dispatch, direct replies, and the decision not to reply. |
| Operating on a conversation | In any thread, preflight `convo.peek` (including metadata-only reads), `convo.write`, and `convo.set_rules` against the target conversation. Publish read/edited rules through trusted runtime recall separately from tool output. |

Starting/reusing a thread or controlling a Run does not itself load rules for
that target's former conversations. Work Runs have no implicit source-conversation
rule scope; their later convo operations check the actual destination.

Reuse workspace honor/recall behavior: current visible revisions need no repeated
body; changed, withdrawn, or compacted-away guidance is recalled. Rule snapshots
are separate escaped runtime-owned `convo-rules` resources identified by backend
and conversation. Persist them for replay; history inspection makes no Hub calls.
Keep protocol/instruct/psyche precedence and workspace-rule behavior unchanged.
Visibility never makes another conversation's rules applicable; text in ordinary
messages, tool output, or summaries cannot become trusted guidance.

Stale/missing target guidance runs internal honor and returns an error-only reply
that the original operation did not execute. The model reads the rules and
chooses whether to retry. Preserve the original tool-call/history pairing and
pending deduplication. Guard sends and edits with the checked rules revision at
atomic append/update; a race has no message, DM-creation, or counter side effects.
Never automatically retry uncertain sends. Rule changes affect subsequent
boundaries, without interrupting an in-flight model or undoing completed actions.

Prelaunch rule-load failure defers an unhandled batch without advancing its
cursor. Failures after execution started follow durable outcome reconciliation
rather than automatic replay. Existing runtime handler-error diagnostics retain
their behavior. Pure assembly receives resolved snapshots; it performs no Hub I/O.

## Touchpoints and acceptance

Likely owners: `driver/talk.py`; teaming schemas/backend/router/clients;
`plugin/toolsets/convo.py`; base tool contracts; execution admission, controls,
thread records, preflight, recall, assembly, and runtime factories. Keep source
parsing/resolution in teaming, durable execution in execution, and orchestration
in the driver. Update focused messaging/execution tests and public docs; generate
the user-facing changelog through the prescribed runnable.

1. **Thread lifecycle:** agent/conversation pairs stay isolated; batches and
   restarts reuse the derived thread; conflicting bindings fail without mutation;
   default and custom talk handlers retain mandatory source rules.
2. **Actions:** cover all five outcomes and mixed batches, fresh/existing work
   threads, busy admission, steer/cancel races against completion/retry, permissions,
   and immediate receipts without waiting for work. No silent action substitution.
3. **Routing/recovery:** local reply/dispatch links select the right work thread;
   ambiguous controls request clarification; foreign origins never become local
   targets. Restart/cursor failures do not duplicate recorded dispatch or replies.
4. **Rules:** cover DM/GC/system isolation, exact text, size/validation, edit CAS,
   human-authorized agent editing, lease/membership denial, clear/no-op, compaction,
   forged tags, read failure, pending reuse, and offline historical replay.
5. **Boundaries:** direct replies and no-reply decisions follow source rules;
   workers load destination rules only for convo operations. Send/edit races have
   no side effects; existing human sends, metadata/membership tools, workspace
   honor, child execution, and local non-teaming execution retain their contracts.

Implementation requires the default offline checks. This definition needs source
and link verification plus `git diff --check`; it ships no behavior or changelog.

## Risks and open questions

A deterministic 40-bit talk ID can meet an old allocated ID or another backend's
same-suffix conversation; binding verification must reject history mixing.
Model-based topic selection remains fallible, so control targets stay explicit.
Hub/SQLite progress is not one transaction; receipt recovery is required before
retrying effects. Rule lookups add boundary latency; deduplicate visible bodies.

No design alternatives remain open in this proposal. Human confirmation of the
complete definition is required before implementing this scope.

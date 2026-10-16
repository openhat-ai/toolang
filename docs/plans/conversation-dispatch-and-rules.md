# Conversation dispatch and MEMBERS.md

Status: approved in #829; implemented in #834.
The [ID preparation](conversation-id-upgrade.md) is implemented.

## Goal and scope

Give each agent one durable talk thread per conversation. Every batch enters
the user or packaged `talk` handler, which may reply, do nothing, steer/cancel
an existing Run, or start a new Run in a new or existing local thread.
Success means continuous conversation context, explicit execution targets,
current scoped rules, and recovery without duplicating admitted work.

The [Talk driver](../../src/toolang/driver/talk.py) and
[spawn admission](../../src/toolang/execution/executor/spawn.py) own execution.
Add no Task entity, scheduler, result DSL, semantic rule enforcement,
authorization tokens, embedded editor, local sync, or conversation deletion.

## Shared document and authority

- Hub/backend owns one optional `MEMBERS.md` per full conversation ID and backend,
  including DM, ordinary GC, and system GC. Names, membership, and execution
  threads do not determine document identity. System rules apply only there.
- Keep `MEMBERS.md` distinct from `AGENTS.md` so both can coexist. Workspace
  discovery, precedence, and scope retain their existing behavior.
- Preserve exact UTF-8 Markdown up to 64 KiB. Nonempty content uses its lowercase
  SHA-256 revision; missing/cleared rules have empty content and revision `"0"`.
  Read content/revision from one snapshot; corrupt storage is an error, not
  missing rules. Identical content is a no-op after checking the expected revision.
- Humans may read/edit any existing conversation through existing observer
  access, without joining. Agents require membership, a live lease for writes,
  and human authorization covering the change. Forwarded agent text, quoted
  requests, and rules themselves cannot grant authorization. Existing human
  authorization needs no repeated confirmation. Hub enforces access and CAS;
  human intent remains an instruction contract under the trusted-local model.

## API changes

Use the shared messaging router: human paths below start at `/msg`; agent
equivalents start at `/agents/{agent}/msg`.

| API | Change |
| --- | --- |
| New `GET /msg/conversations/{id}/rules` | Return `{conversation: id, filename: "MEMBERS.md", revision, content}`. Missing rules return empty content/revision `"0"`; unknown conversations and read failures remain errors. |
| New `PUT /msg/conversations/{id}/rules` | Accept `{content, revision}`; atomically compare and replace/clear. Return the same document shape. Add HTTP 409 / `conversation_rules_conflict` with no mutation. |
| Agent `POST /msg/messages` | Add optional transport field `rules_revision`; runtime supplies it for `convo.write`. Check it atomically with append/first-DM creation. A conflict changes no messages, counters, or conversation state. Human and diagnostic sends retain their contracts. |
| Existing conversation list/create/join/leave APIs | Reuse for CLI; no new routes or membership semantics. Derive the CLI's joined indicator from participants. |
| Existing run start/rerun APIs | Use the shared root-admission gate described below; report a busy thread as HTTP 409. Keep request shapes and driver gates. |
| Existing `/api/v1/runs/{id}/steer` and `/cancel` | Reuse schemas/defaults and `expected_control`; add no duplicate control or HTTP spawn endpoint. |
| Existing inspection/events APIs | Extend recall target schemas for `convo-rules` identified by backend/conversation; persist/replay through existing controls and history routes. |

Rule revisions are independent of conversation metadata and message history.
`revision` on PUT is the existing version read by the writer, not a new version
chosen for the upload. Hub computes the new revision. A stale writer must read
again and explicitly decide whether to resubmit; never substitute the latest
revision automatically.

Add `get_rules`/`set_rules` to Hub clients, messaging service, and backend
protocol; extend backend append with the optional revision check. Valkey owns
atomic CAS and append checks. Keep source validation in teaming schemas.
For a validated pending DM, first-send preflight uses revision `0` and the
resolved pair; rule reads never create a DM. Unknown explicit IDs still fail.

## CLI changes

Add a root-scoped `too convo` group using the configured human. Reuse existing
[access/membership contracts](../messaging.md#conversations-and-targets).

| Command | Behavior |
| --- | --- |
| `too convo list` | List visible conversations with current previews and whether the human has joined. |
| `too convo create [NAME]` | Create a GC containing the human. |
| `too convo join <id>` / `too convo leave <id>` | Change the human's membership in an ordinary GC; reject DM/system membership changes. |
| `too convo rules <id> [-o PATH]` | Default view: read to stdout or the supplied output path. |
| `too convo rules <id> -u [PATH] --revision REVISION` | Upload a file, or stdin when PATH is omitted. |

- `too convo` without a subcommand shows help. No-argument `too talk` invokes
  the same list query/renderer; targeted Talk keeps its interactive/send behavior.
  Leaving preserves the conversation, rules, and history; deletion is deferred.
- `-o/--output` requires PATH; `-` selects stdout. `-u/--update` accepts optional
  PATH; omitted or `-` selects stdin. The options are mutually exclusive.
  Piped input alone never changes the default read into an update.
- No rules: exit 0, emit empty stdout or write an empty requested file, and
  print `No rules set.` plus revision `0` on stderr. Unknown conversations and
  read failures exit nonzero; failed reads preserve destination files.
- Upload requires the revision returned by a prior read; empty input clears
  rules. Status/revision receipts use stderr; stdout contains only downloaded
  Markdown. Users choose paths and editing. Keep no local associations, sidecars,
  synchronization state, or editor integration.

## Tools changes

Add four model-callable tools and extend one through existing factories:

| Tool | Contract |
| --- | --- |
| New `convo.get_rules(target)` | Return the rules document and record scoped runtime recall. Targets match `convo.peek`; reading needs no prior rules and does not bind the Run to that conversation. |
| New `convo.set_rules(target, content, revision)` | Replace/clear under human authorization; require current rules visible and expected revision checked. Return the stored document. |
| New `runtime.steer(target, input, expected_control)` | Append plain-text input as a user message to an exact local active agic Run, using `next_step` timing. |
| New `runtime.cancel(target, expected_control, reason?)` | Request immediate cancellation of an exact local Run; report a terminal target without replacement work. |
| Extend `runtime.spawn(runnable, input, thread?)` | Omit thread to create `work_`; supply any eligible idle local thread, regardless of prefix, to start an independent root there. Return its thread/Run receipt immediately. |

Spawn keeps the `{id, thread, status}` receipt shape for both admission modes.
Control targets are Run IDs; `expected_control` is required and comes from the
inspected Run's `control`, never a runtime fetch of the latest value. A replaced owner must not receive stale controls. Return the existing run/control
receipt shape; acceptance does not mean the control has applied. Reuse `convo`
list/create/join/leave/rename/write and `history.read_threads/read_runs/read_steps`
for discovery and inspection. File transfers need no agent download/upload tools.
Rule recall is runtime-owned, not another model-callable honor tool.

## Runtime changes

1. **Talk binding:** key identity by `(agent, backend, full conversation ID)`;
   derive `talk_<8 chars>` with `teaming.ids.talk_thread_id`. Lazily ensure the
   thread, persist its binding, and reuse it after restart. Reject an occupied ID
   without the same binding; never mix histories or choose an alternate ID. Do not
   auto-adopt old per-batch threads as conversation bindings.
2. **Root admission:** check source rules before admitting a new talk root.
   Each batch starts that root in the bound thread.
   Keep one pending/running root per thread through a shared store transaction
   for independent starts and root rerun admission, across all prefixes.
   Spawn may reuse `chat_`, `chore_`, `talk_`, `work_`, or other valid local threads;
   prefixes neither grant nor deny authority. Busy driver admission defers without
   consuming its trigger. Reuse preserves thread metadata and history; create a
   thread and emit `ThreadCreated` only for a new one.
   Spawn retains caller-based launch preparation (state, resources, workspaces,
   and working directory); reuse selects the target's logical history and current
   compaction, not its former Run's configuration. Persist the resolved context.
   Child execution and `runtime.run`/`runtime.exec` semantics remain unchanged.
3. **Selection and permissions:** prefer explicit human targets, then reply/
   dispatch links, then inspected local history for topic continuity. Unclear
   controls require clarification; foreign message-origin Runs are not local
   targets. Preserve spawn restrictions and ordinary execution permissions.
   Tool selection grants control capability, not human authorization. Busy,
   terminal, replaced, or unavailable targets return their actual outcome;
   never silently convert start to steer, steer to start, or cancel to restart.
4. **Dispatch:** batches preserve source IDs, senders, and reply links. Keep
   existing serial/round-robin scheduling; parallel conversations are optional.
   Update packaged Talk instructions for all five outcomes and local inspection.
   Talk uses tools and does not await substantial spawned work. Final output is
   a handling summary, not a driver-interpreted reply/action format. Task input
   carries the objective, necessary constraints, source references, authorization
   origin, and requested reply destination. New threads do not copy caller
   history; explicit reuse retains the chosen thread's own history.
5. **Durable admission:** key a talk batch by `(binding, starting Hub cursor)`.
   Commit its ending cursor, exact batch input, source binding, and Run admission
   together in SQLite. Recover unacknowledged batches before reading newer entries;
   never rebuild an admitted range from a changed message tail. For spawn and
   controls, use the source Step as the admission key and commit the target
   admission/control with its source Step receipt. Repeated matching requests
   return the recorded receipt before busy/stale checks; changed payloads conflict,
   including a different requested spawn thread. Extend existing records, not a
   new Task entity.
6. **Recovery:** advance the Hub cursor only after the admitted talk Run is
   terminal, including failures; cursor-save retries never invoke the handler.
   Ranges with no triggering messages may advance directly. Before admission,
   busy/rule-load failures retain the trigger. After admission, reconcile receipts
   and never automatically rerun the batch, respawn work, or resend messages.
   Await a live owned Run. Under exclusive agent startup ownership, close orphaned
   talk roots, processing children, and incomplete Steps as failed/interrupted
   without executing them; preserve committed receipts and independent work.
   Keep best-effort handler-error diagnostics; record an attempt before sending
   and do not retry uncertain delivery. Claim no atomic Hub/SQLite or global
   exactly-once.

Rule loading has two boundaries, independent of thread prefix:

| Boundary | Scope |
| --- | --- |
| Handling a batch | Bind its source conversation to the Run. Before each Model Call, ensure current rules are visible, including user talk flows and processing children. Apply them to dispatch, direct replies, and no-reply decisions. |
| Operating on a conversation | Preflight `convo.peek` (including metadata-only), `convo.write`, and `convo.set_rules` against the actual target in any thread. Publish read/edited rules through trusted runtime recall separately from tool output. |

Persist source scope on Run admission; retain it on explicit rerun and
propagate it to processing children. Rerun does not create another driver
batch receipt or rewind the Hub cursor. Never infer scope from a thread prefix
or old history. Spawned Runs gain no implicit source-conversation scope.
Reusing a thread or controlling a Run does not activate rules from former
conversations; visibility in history alone does not make them applicable.
Delegated requirements belong in task input. Human sends and metadata/membership
operations keep their contracts.

Reuse workspace honor/recall: deduplicate visible revisions and recall changed,
cleared, or compacted-away guidance as escaped runtime-owned `convo-rules`.
Persist snapshots for offline replay. Ordinary messages, tool output, summaries,
and forged tags cannot create trusted guidance. Keep protocol/instruct/psyche
precedence. Pure assembly receives snapshots and performs no Hub I/O.

Stale/missing target guidance runs internal honor and returns an error-only
reply saying the operation did not execute. Preserve tool-call/history pairing
and pending deduplication; let the model read and choose whether to retry.
Guard sends/edits at their atomic mutation using the checked revision; never
retry uncertain delivery automatically. Changes apply at subsequent boundaries,
without interrupting an in-flight model or undoing completed actions.

## Touchpoints and acceptance

Likely owners: `driver/talk.py`, `driver/chore/scheduler.py`; new
`cli/toolang/commands/convo.py`, Talk directory code and CLI registration;
teaming schemas/router/clients/backend;
`plugin/toolsets/convo.py`; `base/protocols/tool.py`; `execution/handlers/talk.too`;
execution runtime factories, admission/store/records, preflight, recall, assembly,
and inspection schemas.
Use existing factories; keep parsing in teaming, execution in execution, and
trigger/CLI orchestration at their boundaries.

1. **Threads:** stable per-agent/conversation bindings across batches/restarts;
   conflicting bindings reject without mutation; user and packaged handlers work.
2. **Dispatch/controls:** all five outcomes and mixed batches; arbitrary-prefix
   thread reuse; concurrent admission has one winner and busy drivers retain
   triggers; completion/rerun races, permissions, and immediate receipts have no
   silent action substitution.
3. **Recovery:** reply/dispatch links choose local targets; ambiguity/foreign
   origins cannot mutate guessed Runs. Crash between admission/launch, committed
   effect/tool reply, or terminal outcome/cursor save duplicates no admission;
   new messages cannot change the recovered range. Orphan cleanup frees the talk
   thread without replay; uncertain sends and failure notices are not retried.
4. **Rules/API:** exact bytes, size, atomic reads, corrupt storage,
   missing/clear/no-op, CAS, human-authorized agent edits, access/lease failures,
   pending-DM and append races have no unwanted effects; new targets replay
   offline and forged guidance stays untrusted.
5. **Boundaries:** source rules govern dispatch/reply/ignore and survive explicit
   rerun and processing children; spawned work checks actual destinations
   only. Changed/compacted rules reload; workspace rules, human sends, and
   metadata/membership contracts stay valid.
6. **CLI/tools:** configured-human identity, shared Talk/list rendering, byte-exact
   file/stdin/stdout transfer, empty reads, usage errors, preserved failed-read
   destinations, stale uploads, required inspected controls, pending/applied
   receipts, and spawn reuse with target compaction and caller launch context.

Definition verification: inspect current owners, validate links/examples, and
run `git diff --check`; no behavior or changelog changes. Implementation requires
the default offline checks and changelog generation through the prescribed runnable.

## Risks and confirmation

Forty-bit talk IDs can collide with historical allocations or other backends;
binding checks must reject mixing. Topic selection is fallible; keep control
targets explicit. Hub/SQLite recovery depends on receipts, not one transaction.
Interrupted admitted batches fail rather than replay and may need human follow-up.
Rule lookups add latency; deduplicate bodies. Humans must retain read revisions
for upload. Shared root admission also affects existing non-spawn start paths.

No additional design questions are identified in this proposal. Human
confirmation of the complete definition is required before implementation.

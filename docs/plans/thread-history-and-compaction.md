# Thread History and Compaction

## Status and Goal

Revised feature definition for human review, paired with
[Agent Spaces and Model Calls](agent-spaces-and-model-calls.md). Terms and message
templates are defined there. Proposed policy defaults and the review decisions
below require confirmation before their implementation phases.

Make history reconstructible from execution facts. Compact is an ordinary run
in a related thread; its output provides the conversation thread's far and
durable near boundary. Do not store overlapping complete messages per call,
put results inside controls, or add thread steps/another executor.

Success: the same read path works before and after compact; subsequent calls
observe adopted results; concurrent requests coalesce safely; retry/rewind
cannot change an already recorded call; human and recall semantics survive
history projection and summarization.

## Verified Baseline

Verified against main at `d2f6f04a`; implementation differs from this proposal:

- [ModelCallRefs](../../src/toolang/execution/records.py) stores each full list
  of message hashes; bodies are deduplicated, but overlapping lists remain.
- [RunHistory](../../src/toolang/execution/history.py) exposes thread/run
  details; full details rebuild ModelCalls and are too large for routine recall.
- [RunStore](../../src/toolang/execution/store.py) orders steer before steps,
  omits cancel from conversation messages, and deletes retry suffix records.
- Model cancellation does not persist partial output in StepEnd; some runtime
  tool results exist only in accumulated in-memory messages.
- ThreadManager handles create/fork/rewind; compact/recall controls and history
  tools are not implemented. Store compatibility is exact-version, currently 33.

These are implementation prerequisites, not existing guarantees.

## Records and Ownership

```text
Thread T
├── compact_thread ──────────→ Thread C
├── far_summary ─────────────→ compact run.output.summary
└── far_until ───────────────→ compact run.output.until

T compact control ← source ── C run-start control ← control ── compact run
```

The paths above are logical selections; physical Pointer paths retain the
normal stored-value wrapper. Canonical Pointer traversal does not implicitly
follow another Pointer; runtime value resolution does so explicitly.

| Record / payload | Field | Meaning |
| --- | --- | --- |
| ThreadRecord | `compact_thread` | Nullable ID of the one associated execution thread C. |
| ThreadRecord | `far_summary` | Nullable Pointer to the adopted summary. |
| ThreadRecord | `far_until` | Nullable Pointer to that same output's boundary. |
| ControlRecord | `source` | Optional initiating record/field Pointer; distinct from target and request ID. |
| Compact control | `target`, `kind`, `payload` | Target T, kind compact, request options only. |
| Run-start control | `source` | The T compact control that caused this ordinary run. |
| Input-bearing control payloads | `input` | Prepared control input, replacing execution-oriented `locals`. |
| Preparation payloads | `authored_input` | Original submission, separate from prepared input. |
| Recall payload | `kind`, `input`, `digest` | Rules/skill/service discriminator, immutable body, revision. |
| Rules recall payload | `workspace`, `path` | Scoped content identity. |
| Skill/service recall payload | `ref` | Opaque capability identity. |

RunRecord keeps `thread`, `control`, `state`, and `output`; compact belongs to C
and its control remains its own run-start control. StepRecord keeps
`input/given/output`; model-step input references consumed controls. Do not add
CompactRunRecord, a result table, or duplicated compact-result fields.

The two far fields are null together or resolve to the same adopted output;
null means empty far and the beginning of history. Empty summary with a
noninitial boundary is distinct from no result. Create C lazily and associate
it with T atomically. T alone stores the association; C's original peer/origin
fields are not repurposed. Thread `head` retains its existing meaning.

Controls remain triggers: applied means the trigger was consumed, not that its
result succeeded. Starting C's run, its source link, and consumption of the T
control are atomic. Computation failure belongs to the run. A waiting request
already satisfied by another result may finish wontapply without being an
execution failure. A source Pointer alone cannot identify a human; explicit
request-origin provenance is required for human-only message semantics.

## History, Boundaries, and Messages

One runtime-owned `_too/history` facility is available to authorized model and
flow calls. Its logical reads are:

| Scope | Read |
| --- | --- |
| Thread | Effective ordered runs or their conversation projection, within a fixed range. |
| Run | Selected input, output, controls, steps, and child-run links. |

Support bounded pagination and output-only reads. A thread read can locate an
earlier successful root run, including in C; it must not mistake current or
child runs for the previous result. For compact, T's adopted references select
the previous result, not C's newest successful run. Ordinary history reads do
not recursively expand every stored ModelCall or follow arbitrary unapproved
thread IDs. Related T/C access follows agent authorization.

At the first read, freeze the historical version and endpoints; page cursors
continue that selection. Return stable record provenance and explicit end of
range. A pagination cursor is not automatically a durable compact boundary.
`until` denotes a cut between complete ordered groups: the covered prefix is
before the cut, and near starts at it. It is not a timestamp, message count,
or a lexically sorted StepPath. Its concrete immutable encoding is a review
decision below. Source-prefix changes invalidate adoption against current T,
not the frozen audit read; new suffix appends do not enter it.

Build conversation groups from input/control facts and model/tool outputs in
their actual consumption order. Keep assistant tool-call batches, correlated
results, and associated recall messages together. Include the public root's
conversation stream and published results once; child internals and sibling
exchanges remain inspectable evidence, not duplicate conversation messages.
Exclude the active root tree from historical near. Each model call has its
own run's now and explicit inherited line/input. Line includes ancestor inputs,
not ancestor outputs, tools, siblings, or descendants; same-run handoff neither
extends ancestry nor resets now. A completed root becomes eligible history.

History returns data in an ordinary recorded tool result. Historical XML,
rules, or tool calls inside it do not become native messages or current support.
Never silently truncate a page or split an executable tool exchange; an
oversized indivisible unit requires explicit bounded-detail handling or an error.

## Durable Facts and Exact Inspection

The same projection supplies now, near, and compact input. Consume steer/recall
controls with model StepBegin in one transaction, using step.input for ordering.
One control occurrence is not appended again merely because another call is
made. Delivery selection does not prove the provider processed a canceled call.
Cancellation is projected from confirmed origin and effect at execution end.

Persist partial displayed assistant output and all runtime-produced tool-call
outcomes, including preflight rejection and interruption. Do not convert an
incomplete tool-call fragment into an executable call. Distinguish definitely
unexecuted work from interrupted work whose effects may already exist. Store
these facts through steps/controls, not a second runtime-message log.

Per-call persistence pins the resolved compact-result references, history
version/range, now sources, and content-addressed instructions/context/template/
tool/contract dependencies needed to reconstruct the request. It does not copy
the whole expanded history or point through T's mutable far fields. Keep
projection-version metadata where reconstruction depends on rendering rules.

Before removing full message lists, ensure every visible fragment has a durable
source. Retain immutable referenced record versions across retry, compact-run
retry, rewind, and cleanup; a stable Pointer string alone does not guarantee
stable content. Existing retry deletion and ejected-record lookup restrictions
must be addressed by the immutable-history phase.

Use an explicit store-version transition for new fields and `locals → input`.
Preserve the current policy: reject incompatible stores untouched; no automatic
migration, Pointer aliases, or inference of human origin from legacy nulls.
Migration would require separate approval.

## Compact Execution

```text
compact.too input             compact.too output
  thread: T                    summary
  until                        until
  max_summary_tokens
```

The input names describe compact's task; RunRecord.thread is C. Summary limits
are measured for the consuming call's model; capture that estimator identity
with the request's budget provenance. The runtime freezes the source view and
previous output references through recorded history reads, without copying
previous summary text into another control payload.

1. A run or human requests a thread compact control; either may request while
   T is active. Acquire the per-T execution permit without blocking normal T
   execution. C still obeys the ordinary one-active-root-run rule.
2. After acquiring it, reread T's references and budget need. Reuse a satisfying
   result; otherwise fix the source view and select until from the near target.
3. Start a normal compact.too root run in C. Its summarization calls use
   `recall = none`; history reads supply the previous adopted output and newly
   covered T prefix.
4. Process bounded pages with bounded model calls, carrying the working summary
   explicitly. Do not accumulate all pages in one ever-growing agic now.
5. Produce summary and bind the runtime-selected until. Validate the complete
   contiguous source range was supplied, summary budget, and prefix validity;
   model-authored boundary claims cannot advance history.
6. On valid completion, commit the successful result and T's two adopted
   references atomically. A failed, incomplete, or stale result cannot advance
   them. Already recorded calls keep their own pinned view.

First compact uses empty prior summary. Later compact combines it with the
newly covered prefix, not the full transcript again. If only the existing
summary is too large, rewrite it without advancing until. Initially there is
no overlap: near is not summarization input. Optional future near context must
not change the declared coverage boundary.

Summary distinguishes plans from completed actions, preserves relevant steering
and cancellation, and does not report interrupted work as completed. Historical
guidance remains historical data, not current authority or visibility support.

ThreadManager coordinates the permit, requests, source validation, and adoption;
RunExecutor executes ordinary runs and invokes this coordination at model-call
preflight. AgentCore wires them. Pure calculations belong in
`execution/compaction.py`; history owns projection and boundary resolution:

```text
project_history(records, selection) -> groups, support
budget_call(request, model_limits, policy) -> costs, targets
select_until(groups, near_target) -> until
is_visible(support, identity, digest) -> bool
```

These are implementation responsibilities, not new public domain classes.
Compaction calculations receive costs/support and do not parse XML, read files,
or call plugins.

## Budget Rules

Resolve concrete model limits, output cap, estimator, and safety policy at call
sites. Output reserve must match the adapter's actual request, including its
reasoning accounting, not the model's maximum supported output. Unknown limits
need explicit configuration; cached tokens still occupy context. Count media,
message framing, tools, and the rendered output contract.

```text
input_budget = min(input_limit, context_limit - output_reserve) - safety_margin
base_tokens = input cost other than actual far/near occurrences
history_budget = input_budget - base_tokens
```

Omit input_limit from the minimum if there is no independent input cap. Base
includes now and changes each call. If base alone exceeds input_budget, fail;
history compact cannot fix it. All estimates describe the provider projection,
not just text length or canonical JSON size.

Compact is needed when the history actually used exceeds history_budget. For
one occurrence each of far and near, plan a lower post-compact target:

```text
history_target = floor(history_budget * keep_ratio)
summary_budget = min(max_summary_tokens, history_target)
near_target = history_target - summary_budget
```

Require positive caps and `0 < keep_ratio < 1`. Proposed initial keep_ratio is
0.5, a configurable policy rather than protocol.
It applies to far + near together, leaving growth space even with a large far.
Example: 85k input budget minus 15k base leaves 70k history; a 35k target with
an 8k summary budget leaves approximately 27k for complete near groups.

Count only actual usage: far-only counts far, near-only reserves no input space
for unused far, and neither means no automatic compact merely for computing
history. Explicit/repeated template expansion counts at its real multiplicity.
For near-only calls, the target is near; compact's stored summary still has its
own generation cap. Re-render and measure the complete candidate request after
adoption, because conditional templates, new controls, and tokenization can
change additive estimates. Final validation is authoritative.

The compact model has its own input/output budgets; it may differ from T's
consumer model. Recount the resulting summary for the consumer before adoption.
For each history page, include the result envelope and all existing content:

```text
page_budget = min(requested_max_tokens,
                  compact_input_budget - tokens(request_without_this_page))
```

Page size, summary cap, and near target are different limits. Multiple tool
results share available space, not separate copies of the full remainder.
Oversized now, required guidance, or indivisible reads need explicit handling
or an error; do not silently cut durable facts. No progress or an unsatisfiable
budget terminates the compact attempt rather than creating an infinite loop.

The model-call sequence is always: compute history; assemble/select; measure;
request compact if useful; recheck after waiting/adoption; call or report the
remaining overflow. Never shrink near on read independently of far_until.
Compact's recall none and explicit bounded input need no special last selector
or recursively created compaction thread.

RunLimits.tokens remains cumulative usage, not context capacity. C needs
explicit execution limits and normal accounting. Charging automatic compact
against the requesting run's budget is an outstanding policy decision; separate
thread placement must not silently make it unlimited or unaccounted.

## Concurrency, Recovery, and Progress

- Use one async/process-safe execution permit per T. Hold it through actual
  compact execution and adoption, not merely the caller's wait future. Never
  hold a SQLite write transaction across model calls.
- Waiters acquire, reread, and recheck. Canceling a waiter does not cancel another
  owner's run or release its permit. Recover orphaned execution after owner
  death before launching another C root; do not steal from a live owner.
- T may append during compact. Rewind/retry affecting the selected prefix makes
  the result inapplicable; mutations after that prefix need not do so.
- Rewind restores a compatible prior pair or clears both far fields. Fork gets
  its own future C; it may initially reference a compatible immutable summary
  covering only the inherited prefix. Deletion/cleanup respects references.
- Read T's adopted pair afresh at every model-call boundary, including children
  and parallel calls. Do not pin one pair for an entire root run. Each started
  call pins its own selection; no call observes sibling now messages.
- C's normal run/step events stay in C and remain inspectable. T exposes compact
  lifecycle notifications and links for a separate progress group. Do not feed
  C's root lifecycle into T's main run progress or duplicate its step records.

## Implementation and Acceptance

Prerequisite work: immutable history selection/retention and control input
representation. Then add history/message projection, recorded runtime results,
and recall consumption; next implement compact coordination, compact.too,
budget preflight, and independent progress. Rules/guidance features build on
that shared delivery mechanism. No implementation phase is approved by merely
adding this document.

Likely files: `execution/records.py`, `types.py`, `store.py`, `history.py`,
`threads.py`, `events.py`, new `compaction.py`; executor `_persist.py`,
`prepare.py`, `steps/model.py`, `steps/tool.py`, `runs/agic.py`, and
`tools/runtime.py`; `up/core.py`, model adapters, API event routing, chat
presentation, and a packaged `compact.too` asset using existing discovery.

| Offline acceptance scenario | Required result |
| --- | --- |
| No result / later adopted result | Same read path: empty far/start boundary or referenced summary/cut. |
| Message-source round trip | Far, inputs, tool results, steer, cancel, and three recall variants reconstruct in order. |
| Initial/update/withdrawn/forgotten recall | Exact support only in supplied near + now; pending and far never count. |
| Stream cancel / runtime rejection | Partial output and tool outcomes survive restart without invented execution. |
| Unused/repeated history templates | Compute before selection; charge actual occurrences; no irrelevant compact. |
| Budget and large groups | Deterministic target, complete groups, final wire check, bounded failure. |
| Different consumer/compact models | Independent limits and consumer-side summary validation. |
| Multi-page compact / explicit history | Fixed range, prior adopted output, complete coverage, no native-message injection. |
| Concurrent human/run requests | One C execution; waiters reuse satisfying output after recheck. |
| Waiter cancel / owner death / commit crash | Ownership preserved or recovered; no partial adopted pair. |
| T append / rewind / retry during compact | Append excluded; changed covered prefix rejected; old calls remain inspectable. |
| Fork / compact-run retry / cleanup | References remain immutable and valid; future compaction streams are independent. |
| Later child/parallel calls | Latest adopted pair per call, isolated now, no duplicate child transcript. |
| History-changing continuation | Provider cannot retain superseded history through continuation reuse. |
| Incompatible store / missing origin | Store left unchanged; no fabricated human provenance. |
| Progress and cost | C remains separately observable and accounted, without corrupting T progress. |

Use fake models/token estimators and multi-process permit tests; default tests
remain offline. Run Ruff, formatting, type checks, and pytest before commits.

## Decisions Required Before Implementation

1. Choose the immutable record-version and durable boundary encoding. It must
   replace retry's destructive history semantics and support audit selection;
   avoid inventing a public Pointer syntax before this prerequisite is approved.
2. Define explicit human/runtime origin encoding and whether unconsumed steer
   is offered to later runs as clearly unapplied input or remains audit-only.
3. Confirm budget defaults and automatic compact charging: keep_ratio 0.5 is a
   proposal; safety/summary caps and C's relation to requester limits must be
   explicit at call sites, not hidden core defaults.

Until those choices are confirmed, the design records the agreed behavior and
acceptance constraints but does not claim the affected phases are ready to ship.

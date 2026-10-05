# Define Independent Root Spawning

Status: Proposed; feature definition, group 4 of 4. The human selected spawn for
independent root execution, returning a handle immediately and waiting only
explicitly. The complete definition remains subject to human approval.
This PR contains no implementation.

## Goal, Scope, and Dependencies

Let a Flow start another root run in the same agent/executor, retain or discard
its Future, and optionally await it later. The spawned root is not canceled just
because its launching run finishes, fails, is canceled, or performs exec.

Implement after [group 1](https://github.com/openhat-ai/toolang/pull/684)'s
call/value rules and [group 2](https://github.com/openhat-ai/toolang/pull/685)'s
Future/await support.
Await blocks are not a dependency. Reuse the same Future representation and
handle-await behavior for child and root targets; only ownership differs.

Scope is authored spawn with named/inline targets, local root admission,
durable causal links, independent lifecycle, and existing inspection/control
surfaces. Exclude cross-agent dispatch, new threads, process detachment,
background daemons, transport selection, and new CLI/API launch flags.

## Verified Current Behavior

- RunExecutor.run accepts a root with parent=None and immediately returns a
  LocalRunHandle; its task is owned independently in the executor registry.
- LocalRunHandle shields the target task while a caller waits. RunClient handles
  expose wait(), but neither handle is a language Future.
- Child binding inherits its root ID/thread/limits. Root limit accounting is
  owned per root tree, and each root can be canceled through existing controls.
- RunStore commits root admission and its control transactionally, rejects
  duplicate run/request IDs, and supports existing thread IDs. Model child
  admission already has an atomic receipt pattern.
- Executor.stop cancels and drains all owned roots. The Script CLI calls stop
  when its top-level invocation ends; terminal Chat/API have different host
  lifetimes. Root independence currently does not imply process independence.

## Syntax, Inputs, and Binding

```too
let research = spawn investigate
let spawn record_audit

run draft

let evidence = await research
run: Improve {{_}} using {{evidence}}.
```

| Form | Behavior |
| --- | --- |
| `spawn R` | Accept/start a root; put Future<T> in _ |
| `let job = spawn R` | Accept/start a root; put Future<T> in job only |
| `let spawn R` | Accept/start a root; discard handle and preserve all locals |
| `await job` | Wait; replace job with its complete result |
| `let value = await job` | Wait; write value, retaining job if names differ |
| `let await job` | Wait and discard result; retain job |

Spawn uses run's direct target forms: `spawn R` or
`spawn [-> T]: BODY`. Inline output defaults to Text and using is forbidden.
No async modifier is needed; `async spawn` is outside the allowed async-run
grammar. Bare spawn's _ contains a future, so a Flow with an ordinary output
contract must await it, overwrite it, or use a named/discarded spawn form.

Resolve and authorize the target once at launch using run's baseline-contract,
live named-target, and pinned inline-code rules. Preserve the existing check
against calling the current/ancestor runnable at launch. Bind declared inputs
from a captured local snapshot before changing any result binding. Contract or
admission failure fails the spawn Step and creates no future.

## Root Context and Ownership

- Use a new root Run ID and parent=None, with its own root accounting and task.
  Do not attach it as an execution child of the originating Step.
- Keep the same agent, executor, and thread. Do not silently create or switch
  threads. The root's entry control records the originating spawn Step as
  triggered_by; this causal link is separate from Run.parent.
- Resolve concrete context at the spawn boundary: caller's effective model
  request, workdir, authorized workspaces/ceilings, and inherited runnable
  settings, then apply ordinary target settings. Do not widen authority by
  falling back to unrestricted root defaults.
- Capture the adopted history horizon and follow normal root/thread history
  rules. Do not copy the caller's private in-progress conversation or repeat
  history into a new root execution frame. A target requiring unavailable outer
  iteration history must fail preflight; pass ordinary data as explicit inputs.
- Copy effective limit values as the new root's limits, with fresh root counters.
  Its usage is accounted to the new root, not the launcher's remaining budget.
  Existing agent/resource ceilings still apply; there is no new cross-root
  aggregate budget or global task pool.
- Once admission succeeds, launcher return/failure/cancellation/exec and waiting
  cancellation do not stop the spawned root. Only controls or limits belonging
  to that root, or executor/host shutdown, terminate it.
- Root failure remains inspectable and surfaces on an explicit await; it does
  not asynchronously fail the launcher. Admission errors still fail spawn.
  Repeated waits do not duplicate execution.

Independence lasts while the hosting executor is alive. Preserve current
shutdown behavior: short-lived Script CLI exit stops its executor and therefore
cancels outstanding spawned roots. Long-lived hosts can keep them running after
the originating run finishes. Do not promise that spawn survives command/process
exit, restart work automatically, or extend a CLI process by implicitly waiting.

## Admission, Records, and Recovery

- Add SpawnStmt with ordinary named/default/discard binding. Persist a run-kind
  launch Step whose result is the group 2 Future value; success means root
  admission. The spawned Run has its own terminal status and accepted contract.
- Atomically associate the new root admission, its causal origin, and the launch
  receipt. Generalize the existing receipt mechanism with explicit root origin;
  do not misuse the execution-parent field or relabel a child after acceptance.
- Use the originating run/Step/admission occurrence as a stable launch identity.
  Retry or a receipt-delivery failure recovers an existing matching acceptance
  instead of launching twice. A new intentional loop occurrence or whole-run
  rerun gets a new identity and may create a new root.
- A conflicting identity/spec must fail explicitly. Existing duplicate-request
  errors do not themselves provide idempotency; resolve the matching recorded
  acceptance before attempting a fresh insert.
- Restore committed spawn futures and await results from Step outputs. Recovery
  after admission but before result/event delivery uses the durable receipt.
  Never spawn merely because a future is read or awaited.
- Retain launch receipts while a spawned root references their causal origin.
  A retry/prune operation that would delete such a source Step must reject the
  cut with rerun guidance instead of leaving a dangling origin or deleting the
  independent root. Apply this check before mutating the store.
- Future result loading follows the target's recorded output type/provenance and
  group 2's missing/failed/canceled-target behavior. Root handles use the same
  value codec, without a second wrapper or serialized task/executor.
- Existing inspection shows the new root separately in its thread and links it
  to the source Step/control. Preserve costs/statuses per root. Do not imply a
  launch Step's success means the spawned run succeeded.
- Canceling the launcher immediately before admission creates no root; after
  committed admission the root survives if the executor remains alive. Preserve
  this durable boundary even if the caller never receives its handle.

## Implementation Touchpoints

- Upstream grammar and `src/toolang/lang/{ast,lower,contracts,flow_validation,
  format,description}.py`: SpawnStmt, target grammar, Future result inference,
  binding rules, and preflight validation.
- `src/toolang/execution/executor/{executor,common}.py` and new
  `stmts/spawn.py`: prepare concrete root context, admission receipts, separate
  task ownership, and ordinary output/binding projection.
- `src/toolang/execution/{store,records,events,types,schemas}.py`: durable root
  origin/receipt, request recovery, shared Future codec, and causal inspection.
  Reuse existing root controls instead of adding handle methods.
- Execution history/inspection/progress and lifecycle tests for Script and
  long-lived hosts; grammar/CST/highlighting/formatting tests and prepared caches.
  Keep file parsing, path defaults, and CLI orchestration out of core execution.

Publish/pin the grammar release, update syntax/examples with the host-lifetime
boundary, and generate the implementation changelog entry through the repository
runnable. No changelog entry is needed for this definition-only PR.

## Acceptance Tests

1. Parse/check/format named/inline/default/named-let/discarded-let spawn and all
   supported await combinations. Reject using, async spawn, invalid inputs,
   unauthorized targets, and Future-to-data coercion without creating a root.
2. With deterministic gates, prove spawn returns before completion and has
   captured inputs/context. Check target root parent=None, distinct root identity,
   same thread, causal launch link, and unchanged _ for named/discard forms.
3. Launcher success/failure/cancellation/exec and canceled waits leave an accepted
   root running in a live executor. Root-targeted cancellation works independently.
   Root errors propagate only when awaited; no unhandled task exceptions.
4. Verify inherited authority/workdir/settings and independent limits/accounting;
   no privilege expansion, shared remaining counters, or accidental iteration
   history inheritance.
5. Await typed scalar/array/nested/Part/struct results with original provenance.
   Reuse retained handles; confirm in-place materialization and binding failures.
6. Fault-inject before/after admission and before receipt delivery. Retry restores
   exactly the accepted root; mismatch fails; new loop occurrences and rerun
   intentionally launch new roots. No duplicate external side effects from
   replaying a committed launch.
   Reject retry cuts that would remove an origin still referenced by a root.
7. Verify causal inspection and source/root statuses independently. Executor
   shutdown cancels/drains all owned roots; Script CLI exit follows that rule,
   while long-lived hosts keep accepted roots after launcher completion.
8. Validate docs/examples/links and run all default repository checks for
   implementation; use offline fake providers and explicit synchronization gates.

## Risks and Approval

Risks are accidental parent cancellation, authority/default changes at the root
boundary, duplicate admission after faults, and confusing independent runs with
detached processes. Independent root budgets can multiply total work; preserve
existing ceilings and make accounting ownership visible.

No unresolved product decision is required for implementation under the explicit
same-agent/thread/host scope above. The complete definition, including these
context and lifecycle defaults, still requires human approval. No product code,
hosting change, or new process manager is part of this documentation PR.

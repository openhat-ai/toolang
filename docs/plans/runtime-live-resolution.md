# Live State resolution

Definition only: apply published agent, flow, and cap updates to ongoing Runs
while keeping active code stable. This replaces binding, resource-freezing,
and lineage rules in [latest-State binding](revision-refresh-calls.md) and
[Flow exec](flow-exec.md).
Their unrelated lifecycle rules remain. Syntax belongs to the separate
[grammar plan](https://github.com/openhat-ai/tree-sitter-toolang/pull/41).

## Rules

**Pin the active path, forbid reentry, and resolve other targets when used.**

1. Accepting a Run pins its runnable's code, directives, and types. Locks use
   module and runnable name along each branch's root-to-current path, including
   waiting ancestors, not whole files, siblings, completed children, or history.
   Inline agics keep their containing code; never relocate them by source line.
2. Before each model call, capture the latest published State and apply the
   active path's fixed directive rules to caps, hands, handoffs, instruct, and
   context. Inherit rules, not previously selected sets. Preserve rule scope
   and explicit restrictions; absence from an earlier set is not an exclusion.
   Referenced content can change while its selecting directive stays fixed.
3. At a named invocation, establish target identity from the accepted caller's
   definitions or the current model call's advertised catalog. The name must
   already exist there; no dynamic target expressions are added. Reject the
   current runnable or any ancestor, regardless of revision; omit these targets
   from advertisements. For other targets, capture latest published State once
   and resolve there. Preserve visibility, existing signature checks against
   the caller/catalog, input/output checks,
   and authorization. Missing or incompatible targets fail without fallback.
4. `run` creates a child and returns; `exec` replaces the current Run's binding
   and never returns on success, for both named and inline targets. Both check
   the path **before** the call. Only a committed exec removes the outgoing
   binding and its directives; ancestor bindings and rules remain. Failure before commit
   changes nothing. Earlier handoff targets are not on the path and may be
   called again; execution history is not a name blacklist.
5. A prepared model call and its resource/tool reads retain that call's snapshot;
   runnable invocations from its response use rule 3. Later boundaries may see
   newer State. Record code bindings and model-call dependency State separately
   for inspection/restoration; never substitute current data for past data.

Apply the same reentry check to every invocation: `run`, `exec`, `map`, other
collection/helper calls, inline agics, and runtime tools. Keep captured Setup,
external authority ceilings, Run identity, accounting, and the entry output
contract. No new publication barrier, revision
ordering, root-spawn API, or cross-agent behavior is introduced.

## Acceptance scenarios

`S1` and `S2` below label publication order, not comparable revision values.
Examples assume visible, authorized targets with compatible contracts and no
further publication during the sequence; handoff targets exist beforehand.

| Scenario after S2 is published | Required result |
| --- | --- |
| A model call is already prepared under S1 | Its prompts and resource reads stay fixed; the next call uses S2 dependencies. |
| Caps/instruct change; active `grow` code/directives also change | External selections refresh; active code and selector rules stay at S1. New eligible caps appear; explicit exclusions still apply. |
| `GENERATION` advances in instruct | The next model call sees it; this does not prove that active runnable code has advanced. |
| Old `grow` calls an idle helper or exported flow | The child uses S2. Its newly accepted code can introduce further static target names. |
| `run`, `exec`, `map`, or a helper targets current/ancestor `grow` | Reject before acceptance, even if S2 has a newer definition. |
| `grow@S1` executes `evolve@S2`, which executes `grow` | The same Run reaches `grow@S2`; the old grow is no longer on the path. |
| `evolve` is a child while ancestor `grow@S1` still waits | Executing or calling `grow` fails; the ancestor remains on the path. |
| `map` branches call the same worker, with publication between acceptances | Each branch checks its own path. Siblings may use S1 and S2 concurrently; self and shared-ancestor calls still fail. |
| Inline source moves; a target is deleted/renamed or becomes incompatible | Inline code stays with its owner. Unlocked target calls fail; rejected exec retains the outgoing binding. |

Also test module collisions, denied/unadvertised routes, missing required
dependencies, publication during acceptance, and snapshot restoration. Deleting
a target does not replace its active code, but future calls cannot fall back to
it. Use deterministic gates, including repeat and collection calls.

Use two stable names for evolution; both stages do useful work:

```too
flow grow:
  run: Complete useful work toward the goal.
  exec evolve

flow evolve:
  run: Review results, improve the definitions, and verify the changes.
  exec grow
```

Each exec replaces the current stage, so this cycle adds no ancestor frames.
The external entry stays `grow`; updates are adopted after publication without
versioned names. Neither stage can return to an ancestor through a child Run.

## Implementation and limits

Current code freezes dependencies and blocks historical handoff identities.
Change shared resolution and frame preparation in
`src/toolang/execution/executor/{executor,resources,frame,tool_runtime}.py` and
`runs/agic.py`; update records/store/schema and focused execution tests as needed.
Keep publication/visibility in `src/toolang/state/` and contract checks in
`src/toolang/lang/contracts.py`.

Valid intermediate States may publish between file writes; this is not a
multi-file transaction. Incompatible updates can fail a future call. No open
semantic alternatives; implementation and acceptance tests are a separate change.

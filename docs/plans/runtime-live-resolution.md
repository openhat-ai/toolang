# Live State resolution

Definition only: apply published agent, flow, and cap updates to ongoing Runs
while keeping active code stable. This replaces binding, resource-freezing,
and lineage rules in [latest-State binding](revision-refresh-calls.md) and
[Flow exec](flow-exec.md).
Their unrelated lifecycle rules remain. Syntax belongs to the separate
[grammar plan](https://github.com/openhat-ai/tree-sitter-toolang/pull/41).

## Rules

**Pin the active path; resolve everything else when used.**

1. Accepting a Run pins its runnable's code, directives, and types. Locks follow
   module-qualified identity along the root-to-current path, including waiting
   ancestors, not whole files, siblings, completed children, or exec history.
   Inline agics keep their containing code; never relocate them by source line.
2. Before each model call, capture the latest published State and apply the
   active path's fixed directive rules to caps, hands, handoffs, instruct, and
   context. Inherit rules, not previously selected sets. Preserve rule scope
   and explicit restrictions; absence from an earlier set is not an exclusion.
   Referenced content can change while its selecting directive stays fixed.
3. At invocation, establish target identity from the accepted caller's
   definitions or the current model call's advertised catalog. The name must
   already exist there; no dynamic target expressions are added. Use its locked
   definition if on the path; otherwise capture latest published State once
   and resolve there. Advertisements use the same locks. Preserve visibility,
   existing signature checks against the caller/catalog, input/output checks,
   and authorization. Missing or incompatible targets fail without fallback.
4. `run` creates a child and returns; `exec` replaces the current Run's binding
   and never returns on success. Both resolve against the path **before** the
   call. Only a committed exec removes the outgoing binding and its directives
   from that path; ancestor bindings and rules remain. Failure before commit
   changes nothing. An on-path name selects its pinned version; being on the
   path or in history is not itself a reason to reject a call.
5. A prepared model call and its resource/tool reads retain that call's snapshot;
   runnable invocations from its response use rule 3. Later boundaries may see
   newer State. Record code bindings and model-call dependency State separately
   for inspection/restoration; never substitute current data for past data.

Apply these rules to authored calls, collection/helper calls, inline agics, and
runtime tools. Keep captured Setup, external authority ceilings, Run identity,
accounting, and the entry output contract. No new publication barrier, revision
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
| A child calls ancestor `grow`, or `grow` directly executes itself | The target is `grow@S1`; exec does not unlock itself before resolution. |
| Root `grow@S1` executes `bridge@S2`, which executes `grow` | The same Run reaches `grow@S2`: the first handoff released the old grow lock. |
| The bridge is a child while ancestor `grow@S1` still waits | Executing `grow` selects S1; the ancestor lock remains. |
| A sibling still runs the helper at S1 | A new sibling can select S2; sibling bindings do not lock each other. |
| Inline source moves; a target is deleted/renamed or becomes incompatible | Inline code stays with its owner. Unlocked target calls fail; rejected exec retains the outgoing binding. |

Also test module collisions, denied/unadvertised routes, missing required
dependencies, publication during acceptance, and snapshot restoration. A deleted
target remains callable through a path lock, subject to contract/dependency
checks. Use deterministic gates, including repeat and collection calls.

## Implementation and limits

Current code freezes dependencies and blocks historical handoff identities.
Change shared resolution and frame preparation in
`src/toolang/execution/executor/{executor,resources,frame,tool_runtime}.py` and
`runs/agic.py`; update records/store/schema and focused execution tests as needed.
Keep publication/visibility in `src/toolang/state/` and contract checks in
`src/toolang/lang/contracts.py`.

Valid intermediate States may publish between file writes; this is not a
multi-file transaction. Incompatible updates can fail a future call. A bridge
is necessary to refresh an active same-name runnable, and cannot release an
ancestor's lock. No open semantic alternatives; implementation and acceptance
tests belong to a separate change.

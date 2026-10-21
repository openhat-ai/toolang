# Runnable Names and Builtin Compaction

## Status

Approved in the 2026-10-21 discussion: implement the agreed naming rules,
document their terminology, and fix builtin compaction ownership together.

## Goal

Give every runnable one layout-local fully qualified name (FQN), keep kind
separate from identity, and make automatic compaction independent of its caller
and of user-authored runnables named `compact`.

## Decisions

- An FQN is `module::runnable`. Module sources are the **builtin module** `_`,
  the **main module** `agent` from `agent.too`, and a **runnable module** named
  after `runnables/<module>.too`. Reserve `agent` and `_` as module filenames.
- A runnable has a name and a kind. Kind does not participate in its identity.
  Persist accepted FQNs and carry kind separately through controls, events,
  inspection, and live observations. Construct both from the accepted binding.
- A valid State already guarantees unique effective public names. Execute by
  name within the existing visibility rules; preserve State acceptance,
  shadowing, resource ownership, and driver selection behavior. Layout-level
  declarations can coexist even when one is excluded from effective State.
- Display `kind:name`, including `agic:` or `flow:` for an unnamed entry, using
  the Run's bound State. This string is a label, not another identity concept.
  Preserve existing source-generated entry/adhoc locators for durable binding.
- Keep public commands and flags. Existing typed input spellings may be decoded
  at input boundaries; execution resolves their name and checks any explicit
  kind separately. New accepted records and model discovery use names/FQNs.
- Read retained legacy records and State snapshots without rewriting them.
  Normalize legacy module names and recover legacy kind at decoding boundaries;
  emit the new representation for newly accepted work. No history reset,
  database migration, or source-directory rename is required.
- Automatic compaction explicitly binds builtin module `_` and runnable
  `compact`, with FQN `_::compact`. Its dedicated executor implementation keeps
  normal Run/Step persistence, ownership validation, cancellation, limits, and
  checkpoint recovery. It never resolves the public short name `compact`.
- `runnables/compact.too` contributes `compact::compact` and cannot replace
  automatic compaction. The builtin Talk handler is `_::talk` and retains its
  existing captured-source and user-handler precedence contracts.

## Scope and Touchpoints

- `lang/types.py`: parsing, canonical names, and explicit-kind display helpers.
- `state/{state,types,builtin,cache}.py`: module names, source validation,
  indexes, captured builtin code, and retained snapshot decoding.
- `execution/{runnables,calls,records,store,events,schemas,subscriptions,live}.py`
  and executor admission/handoff/spawn paths: accepted FQNs and separate kind.
- Compaction producer, checkpoint reader, and inspection: builtin identity.
- API request validation, CLI selectors/presenters, and derived observations:
  name lookup and labels using explicit kind.
- Focused current design/authoring documentation and the generated changelog.

Exclude changes to language syntax, module imports, source layout, scheduling,
models, compaction policy, and unrelated runtime behavior.

## Acceptance Checks

1. Root, file-export, private helper, builtin Talk, and anonymous runs retain
   their selected declaration and kind; newly accepted identities use FQNs.
2. Preparing/reloading State yields `agent`, file stems, and `_`; reserved
   filename collisions are rejected and existing visibility remains enforced.
3. API and model discovery accept effective names and expose kinds separately;
   UI output still shows `agic:name`, `flow:name`, and empty-name labels.
4. Legacy captured State and run records remain inspectable and rerunnable.
5. The builtin Talk compaction regression fails with the reported ownership
   error before the fix, then succeeds with `_::compact`; persisted checkpoints,
   replay, recovery, accounting, and internal progress suppression remain valid.
6. A user `compact` can be invoked normally while automatic compaction continues
   through the dedicated builtin implementation.
7. Default Ruff, formatting, type, and offline parallel test checks pass.

## Risks and Open Questions

Kind previously came from string parsing in several independent presenters;
missing propagation can misclassify Flow runs or expose internal compact steps.
Retained snapshots require normalization without changing their immutable source
or revision. Regression tests must cover both paths. No open product decisions
remain within this scope.

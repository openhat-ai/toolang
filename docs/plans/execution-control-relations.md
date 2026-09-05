# Execution control relations

## Goal and approved scope

Simplify preparation controls and persist explicit Step/control relationships.
This PR implements the approved PR2 definition; it does not change ModelCall
assembly, history membership, compaction, or resource loading.

## Decisions

- Rerun is a CLI/API convenience that creates an ordinary run control, without
  a persisted source link. Preserve restart validation at the caller boundary.
- Run retains initial inputs and effective settings. Retry records only its
  anchor, resources, limits, and model request; immutable facts come from the
  initial run control. Keep existing retry eligibility restrictions.
- Persist runnable identities as `module$kind:name`. Execute retains state,
  runnable, and input, without a duplicate module or source field.
- Control.triggered_by identifies the emitting Step. Step.preceded_by records
  controls adopted before begin; Step.aborted_by identifies cancel or immediate
  steer that interrupted execution. Preserve input as a separate data relation.
- Record runtime reload/execute calls as tool Steps, including their results.
- Persist partial model text and completed Parts on cancel, immediate steer,
  or stream failure. Exclude unfinished ToolCall placeholders from Step.output;
  preserving output does not execute calls or change message assembly.
- Include result delivery in interruption handling: close Parts exactly once
  and retain completed model/tool results even when delivery is canceled.
- Recall stores target, revision, and original content. Targets distinguish
  rules(workspace, path), skill(ref), and service(ref). Successful recalls are
  applied records; this PR adds persistence only, not loading or message output.
- Retry atomically removes the Step suffix and its unreferenced emitted runtime
  controls, retains other terminal controls, and marks remaining pending controls
  wontapply. Control indexes may have gaps and must not be reused.
- Increment the store schema; reject incompatible databases without modification.

## Touchpoints and acceptance

Update execution records, events, store, executor, protocol projections, and
their tests. Cover rerun, repeated retry and restart, qualified runnable identity,
empty execute input, recall codecs, runtime tool results, control ordering,
cancel/steer boundaries, partial output after restart, retry cleanup, and
non-reused control indexes.
Run Ruff check/format, ty, and the complete offline test suite.

History assembly from Step relations, rules preflight, pick tools, content
updates, compaction, and runspace remain separate follow-ups. In particular,
recent_conversation_messages retains its current membership policy in this PR.

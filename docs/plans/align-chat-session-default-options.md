# Align Chat Session Default Options

Status: Approved for implementation on 2026-09-02

## Goal

Align CLI vocabulary with setting lifetime: Chat startup establishes defaults
for multiple runs, while Script and rerun options affect one run and retry
preserves its persisted model.

This plan supersedes the Chat, retry, and rerun compatibility decisions in
`docs/plans/model-settings-across-surfaces.md` and the corresponding deferred
scope in `docs/plans/remove-script-default-option.md`. It does not change Agent
startup or Script behavior.

## Success Criteria

- Chat exposes `--default FIELD=VALUE` as its only startup model/runnable
  setting form and does not emit a deprecation warning for it.
- Chat rejects startup `--model` and `--runnable` as unknown options; `/model`,
  `/runnable`, `:model`, and `:runnable` keep their current behavior.
- Rerun exposes only `--model MODEL_BODY` for model replacement and rejects
  `--default` as an unknown option.
- Retry rejects `--default` as an unknown option and continues to preserve the
  persisted model.
- Agent `run`, `start`, and `serve` retain `--default FIELD=VALUE`, and Script
  retains `--model MODEL_BODY` without `--default`.

## Scope and Decisions

- Make Chat `--default` visible and canonical for initial session defaults.
- Remove Chat startup `--model` and `--runnable`; there is no compatibility
  alias because the affected CLI has not shipped in a tagged release.
- Remove hidden `--default` from retry and rerun, including compatibility
  parsing, warnings, and conflict diagnostics.
- Keep the shared `FIELD=VALUE` parser and the existing closed default fields:
  `model` and `runnable`.
- Preserve all model-body parsing, clearing semantics, policy options, local
  and remote Chat behavior, and persisted-run execution behavior.

## Implementation Touchpoints

- `src/toolang/cli/toolang/commands/chat/__init__.py` and `main.py`: expose the
  Chat default option and remove duplicate startup forms and compatibility
  branches.
- `src/toolang/cli/toolang/commands/thread.py`: remove retry/rerun compatibility
  options and simplify rerun model parsing.
- CLI tests: cover help, rejection, and unchanged canonical behavior.
- `docs/models.md` and `docs/api.md`: document the lifetime-based mapping.

## Acceptance Tests

1. Chat help includes `--default` and excludes `--model` and `--runnable`.
2. Chat `--default model=BODY` and `--default runnable=REF` produce the expected
   initial session setting without warnings.
3. Chat startup `--model`/`--runnable` and retry/rerun `--default` fail with
   standard unknown-option status 2 errors.
4. Rerun `--model BODY`, Agent startup `--default`, Script `--model`, Chat slash
   settings, and one-run colon settings pass their existing tests.
5. The default offline verification suite passes.

## Risks

- Development builds between the original public options and this change may
  have callers using either syntax. No tagged release contains these surfaces,
  so keeping one canonical form before 0.3.0 is preferred over shipping aliases.
- Chat default parsing is shared with Agent startup. Changes must remain at the
  Chat call site so startup behavior is unaffected.

## Open Questions

None.

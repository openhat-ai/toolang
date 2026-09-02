# Remove the Script `--default` Option

Status: Approved for implementation on 2026-09-02

## Goal

Make Script invocation syntax reflect its lifetime: a Script command selects
the model for one root run with `--model MODEL_BODY` and does not expose the
Setup-oriented `--default` vocabulary.

## Success Criteria

- Every dynamically generated Script agic and flow command rejects
  `--default` as an unknown option.
- `--model MODEL_BODY` remains the only Script CLI model-setting option and
  preserves the shared model-body behavior, including parameter-only bodies.
- Agent startup commands retain `--default FIELD=VALUE` because those values
  establish the Setup baseline for all runs in that runtime.
- Script `--allow` and `--limit`, local/remote execution, colon overrides, and
  environment-derived Setup defaults keep their current behavior.

## Scope and Decisions

- Remove the hidden `--default` option from generated Script runnable commands.
- Remove Script-only compatibility parsing, conflict diagnostics, and the
  deprecation warning for `--default model=...`.
- Let Click report its standard `No such option: --default` usage error. No
  custom migration fallback is retained.
- Keep hidden `--default` compatibility on Chat and rerun unchanged; removing
  those surfaces is outside this change.
- Do not change config, `TOOLANG_DEFAULT_MODEL`, agent `run`/`start`/`serve`,
  `/model`, or `:model` syntax.

## Implementation Touchpoints

- `src/toolang/cli/toolang/commands/script.py`: remove the generated option and
  its `default_options` plumbing; simplify Script session override construction
  to use only `--model`, `--allow`, and `--limit`.
- `tests/unit/cli/test_script_command.py`: remove compatibility expectations
  and cover rejection of `--default` plus unchanged `--model` behavior.
- User documentation already presents `--model` as the Script form; update it
  only if implementation review finds a remaining Script compatibility claim.

## Acceptance Tests

1. A Script command using `--default model=...` exits with CLI usage status 2
   and reports `No such option: --default` before any run starts.
2. The equivalent `--model 'REF effort=high'` command still builds the expected
   typed invocation override.
3. Script help lists `--model`, `--allow`, and `--limit`, and does not list
   `--default`.
4. Existing local and remote Script execution tests pass unchanged apart from
   removed compatibility plumbing.
5. The default offline verification suite passes.

## Risks

- This is an intentional immediate compatibility break for callers still using
  the hidden Script option. The standard error points them away from a syntax
  that was never shown in help; release communication should name `--model` as
  the replacement.
- Startup and Script commands share some policy parsers. The implementation
  must remove only Script option plumbing so startup `--default` remains intact.

## Open Questions

None. The requested scope intentionally removes Script compatibility without a
deprecation period and leaves other command categories unchanged.

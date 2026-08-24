# Default Script Progress Output

## Work Type and Approval

Feature implementation. The human approved this behavior on 2026-08-24 by
confirming the decisions below and requesting an implementation pull request.

## Goal and Success Criteria

Give Script one default execution-progress policy without a verbosity switch.
Progress is enabled unless the caller selects quiet mode. TTY and non-TTY
streams share progress semantics and differ only where terminal capabilities
require different rendering mechanics.

## Scope and Design

- Remove the Script `-v` and `--verbose` options and their internal verbosity
  plumbing. Former invocations that still pass either option fail through the
  normal unknown-option path.
- Enable prepare and execution progress by default regardless of whether
  stderr is a TTY.
- Keep the existing TTY presentation: ANSI styling, terminal-width adaptation,
  and replaceable Rich Live state.
- Keep the existing non-TTY presentation: no ANSI styling, cursor movement, or
  live replacement; emit only stable append-only progress.
- Use the same prepare event state and the same execution `ProgressProjector`
  for both stream types. Except for live-only state, ANSI, and width-dependent
  wrapping, their finalized content and ordering must agree.
- Make `-q` and `--quiet` suppress both prepare progress and execution progress,
  including the root Run footer.
- Keep actionable errors visible in quiet mode. Quiet is not a blanket stderr
  suppression mode.
- Preserve stdout result saving, log diagnostics, exit codes, execution events,
  durable records, Chat behavior, and retry or rerun behavior.

## Touchpoints

- `src/toolang/cli/toolang/commands/script.py`
- `tests/unit/cli/test_script_command.py`
- `tests/integration/cli/test_script_local.py`
- `docs/api.md`
- `docs/execution-presentation.md`

No execution-core or renderer change is expected because both existing
progress renderers already select TTY-appropriate mechanics from their output
stream.

## Acceptance Tests

1. Runnable help does not advertise `-v` or `--verbose`, and passing the
   removed option reports an unknown option.
2. Default non-TTY Script execution emits stable progress and its Run footer to
   stderr without ANSI or cursor-control sequences.
3. Default TTY Script execution retains colored Rich Live presentation and
   terminal-width adaptation.
4. Prepare progress receives a sink by default for both TTY and non-TTY stderr.
5. Quiet mode supplies no prepare sink and no execution tracer, while failures
   still emit one actionable diagnostic.
6. TTY and non-TTY execution use the same projected ownership, content,
   ordering, aggregates, errors, and footer facts.
7. Explicit result destinations, stdout bytes, exit codes, Chat, retry, and
   rerun behavior remain unchanged.
8. The default repository verification passes.

## Risks and Open Questions

Successful non-TTY commands now write progress to stderr by default. Callers
that require silent stderr must add `-q`. Removing `-v` is intentionally a
breaking CLI cleanup for callers that still pass it. There are no open
questions.

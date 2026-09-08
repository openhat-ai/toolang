# Chat Thread Option

Status: Approved for implementation on 2026-09-08.

## Goal and Success Criteria

Replace Chat's optional positional `THREAD` with `--thread [THREAD]`, with `-t`
as its short alias. Users can start a new conversation, resume the most recently
updated thread, or select a thread explicitly through one option. Provide a
reusable optional-value component for future options such as `--dev [PATH]`.

| Invocation | Behavior |
| --- | --- |
| `toolang alice chat` | Start a new session; create its thread on first input. |
| `toolang alice chat --thread` | Resume the most recently updated thread. |
| `toolang alice chat --thread term_ID` | Continue the specified thread. |
| `toolang alice chat --thread run_ID` | Continue that run's thread, as supported today. |
| `toolang alice chat -t [THREAD]` | Short alias with identical optional-value behavior. |

## Scope and Decisions

- Remove the positional form; `chat term_ID` becomes a usage error. Declare
  `--thread` as the primary option and `-t` as its alias. Help shows both with
  an optional value; documentation and diagnostics use `--thread` canonically.
- Build the reusable component under `src/toolang/cli/common`, with Chat as its
  first consumer. Changing `--dev` to accept a bare option and defining what
  that means are future work; its existing required-value behavior stays intact.
- Preserve deferred thread creation: help, session settings, EOF, and exit
  without input do not persist an empty thread.
- Resolve latest selection once, before entering Chat, within the selected
  agent's execution history. Resident, roaming, and visiting layouts remain
  isolated. Do not filter by thread prefix, origin, channel, or status.
- Use `RunHistory.list_threads(limit=1)`, whose existing ordering uses the
  projected `ThreadInfo.updated_at`, including recorded run activity. Reuse
  its existing tie ordering. Do not substitute database creation order or
  bare `ThreadRecord.updated_at` ordering.
- Use the existing read-only `open_execution` boundary, also used to resolve
  explicit run IDs. Pass a concrete thread ID or `None` into Chat; local,
  attached, and temporary guest execution keep their existing behavior.
- Bare selection with no history or no threads fails with a clear error and
  guidance to omit `--thread` to start a new session. Do not create a thread
  or launch a runtime in this case. Preserve incompatible-store diagnostics.
- Preserve explicit thread/run handling and errors. Selecting a task or chore
  thread does not reopen its work item or create a manual scheduled run.
- An explicitly empty value (`--thread=` or `-t ""`) is a usage error; it is
  distinct from a bare option. Repeated occurrences use the last occurrence.

## Reusable Optional-Value Design

The previous Typer 0.27.2 experiment at `/tmp/toolang-typer-tristate.py` uses a
command-local parser extension for optional values. Its 69 checks pass against
the repository's installed version. It deliberately supports only long options;
short-alias handling must be added for this feature.

- Adapt the experiment's explicit per-parameter configuration into a shared
  parser and composable command extension. Configuration identifies a parameter
  and its bare value, not an option spelling; all aliases share the behavior.
  Multiple scalar parameters on one command can opt in independently.
- Omission uses the declared default. A bare option supplies its configured
  value. Explicit input follows ordinary native conversion and validation.
  Configuration may supply text, integer, or path input to the corresponding
  native converter; it must not assume every option is a thread ID or a string.
- Keep bare values declarative and side-effect free. Domain decisions such as
  selecting a thread or locating a development wheel belong to the command's
  CLI orchestration, after parsing. The shared component does not read history,
  discover paths, start runtimes, or import Chat.
- Integrate through existing command classes and the lazy factory, preserving
  required-agent routing, native help, completion, errors, and parameter-source
  tracking. Only explicitly configured scalar options gain optional values;
  unsupported flags, counters, repeated-value parameters, and multi-value
  parameters fail configuration validation.
- Support separated values for either alias, `--thread=term_ID`, and the native
  short attached form `-tterm_ID`.
- A bare alias at end of arguments, before another option, or before `--`
  selects latest. For example, `chat -t --sandbox host` and
  `chat --thread --default model=MODEL` preserve the following option.
- Explicit attached values remain values even when they begin with `-`.
  Unknown following options still fail. Tokens after `--` retain normal
  end-of-options semantics and cannot become a positional thread.
- For Chat, configure a private bare-selection marker distinct from omission
  and all command-line string values, including explicit empty input. Resolve
  it at the CLI boundary only. Do not add a public `latest` keyword or an
  environment default for thread selection. Rejecting an empty thread ID is
  Chat's policy; the reusable component preserves explicit empty input.
- Keep the extension within the pinned Typer implementation; no external Click
  parser, global monkeypatch, dependency change, or root argument rewrite.

## Implementation Touchpoints

- `src/toolang/cli/common/options.py`: reusable optional-value configuration,
  parser, and command extension; keep private Typer dependencies here.
- `src/toolang/cli/toolang/commands/chat/__init__.py` and
  `src/toolang/cli/toolang/main.py`: declare the canonical option and alias,
  configure the shared extension, and retain the existing lazy registration.
- `src/toolang/cli/toolang/commands/chat/main.py`: resolve the three states and
  latest-history errors before opening the session.
- `tests/unit/cli/test_optional_value_options.py`: independent reusable-component
  tests, including native text, integer, and path conversion.
- `tests/unit/cli/test_chat_command.py`, `test_cli_help.py`, and
  `test_cli_routing.py`: parser, selection, help, and agent-routing coverage.
- `docs/api.md`: replace positional examples and describe latest selection.

No runtime, HTTP API, history schema, scheduling, or TUI behavior changes.

## Acceptance Tests

1. `--thread` and its `-t` alias exercise omitted, bare, and explicit selection
   through the actual lazy CLI entry point. Help contains `[THREAD]` only as an
   option value; canonical examples and diagnostics use `--thread`.
2. Cover separated/attached values, following known/unknown options, `--`,
   explicit empty values, repeated aliases, and rejection of positional IDs.
   Ordinary options still require values and retain their parsing behavior.
3. Latest selection follows projected update time, including a run update that
   changes the order relative to thread-record timestamps. Cover the existing
   tie ordering and a latest thread with a non-terminal prefix.
4. Missing and empty history produce an error without creating a thread or
   starting a runtime. Explicit run IDs retain existing resolution and errors.
5. Resident, roaming, and visiting routes use the selected history; local and
   remote session startup receive the resolved ID. Omitted selection still
   creates exactly one thread on first submission and none on help or exit.
6. A standalone test command reuses the component with text, integer, and path
   options. Cover multiple configured parameters, aliases, typed bare values,
   explicit empty strings, conversion errors, completion, Rich/plain help,
   environment/default precedence, parameter sources, and nested dispatch.
   A bare option must not execute filesystem discovery during parsing or help.
7. Configuration rejects unsupported parameter shapes. Unconfigured sibling
   options and commands retain native behavior; product `--dev` still requires
   its path value. No Chat import is needed to use the shared component.
8. Updated documentation examples match the parser. Default verification passes:
   `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and
   `uv run pytest`. Live-provider tests remain opt-in.

## Risks and Open Questions

- Positional callers must migrate to `--thread THREAD` (or its `-t` alias).
- The parser extension depends on private Typer APIs; alias and boundary tests
  must protect future upgrades. The existing version pin remains unchanged.
- Another process may update history after selection. Select a snapshot once;
  do not switch threads during the session.
- No unresolved design questions.

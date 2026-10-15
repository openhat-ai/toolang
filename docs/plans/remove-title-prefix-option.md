# Remove the terminal title prefix CLI option

Status: approved for implementation by the human on 2026-10-15.

## Goal and success criteria

Reduce CLI option clutter by removing `--title-prefix` while retaining per-type
configuration and environment overrides. Every built-in command stops exposing
or consuming the option, and existing configured terminal titles remain intact.

## Decisions and scope

- Remove the option from Chat, Talk, Top, agent/Hub Serve, Script Run, Retry,
  and Rerun, including generated script group/runnable commands and both
  `too file.too` and `too run file.too` dispatch forms.
- Remove `TitlePrefixOption` and its internal override plumbing, including
  script argument injection/inheritance and the `option` input to
  `resolve_title_prefix`. Keep resolution at the CLI boundary and pass a
  concrete prefix to the title publisher.
- Retain `[terminal.title.prefix]` keys `chat`, `talk`, `top`, `serve`, `run`,
  `retry`, and `rerun`. Precedence becomes `TOOLANG_TITLE_PREFIX_<TYPE>` >
  agent-home config > root config > empty string. Root-only commands skip
  agent-home configuration; roaming/scripts retain their current ordered
  project `toolang.toml` layers and process-only environment behavior.
- Preserve per-key inheritance, explicit empty-string clearing, exact printable
  spaces, validation errors, and existing dotenv precedence. Title labels,
  sanitization, clipping, publication eligibility, lifecycle, and pane marks
  retain their existing behavior.
- Remove the option outright, without a hidden compatibility alias. Built-in
  option parsing rejects it; retain existing script-authored argument semantics.
  Migrate one-off uses to an environment assignment, for example
  `TOOLANG_TITLE_PREFIX_CHAT='[too] ' too alice chat`. Persistent users keep
  their current TOML settings. The tradeoff is breaking existing invocations
  that use the flag; environment assignments retain one-off control.
- Update current API documentation and supersede only the CLI-option decisions
  in [the pane/title plan](tmux-pane-metadata.md#prefix-configuration).
  Record the removal and migration in `CHANGELOG.md` through the repository's
  `too aid.too update_changelog` workflow; preserve historical release entries.

## Implementation touchpoints

- `src/toolang/cli/common/parameters.py`, `terminal_session.py`, and
  `src/toolang/cli/config.py`: remove the override surface.
- `src/toolang/cli/toolang/commands/`: update `chat/__init__.py`, `chat/main.py`,
  `talk/__init__.py`, `top.py`, `hub.py`, `runtime.py`, `thread.py`, and
  `script.py` using the current factories and entry points.
- `tests/unit/cli/test_terminal_session.py`, `test_terminal_commands.py`,
  `test_chat_command.py`, `test_script_command.py`, and
  `tests/integration/cli/test_terminal_pane.py`: adapt signatures and assertions.
- `docs/api.md`, `docs/plans/tmux-pane-metadata.md`, and `CHANGELOG.md`:
  document the new public contract and migration.

## Acceptance and verification

1. Command help, including script group/runnable help for both dispatch forms,
   contains no built-in `--title-prefix`; representative invocations fail with
   an unknown-option error before starting a service or execution.
2. All seven kinds resolve environment/config precedence correctly, including
   missing values, explicit empty values, spaces, independent keys, and invalid
   configuration errors even when an environment override exists.
3. Resident, root-only, and roaming/script cases retain their source boundaries;
   script invocation forms produce the same prefix from identical inputs.
4. Existing title publication and pane-mark checks pass without CLI overrides.
5. Run `git diff --check` and the four default code checks listed in
   [AGENTS.md](../../AGENTS.md#verification) before implementation commits.

## Risks and open questions

The compatibility risk is existing shell aliases/scripts using the removed flag;
the migration above preserves their behavior. Generated script dispatch needs
coverage to avoid retaining an inherited option or consuming authored arguments.
No design questions remain.

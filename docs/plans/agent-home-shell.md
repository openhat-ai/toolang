# Open an Interactive Shell in an Agent Home

## Status

Proposed for review; feature definition only. Do not implement before human approval.

## Goal and Success Criteria

Let a user enter a resident agent's home without printing and copying its path.
`too AGENT shell` starts an interactive child shell with its working directory set
to that agent's home. The command must not start an interactive shell from a
non-interactive invocation.

## Verified Baseline

- Resident homes are `${TOOLANG_ROOT}/agents/<agent>/`; `--root` and
  `TOOLANG_ROOT` select the root, with `~/.toolang` as the default.
- CLI commands support a resident-agent prefix (`too AGENT COMMAND`), including
  explicit `agent:NAME` selectors. There is no command for opening an agent home.
- Toolang supports macOS and Linux; current root help has an `Agent Commands`
  panel.

## Decisions

1. Add `too AGENT shell` for resident agents only, using existing target routing
   and root selection. Require an explicit agent; do not infer a “current” agent
   or add another spelling in this change.
2. Show `shell` in the root `Agent Commands` panel after `info` and before
   `serve`, with the help description `Open a shell in the agent's home`.
3. Launch `SHELL -i` as an interactive child process, with inherited
   stdin/stdout/stderr and environment, and `cwd` set to the resolved agent
   home. If `SHELL` is unset, use `/bin/sh`; report an error if the selected
   executable cannot be launched. Do not load agent `.env` files or start the
   agent runtime.
4. Require both stdin and stdout to be TTYs. Otherwise exit with a concise
   diagnostic before launching the shell. Do not allocate a PTY; inherit the
   caller's terminal and propagate the child shell's exit status.
5. Keep this command interactive-only. A non-interactive `exec` mode or a
   command that prints the home path is out of scope.

## Scope and Touchpoints

- `src/toolang/cli/toolang/main.py` and `src/toolang/cli/toolang/routing.py`:
  register `shell` after `info` in Agent Commands with the stated help
  description and resident target-first route.
- `src/toolang/cli/toolang/commands/shell.py`: resolve the selected layout,
  check TTYs, and launch the child shell; keep shell process handling in the
  CLI layer.
- CLI routing and command tests: cover target/root selection, TTY refusal,
  launch cwd/environment/streams, and child exit status without starting a
  real interactive shell.
- `README.md`: add the command to common commands and explain its interactive
  behavior.

## Acceptance Tests

1. `too alice shell` starts the configured shell in the resolved home for
   `alice`; `--root` and `TOOLANG_ROOT` select the expected home.
2. Missing/invalid agent and shell-launch failures report CLI errors without
   starting a process.
3. If stdin or stdout is not a TTY, the command exits nonzero with a useful
   message and does not invoke the shell.
4. The child inherits standard streams and process environment, receives no
   agent `.env` values, and its exit status is returned by the CLI.
5. Root help shows `shell` in the stated position with the stated description;
   README shows the command, and existing command routing and help remain
   unchanged.

## Risks and Open Questions

The `$SHELL` value may not match the shell that launched the CLI; this design
intentionally uses the configured user shell, falling back to `/bin/sh` only
when `$SHELL` is unset. No open questions remain for this scoped proposal.

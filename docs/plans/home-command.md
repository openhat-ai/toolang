# Home Command

Approved for implementation on 2026-10-01. Supersedes the naming, help, and
placement restrictions in [agent-home-shell.md](agent-home-shell.md) and
[root-shell.md](root-shell.md).

## Behavior

- Rename `too [AGENT] shell` to `too [AGENT] home`, without a `shell` alias.
  Keep its Agent Commands position. Description: `Open a shell in agent home`.
  Argument help: `Agent name, .too file, reference, or URL`.
- Support resident names, visiting references/URLs, and roaming `.too` paths
  before the command. Open a local shell in the selected `layout.home`.
- Prepare nonresident homes through existing program preparation, including
  roaming projection and visiting fetching/cache rules. Do not start a runtime.
- Omitting AGENT retains the existing root fallback and root precedence;
  do not mention this fallback in public help or README descriptions.
- Preserve shell selection, TTY checks, inherited environment/streams, errors,
  exit status, and placement-specific global-option rules. Missing explicit
  targets never fall back to root. Do not load dotenv values into the shell.

## Changes and Acceptance

Update CLI registration/routing, rename the shell command module and tests to
home, and update README. Reuse existing layout/preparation APIs.

Tests cover all placements, first/repeated nonresident preparation, omitted
agent/root precedence, invalid targets and preparation failures, TTY refusal,
shell fallback/errors/exit status, exact help, and `agent:home` disambiguation.
Keep tests offline; run default Ruff, formatting, ty, pytest, and diff checks.

Risks: old `shell` invocations break; `home` becomes reserved. Nonresident
preparation may write generated files or fetch/refresh remote source.
No open questions.

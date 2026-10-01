# Home Shell Entry Message

Approved by the user on 2026-10-01; replaces the proposed prompt marker.

- Immediately before launching an interactive shell, print one plain-text line:
  - Agent selected: `Entered agent home. Type exit to return.`
  - No agent: `Entered Toolang root. Type exit to return.`
- Print only after target preparation and TTY validation succeed. Retain normal
  shell-launch errors if starting the process subsequently fails.
- Do not add a marker, styling, persistent prompt changes, or shell adapters.
  Clearing or resetting the terminal removes the message; it is not reprinted.
- Preserve all existing shell selection, directory, environment, and exit behavior.

Update `cli/toolang/commands/home.py` and its existing tests. Verify exact output
and ordering for every placement and root scope, plus no message for non-TTY
calls. Run the default checks. No open questions.

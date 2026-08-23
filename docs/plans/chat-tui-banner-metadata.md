# Chat TUI Banner Metadata

Status: Implemented

## Goal and success criteria

Keep the Chat TUI banner metadata in a compact two-column label/value grid,
align the version with the other values, and name the execution location in
terms that also work for a future remote chat client. This follow-up supersedes
the identity-row and executor-value presentation defined by
`chat-tui-banner.md`.

The change succeeds when:

- bold bright-cyan `Toolang` appears in the first column with the complete dim
  `v<exact-version>` in the second column;
- the dim `home` key and its normal-style resolved, abbreviated agent-home
  value appear on the next row;
- the dim `executor` key and normal-style `embedded` value appear on the next
  row for the process-local client;
- the executor value can instead render `at <endpoint>`, including
  `http://localhost:7001` and remote `https://` endpoints;
- `Toolang`, `home`, and `executor` share the first column, while the version,
  home, and executor values share a second aligned column;
- wide and narrow layouts continue to fit or fold the complete metadata
  without clipping; and
- logo, Toolang/version, panel, path abbreviation, status bar, and chat
  behavior otherwise remain unchanged.

## Scope and design

The wide banner keeps the logo and details side by side. All three detail rows
use one two-column grid. The existing bold bright-cyan `Toolang` styling
remains. The `home` and `executor` labels and complete `v`-prefixed version are
dim; the home directory and complete executor value use the normal foreground.

```text
╭────────────────────────────────────────────────────────╮
│                                                        │
│  ████           ██    Toolang   v0.3.0                 │
│   ██   ⬤   ⬤    ██    home      ~/.toolang/agents/eve  │
│   ██          ████    executor  embedded               │
│                                                        │
╰────────────────────────────────────────────────────────╯
```

`ChatClient` exposes an executor display value. The embedded implementation
returns `embedded`; a future HTTP implementation returns
`at <sanitized-base-endpoint>`. The banner owns the stable `executor` key and
treats the value as opaque display text, so an HTTPS endpoint requires no
layout or presentation change. Remote connection setup, endpoint discovery,
and endpoint sanitization remain owned by the future HTTP client and are not
part of this change.

The current responsive decision remains: render side by side only when the
logo and the longest complete detail line fit at the wide threshold; otherwise
stack details below the logo. Long home or executor values may fold inside the
value column but must not be clipped.

## Implementation touchpoints and acceptance tests

- `src/toolang/cli/toolang/commands/chat/base.py`: add the executor display
  suffix to the chat-client contract.
- `src/toolang/cli/toolang/commands/chat/local.py`: identify the process-local
  executor as `embedded`.
- `src/toolang/cli/toolang/commands/chat/blocks.py`: accept the executor suffix,
  render all three detail rows in one two-column grid, prefix and dim the
  complete version, and calculate responsive width from the labels and longest
  value.
- `src/toolang/cli/toolang/commands/chat/tui.py`: pass the active client's
  executor suffix into the header.
- `tests/unit/cli/test_chat_tui.py`: cover the exact embedded and HTTP/HTTPS
  values, version prefix, column alignment and styles, row order, wide layout,
  and narrow folding.
- `tests/unit/cli/test_chat_command.py`: keep scripted test clients conformant
  with the expanded client contract.
- `tests/system/cli/test_chat_tui_e2e.py`: continue to cover process-local
  startup with the new embedded label and unchanged chat behavior.

Acceptance requires the focused banner tests and the default repository
verification to pass. Risks are widening the banner unexpectedly for endpoint
text, leaking unsafe endpoint components from a future client, and making test
clients incomplete when the protocol grows. The client remains responsible for
supplying a safe suffix, and focused test clients must declare their execution
location explicitly.

## Out of scope

- Implementing an HTTP ChatClient or remote execution transport.
- Selecting, discovering, validating, or authenticating an endpoint.
- Changing the `info` command, home-path abbreviation, logo, resolved version,
  panel, status bar, transcript, or run behavior.

## Open questions

None.

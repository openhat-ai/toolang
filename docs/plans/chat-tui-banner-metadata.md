# Chat TUI Banner Metadata

Status: Implemented

## Goal and success criteria

Keep the Chat TUI banner metadata in a compact two-column key/value grid and
name the execution location in terms that also work for a future remote chat
client. This follow-up supersedes the executor value defined by
`chat-tui-banner.md` while retaining its keyed metadata layout.

The change succeeds when:

- the dim `home` key and its normal-style resolved, abbreviated agent-home
  value appear on the first metadata row;
- the dim `executor` key and normal-style `embedded` value appear on the next
  row for the process-local client;
- the executor value can instead render `at <endpoint>`, including
  `http://localhost:7001` and remote `https://` endpoints;
- the keys share one column and the values share a second aligned column;
- wide and narrow layouts continue to fit or fold the complete metadata
  without clipping; and
- logo, Toolang/version, panel, path abbreviation, status bar, and chat
  behavior otherwise remain unchanged.

## Scope and design

The wide banner keeps the logo and details side by side. The details area has
the Toolang/version identity row followed by a two-column key/value grid. The
existing bold bright-cyan `Toolang` styling remains. The `home` and `executor`
keys are dim; the version, home directory, and complete executor value use the
normal foreground.

```text
╭────────────────────────────────────────────────────────╮
│                                                        │
│  ████           ██    Toolang 0.3.0                    │
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
  render the home and executor values in a keyed two-column grid, and calculate
  responsive width from both keys and the longest value.
- `src/toolang/cli/toolang/commands/chat/tui.py`: pass the active client's
  executor suffix into the header.
- `tests/unit/cli/test_chat_tui.py`: cover the exact embedded and HTTP/HTTPS
  values, key/value alignment and styles, row order, wide layout, and narrow
  folding.
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
- Changing the `info` command, home-path abbreviation, logo, version, panel,
  status bar, transcript, or run behavior.

## Open questions

None.

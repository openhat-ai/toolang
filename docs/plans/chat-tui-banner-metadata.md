# Chat TUI Banner Metadata

Status: Proposed.

## Goal and success criteria

Simplify the Chat TUI banner metadata into one unlabelled text column and name
the execution location in terms that also work for a future remote chat client.
This follow-up supersedes only the `home` and `executor` key/value presentation
defined by `chat-tui-banner.md`.

The change succeeds when:

- the resolved, abbreviated agent-home directory appears as one normal-style
  line with no `home` key or key/value indentation;
- an embedded client appears on the next line as the normal-style text
  `executor embedded`;
- the banner can render a remote client on that line as
  `executor at <endpoint>`, including `http://localhost:7001` and remote
  `https://` endpoints;
- the home and executor lines remain in that order and share one text column;
- wide and narrow layouts continue to fit or fold the complete metadata
  without clipping; and
- logo, Toolang/version, panel, path abbreviation, status bar, and chat
  behavior otherwise remain unchanged.

## Scope and design

The wide banner keeps the logo and details side by side. The details column is
now three normal text rows: Toolang/version, the home directory, and the
executor description. The existing bold bright-cyan `Toolang` styling remains;
the version, home directory, and complete executor description use the normal
foreground. There is no nested key/value table.

```text
╭────────────────────────────────────────────────────────╮
│                                                        │
│  ████           ██    Toolang 0.3.0                    │
│   ██   ⬤   ⬤    ██    ~/.toolang/agents/eve            │
│   ██          ████    executor embedded                │
│                                                        │
╰────────────────────────────────────────────────────────╯
```

`ChatClient` exposes an executor display suffix. The embedded implementation
returns `embedded`; a future HTTP implementation returns
`at <sanitized-base-endpoint>`. The banner owns the stable `executor ` prefix
and treats the suffix as opaque display text, so an HTTPS endpoint requires no
layout or presentation change. Remote connection setup, endpoint discovery,
and endpoint sanitization remain owned by the future HTTP client and are not
part of this change.

The current responsive decision remains: render side by side only when the
logo and the longest complete detail line fit at the wide threshold; otherwise
stack details below the logo. Long home or executor lines may fold inside the
panel but must not acquire key/value indentation or be clipped.

## Implementation touchpoints and acceptance tests

- `src/toolang/cli/toolang/commands/chat/base.py`: add the executor display
  suffix to the chat-client contract.
- `src/toolang/cli/toolang/commands/chat/local.py`: identify the process-local
  executor as `embedded`.
- `src/toolang/cli/toolang/commands/chat/blocks.py`: accept the executor suffix,
  replace the key/value grid with one details column, and calculate responsive
  width from the complete normal-text lines.
- `src/toolang/cli/toolang/commands/chat/tui.py`: pass the active client's
  executor suffix into the header.
- `tests/unit/cli/test_chat_tui.py`: cover the exact embedded and HTTP/HTTPS
  text, absence of the `home` key, row order, normal styles, wide layout, and
  narrow folding.
- Existing command and system tests continue to cover process-local startup and
  unchanged chat behavior.

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

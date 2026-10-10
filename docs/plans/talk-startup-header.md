# Chat and Talk startup headers

Status: Proposed; awaiting confirmation of the complete definition.

## Goal and examples

Add a Talk startup header and version captions to both clients. Illustrative
wide layouts:

```text
╭─ Chat v0.4.0a2 ──────────────────────────────────────────────╮
│                                                              │
│  ████        ██    runtime     v0.4.0a2-25-g7297ecfd         │
│   ██  ⬤  ⬤   ██    sandbox     host · macOS 27.0.1 arm64     │
│   ██        ███    workspaces  lab                           │
│                                                              │
╰──────────────────────────────────────────────────────────────╯

╭─ Talk v0.4.0a2 ──────────────────────────────────────────────╮
│                                                              │
│  ████        ██    hub    v0.4.0a2-25-g7297ecfd              │
│   ██  ⬤  ⬤   ██    convo  gc_rcpya1zw                        │
│   ██        ███    user   bryan                              │
│                                                              │
╰──────────────────────────────────────────────────────────────╯
```

Only observers add a suffix:

```text
user   bryan · view only
```

## Behavior

- Caption version comes from the local Toolang process; `runtime` and `hub`
  come from their servers. Show both even when equal. Keep complete revisions
  and dirty markers, one `v` prefix for known versions, and bare `unknown`.
- `convo` contains only the complete ID, including pending DMs. `user` has no
  suffix when sending is allowed. Opening a pending DM does not create it.
- Preserve the existing cyan logo, dim border/keys, and padding. Captions and
  values use normal foreground, including the full `bryan · view only` value.
- Talk prints once before messages and scrolls away, with one blank line after
  the panel. Reconnects, redraws, Ctrl+L, and pane reuse do not reprint it.
  Directory and one-shot sends have no header. Footers, titles, and placement
  retain their behavior; permissions in the header are an entry-time snapshot.
- Keep the current width cap and 69-cell side-by-side threshold. Stack fields
  below the logo when needed. Move an overlong caption inside above the logo;
  fold complete values. At extreme widths, omit decoration. Sanitize terminal
  controls, render literal text, and measure Unicode cells.

## Version data and scope

Add Hub `GET /info` with typed `HubInfo {version: str}`: a nonempty printable
single-line version resolved by `toolang_version()` in Hub composition and
passed into the app factory. Keep existing identity checks and `/healthz`'s
exact `{"ok": true}` response. Talk fetches once after placement, with a two-second
total deadline, no retry, and additive-field tolerance. Unsupported, failed, or
invalid metadata displays `unknown`; never substitute the local version.

Chat retains its runtime rows and startup spacing. This proposal supersedes
only the no-caption/client-version decision in
[chat-runtime-header.md](chat-runtime-header.md); messaging follows the existing
[contract](../messaging.md).

Likely files: `teaming/{schemas,api,client}.py`, `up/hub.py`, Talk composition,
TUI and new `talk/header.py`, Chat TUI and `blocks.py`, and a shared pure
`cli/common/banner.py` renderer, all under `src/toolang`. Keep field meanings
with their command. Update affected tests, `docs/chat.md`, `docs/messaging.md`,
and the changelog through `too aide.too update_changelog` when implemented.

## Acceptance and risks

Verify the examples for DM, GC, pending DM, member, and observer; independent
client/server versions; unavailable/malformed metadata; unchanged Hub readiness;
once-only startup; narrow/Unicode/no-color rendering; and unchanged Chat runtime
rows and messaging behavior. Run repository default checks for implementation;
this documentation-only definition requires valid references and `git diff --check`.

Risks: the permission snapshot can become stale, version lookup can add two
seconds, and shared layout changes can affect Chat. The live footer/composer
remains authoritative; cover both renderers. Open questions: none within this
proposal. No implementation is included.

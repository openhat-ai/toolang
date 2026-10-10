# Chat runtime header

Status: Approved and revised in the implementation requests on 2026-10-10.

## Goal and scope

Describe the connected runtime and its available workspaces in the startup
banner. This supersedes the metadata presentation in
[chat-tui-runtime-banner.md](chat-tui-runtime-banner.md) and the caption text in
[chat-tui-version-caption.md](chat-tui-version-caption.md).

## Decisions

- No frame caption or client version; use `too -V` to inspect the client version.
  Retain the logo, panel styling, padding, and responsive folding.
- Rows, in order: `runtime`, `sandbox`, `workspaces`.
- Runtime: always show only `<version>`, including matching, dirty, and unknown
  versions. Known versions have exactly one `v` prefix. Omit the endpoint and
  its separator/link from the banner; use `too AGENT info` to inspect the API
  endpoint. Retain endpoint metadata for connection and validation.
- Sandbox: `<driver> · <environment>`. Host uses the existing OS description;
  Docker uses the image selector after `docker:`. Do not display container IDs.
  Runtime identity validation and the API's instance field remain unchanged.
- Workspaces: a startup snapshot of available logical names from the connected
  runtime's `GET /api/v1/workspaces`, preserving response order. Exclude
  unavailable entries and filesystem paths. Show `none` for an empty list and
  `unavailable` if this presentation-only request fails or is malformed; never
  infer remote availability from the client's filesystem. Ignore additive fields.
- Remove the local agent-home value from the TUI banner and its input plumbing.
  Current workspace selection remains in the existing dynamic status bar.
- Retain the internal local client used by offline tests; its banner may identify
  an embedded runtime. Public Chat continues to require AgentServer.

## Touchpoints

Chat client metadata and local/remote adapters, `HeaderBlock`, TUI startup and
CLI composition, their unit/integration/PTY fixtures, and `docs/chat.md`.
Update the changelog through `too aide.too update_changelog`.

## Acceptance

1. Host and Docker headers show ordered rows and complete runtime versions,
   with no frame caption, endpoint text/link, dangling separator, agent-home
   paths, or container IDs. `too -V` and `too AGENT info` retain their output.
2. Runtime workspace inspection filters unavailable entries, preserves names,
   tolerates additive fields, and handles empty/failed/malformed responses.
3. Wide and narrow layouts preserve values, alignment, styles, and padding.
4. Actual API-backed Chat startup and offline PTY exchanges retain their existing
   execution behavior; repository default verification passes.

## Risks and open questions

The extra startup inspection may add one HTTP timeout; failure only changes its
display to `unavailable`. Workspace availability can change after startup; the
banner is a snapshot, while the status bar remains live. Long names and image
selectors fold using the existing narrow layout. Open questions: none.

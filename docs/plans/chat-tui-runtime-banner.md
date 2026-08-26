# Chat TUI Runtime Banner

Status: Approved on 2026-08-25; amended and approved on 2026-08-26.

## Goal and success criteria

Make the Chat TUI banner distinguish the Chat process, its executor, and its
sandbox without deriving runtime identity in the renderer. This definition
supersedes only the banner metadata presentation in
`chat-tui-banner-metadata.md` and `remote-chat-tui.md`; their execution behavior
and the existing logo, panel, path abbreviation, and responsive layout remain
unchanged.

The change succeeds when:

- metadata is always ordered as `Toolang`, `executor`, `sandbox`, then `home`;
- `Toolang` shows `v<exact-chat-process-version>`;
- an embedded executor shows only `executor  embedded`;
- a remote executor shows its normalized HTTP origin as a terminal hyperlink,
  followed by a dim ` · ` separator and its source version unless that version
  is confirmed equal to the TUI source version;
- equal versions are confirmed only when both are known and clean, so matching
  dirty versions and matching `unknown` values remain visible;
- Docker displays the complete selector and conventional twelve-character
  container ID, separated by a dim ` · `;
- host execution displays `host` followed by a compact operating-system name,
  version, and architecture, separated from `host` by a dim ` · `;
- the host sandbox plugin owns and caches host-system detection and supplies a
  ready display value; the banner does not inspect the OS or sandbox identity;
- the panel keeps exactly one empty content row above and below the logo and
  metadata in every case; and
- wide and narrow layouts fit or fold complete values without clipping.

## Presentation

The detail grid has the following forms. The remote endpoint's visible text and
hyperlink target are the same normalized origin.

```text
Toolang   v0.2.7-87-g69439a4e*
executor  http://localhost:7001 · v0.2.7-88-gc73484a9
sandbox   docker:python:3.13-slim · 5741cca76066
home      ~/.toolang/agents/eve
```

```text
Toolang   v0.3.0
executor  http://localhost:7001
sandbox   host · macOS 27.0 arm64
home      ~/.toolang/agents/eve
```

```text
Toolang   v0.2.7-87-g69439a4e*
executor  embedded
sandbox   host · macOS 27.0 arm64
home      ~/.toolang/agents/eve
```

`Toolang` remains bold bright cyan. Its complete version and all metadata keys
remain dim; executor, sandbox, and home values use the normal foreground. The
remote endpoint additionally carries a Rich/OSC-8 link style. The normalized
origin contains only the scheme, host, and explicit port already accepted by
`RemoteRunClient`; it never includes credentials, path, query, or fragment. The
executor source version is presentation-only metadata: suppressing a confirmed
clean match does not remove it from the runtime profile.

The wide layout keeps the three-line logo and the metadata grid side by side,
top-aligned, with `home` as the only metadata row below the logo. The narrow
layout keeps the existing logo, blank separator row, then the complete metadata
grid. Panel padding remains `(1, 2)`, and the existing blank separation before
the panel and after it remains unchanged.

## Runtime identity and client boundary

`ChatClient` exposes structured banner metadata with a sandbox selector and
ready detail:

- embedded: executor kind `embedded` and the local host sandbox label;
- remote host: normalized endpoint, the conditionally displayed remote source
  version, and the host label supplied by the remote runtime;
- remote Docker: normalized endpoint, the conditionally displayed remote source
  version, complete sandbox selector, and a twelve-character container ID.

The host sandbox plugin derives its description with Python's standard
`platform` APIs. macOS uses its product version, Linux uses the freedesktop OS
release values, and Windows uses its release. All platforms append the machine
architecture and omit OS build identifiers. Detection is cached once per process.
`HostSandbox.prepare()` supplies this description to AgentServer through a
control environment value. Docker explicitly removes that host-only value.

The API runtime profile remains the source of remote process truth. Its sandbox
identity contains the complete selector and two mutually exclusive details:
Docker supplies the complete `instance`, while non-Docker sandboxes normally
supply a ready `description`. `RemoteChatSession` validates that the selector
matches the selected resident runtime status, validates the applicable detail,
shortens a Docker container ID to twelve characters, and exposes the ready
detail. `description` is optional presentation metadata: a missing or `null`
host description is obtained from the local host sandbox plugin, while Docker
continues to use `instance`. Unknown additive profile fields are ignored.
Explicitly malformed, mismatched, or contradictory execution identity still
fails selection rather than displaying a guessed value.

`HeaderBlock` owns labels, ordering, styles, hyperlink rendering, and responsive
width calculation. It joins the supplied sandbox selector and detail with the
dim separator and does not parse, shorten, or calculate sandbox or
operating-system identity.

## Scope and implementation touchpoints

- `src/toolang/plugin/sandboxes/host.py` and `docker.py`: produce the cached host
  description and isolate its runtime control value from Docker.
- `src/toolang/up/process.py` and `server.py`: persist the prepared sandbox
  description in runtime process state.
- `src/toolang/api/schemas.py` and `routers/agent.py`: expose and validate the
  mutually exclusive sandbox instance and description fields.
- `src/toolang/cli/toolang/commands/chat/base.py`: expose structured banner
  metadata with required sandbox selector and detail fields.
- `src/toolang/cli/toolang/commands/chat/local.py` and `remote.py`: supply and
  validate the exact executor and sandbox identities.
- `src/toolang/cli/toolang/commands/chat/blocks.py` and `tui.py`: render the
  ordered grid, hyperlink, responsive sizing, and fixed vertical padding.
- Focused sandbox, API, remote Chat, local Chat, command, TUI, and PTY tests:
  cover identity production, transport, validation, order, styles, hyperlink,
  wide/narrow folding, and panel padding.
- `docs/api.md` and `docs/chat.md`: document the runtime profile and banner.

No executor selection, sandbox launch semantics, status bar, transcript,
command, run, control, recovery, or queue behavior changes.

## Acceptance tests

1. Embedded and remote host Chat always render the host sandbox row using the
   plugin-supplied label; the banner itself performs no system detection.
2. Remote execution omits a matching known clean executor version and retains
   differing, dirty, or unknown versions.
3. Remote Docker Chat renders the complete selector and twelve-character
   instance with a dim middle-dot separator.
4. Docker execution requires an instance; sandbox descriptions remain optional
   presentation metadata. Missing or `null` host descriptions use the host
   plugin fallback, and contradictory or malformed identity is rejected.
5. Host detection covers macOS, Linux, and Windows name/version/architecture
   without displaying OS build identifiers or starting an external command.
6. Wide and narrow renders preserve aligned keys and values, complete folding,
   existing logo styling, and one internal blank row above and below.
7. Focused tests and the default repository verification pass.

## Risks and open questions

The profile's `sandbox.description` field is optional. TUI and executor releases
must independently tolerate absent optional fields and ignore unknown additive
fields; a breaking protocol change requires an explicit versioned contract.
Long host descriptions and sandbox selectors can widen the banner; existing
narrow folding remains the mitigation.

Open questions: none.

# Chat TUI Runtime Banner

Status: Approved on 2026-08-25; amended and approved on 2026-08-26.

## Goal and success criteria

Make the Chat TUI banner distinguish the Chat process, its executor, and an
optional non-host sandbox without compressing those identities into one value.
This definition supersedes only the banner metadata presentation in
`chat-tui-banner-metadata.md` and `remote-chat-tui.md`; their execution behavior
and the existing logo, panel, path abbreviation, and responsive layout remain
unchanged.

The change succeeds when:

- metadata is ordered as `Toolang`, `executor`, optional `sandbox`, then `home`;
- `Toolang` shows `v<exact-chat-process-version>`;
- an embedded executor shows only `executor  embedded`;
- a remote executor shows its normalized HTTP origin as a terminal hyperlink,
  followed by one space and its source version unless that version is confirmed
  equal to the TUI source version;
- equal versions are confirmed only when both are known and clean, so matching
  dirty versions and matching `unknown` values remain visible;
- a remote host executor has no `sandbox` row;
- a remote non-host executor adds one `sandbox` row containing the complete
  sandbox selector and the conventional twelve-character short runtime ID,
  separated by one space;
- the panel keeps exactly one empty content row above and below the logo and
  metadata in every case, so the sandboxed form is exactly one row taller than
  either three-row form; and
- wide and narrow layouts fit or fold complete values without clipping.

## Presentation

The detail grid has the following three forms. The remote endpoint's visible
text and hyperlink target are the same normalized origin.

```text
Toolang   v0.2.7-87-g69439a4e*
executor  http://localhost:7001 v0.2.7-88-gc73484a9
sandbox   docker:pyslim-3.11 2f0f8934abcd
home      ~/.toolang/agents/eve
```

```text
Toolang   v0.3.0
executor  http://localhost:7001
home      ~/.toolang/agents/eve
```

```text
Toolang   v0.2.7-87-g69439a4e*
executor  embedded
home      ~/.toolang/agents/eve
```

`Toolang` remains bold bright cyan. Its complete version and all metadata keys
remain dim; executor, sandbox, and home values use the normal foreground. The
remote endpoint additionally carries a Rich/OSC-8 link style. The normalized
origin contains only the scheme, host, and explicit port already accepted by
`RemoteRunClient`; it never includes credentials, path, query, or fragment.
The executor source version is presentation-only metadata: suppressing a
confirmed clean match does not remove it from the runtime profile.

The wide layout keeps the three-line logo and the metadata grid side by side,
top-aligned. In the sandboxed form, the `home` row is the only metadata row
below the logo. The narrow layout keeps the existing logo, blank separator row,
then the complete metadata grid. Panel padding remains `(1, 2)`, and the
existing blank separation before the panel and after it remains unchanged. No
conditional blank row accompanies the optional sandbox row.

## Runtime identity and client boundary

Replace the opaque `ChatClient.executor_label` presentation property with
structured banner metadata owned by the client:

- embedded: executor kind `embedded`, with no endpoint, remote version, or
  sandbox identity;
- remote host: normalized endpoint plus the remote source version when it is
  not a confirmed clean match, with no sandbox display identity;
- remote non-host: normalized endpoint, conditionally displayed remote source
  version, complete sandbox selector, and a twelve-character runtime instance
  ID. For Docker, this matches the short container ID shown by the Docker CLI.

The API runtime profile remains the source of remote process truth. Extend its
sandbox identity with the complete selector and change its projected non-host
instance from six to twelve characters. `RemoteChatSession` validates that the
profile selector matches the selected resident runtime status before exposing
the structured metadata. Missing, malformed, mismatched, or too-short remote
identity continues to fail selection rather than displaying a guessed value.
The `host` selector is validation data but is not rendered as a sandbox row.

`HeaderBlock` owns labels, ordering, styles, hyperlink rendering, responsive
width calculation, and optional-row composition. It does not parse a combined
executor string.

## Scope and implementation touchpoints

- `src/toolang/api/schemas.py` and `src/toolang/api/routers/agent.py`: expose the
  complete runtime sandbox selector and twelve-character non-host instance.
- `src/toolang/cli/toolang/commands/chat/base.py`: replace the opaque executor
  label with structured banner metadata.
- `src/toolang/cli/toolang/commands/chat/local.py` and `remote.py`: provide the
  three exact identity cases and validate remote profile truth.
- `src/toolang/cli/toolang/commands/chat/blocks.py` and `tui.py`: render the
  ordered optional-row grid, hyperlink, responsive sizing, and fixed vertical
  padding.
- Focused API, remote Chat, command, TUI, and PTY tests: cover profile identity,
  all three forms, exact order and styles, hyperlink target, wide/narrow
  folding, and panel height/padding.
- `docs/api.md` and `docs/chat.md`: update the runtime profile and banner
  examples.

No executor selection, sandbox launch, status bar, transcript, command, run,
control, recovery, or queue behavior changes.

## Acceptance tests

1. Embedded Chat renders the exact three metadata rows in the requested order
   with no sandbox row.
2. Remote host Chat renders the normalized linked endpoint with no sandbox row;
   it omits a matching known clean version and retains differing, dirty, or
   unknown versions.
3. Remote non-host Chat renders the same executor row plus exactly one sandbox
   row containing the complete selector and twelve-character instance, using
   one space between adjacent values and no dot separators.
4. Profile validation rejects selector mismatches, host instances, missing or
   malformed fields, and non-host instance IDs shorter than twelve characters.
5. Wide and narrow renders preserve aligned keys and values, complete folding,
   and existing logo styling without clipping.
6. Both three-row cases retain one internal blank row above and below; the
   four-row case adds exactly one rendered line and retains the same internal
   and external vertical spacing.
7. Focused tests and the default repository verification pass.

## Risks and open questions

The main compatibility risk is changing the public profile's short instance
projection from six to twelve characters. It is an additive identity-strength
change for current consumers, but strict consumers must update their expected
length. Long sandbox selectors can widen the banner; existing narrow folding
remains the mitigation.

Open questions: none.

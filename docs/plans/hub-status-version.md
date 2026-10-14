# Hub Status Version

Status: Approved for implementation on 2026-10-12 with inline version output.

## Goal and success criteria

Show the running Hub process's Toolang source version in `too hub status`,
including when the invoking CLI has a different version.

Example (illustrative version):

```text
Hub running: http://127.0.0.1:7000, v0.4.0a5-3-g12345678*
```

## Verified behavior

`cli/toolang/commands/hub.py` currently prints only state and endpoint.
`HubProcess.ready()` checks `/healthz`, whose response is exactly `{"ok": true}`.
The Hub already exposes `/info` with a validated `HubInfo.version`, captured
from `toolang_version()` when `up/hub.py` constructs the server application.
Talk already consumes this metadata. No server protocol change is required.

## Scope and decisions

- Keep the state classification, commands, flags, and exit behavior. Append
  `, <version>` after the endpoint on the existing status line when a live Hub
  record exists. A stopped Hub continues to print only `Hub stopped`.
- Query `/info` on the endpoint from the process-validated current record,
  with the recorded backend and human identity headers. Never substitute the
  invoking CLI's version, distribution metadata, or current checkout version.
- Keep the synchronous metadata lookup with process inspection in
  `up/hub.py`; validate through the existing `HubInfo` schema. Use a one-second
  HTTP timeout and disable environment proxy settings, as readiness does.
- Read version independently of backend health, allowing an unavailable Hub
  whose HTTP server still responds to report its version. Skip the metadata
  request while the record is `starting` and display `unknown`.
- Missing `/info` on older Hubs, HTTP errors, identity rejection, malformed
  metadata, and timeouts yield `, unknown` without failing status or
  changing its readiness classification.
- Format the supplied value through the existing CLI `version_label()` helper:
  exactly one leading `v` for known versions, unchanged revision and dirty
  suffix, and `unknown` without a prefix.

Persisting version in the Hub record, displaying CLI/Hub comparisons, changing
`hub start` output, and changing `/healthz` or `/info` are out of scope.

## Implementation touchpoints

- `src/toolang/up/hub.py`: bounded, best-effort process metadata lookup.
- `src/toolang/cli/toolang/commands/hub.py`: inline version and status help text.
- `tests/unit/up/test_hub_process.py`: deterministic HTTP metadata coverage.
- `tests/unit/cli/test_hub_commands.py`: status output acceptance tests.
- `CHANGELOG.md`: generate an Unreleased entry through
  `too aid.too update_changelog` during implementation; maintainer verification
  remains required before merge.

## Acceptance tests

1. A healthy Hub with a version different from the CLI prints its endpoint and
   the server version, including revision and `*` suffix.
2. Version values with and without a leading `v` render exactly one prefix;
   the server's `unknown` renders unchanged.
3. Backend readiness failure with valid `/info` prints `Hub unavailable` and
   the server version.
4. Missing, rejected, invalid, or timed-out metadata prints `unknown` while the
   existing status and exit code remain intact. Requests use the record's
   endpoint and identity headers with the configured timeout and no proxies.
5. A starting Hub prints its existing state and `, unknown` without
   making a metadata request; a stopped or stale process prints only
   `Hub stopped` without HTTP requests.
6. Tests remain offline and deterministic. Default Ruff, formatting, type,
   and pytest verification pass before implementation commits.

## Risks and open questions

The additional request can add up to one second on failure. Process shutdown or
replacement between discovery and requests can make metadata unknown; identity
headers retain the existing protection against a mismatched backend or human.
No additional persistence or source-version resolution is needed.

Open questions: none.

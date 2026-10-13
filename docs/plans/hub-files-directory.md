# Hub Runtime File Directory

Status: Approved for implementation on 2026-10-13.

## Goal

Store the root Hub's generated files in `<root>/hub/` instead of `<root>/.runtime/`.

## Decisions

- Place the Hub record, process lock, and log at `<root>/hub/hub.json`,
  `<root>/hub/hub.lock`, and `<root>/hub/hub.log`, respectively.
- Create `hub/` when Hub startup needs it. Keep the directory Hub-specific; do
  not change the shared runtime layout or other `.runtime` data.
- Update discovery to read only `<root>/hub/hub.json`. Do not migrate, read, or
  write legacy Hub files in `<root>/.runtime/`; keep migration guidance limited
  to the required changelog entry, with no separate guide.
- Preserve Hub lifecycle, locking, record format, file privacy, and CLI behavior.
- This plan supersedes the Hub artifact location specified in
  [teaming](teaming.md) only; all other teaming decisions remain unchanged.

## Scope

Implementation updates the Hub path construction and discovery, adds regression
coverage, and aligns Hub path references in the teaming and agent Hub transport
plans, layout and messaging documentation, and `CHANGELOG.md`.

Likely files:

- `src/toolang/up/hub.py`
- `src/toolang/teaming/discovery.py`
- `tests/unit/up/test_hub_process.py`
- `tests/unit/messaging/test_agent_api.py`
- `tests/unit/cli/test_hub_commands.py`
- `docs/plans/teaming.md`
- `docs/plans/agent-hub-transport.md`
- `docs/layout.md`
- `docs/messaging.md`
- `CHANGELOG.md`

## Acceptance

- Hub start/serve places `hub.log`, `hub.json`, and `hub.lock` under `<root>/hub/`
  and does not create those files under `<root>/.runtime/`.
- Process control and client discovery use the new record path; an old record
  under `.runtime` is not treated as current Hub state.
- Concurrent Hub startup remains excluded by the lock under `hub/`.
- Existing Hub record permissions, readiness/status behavior, startup cleanup,
  and CLI results remain unchanged.
- Focused Hub process, discovery, and command tests pass; the implementation
  change also passes the repository's default verification suite.

## Risks and open questions

Moving the lock means an older process using `.runtime/hub.lock` does not
coordinate with a newer process using `hub/hub.lock`. Automatic compatibility
and migration are intentionally out of scope for this alpha change.

No open design questions remain. Implementation requires explicit approval of
this plan.

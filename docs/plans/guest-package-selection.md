# Guest Package Selection

Status: Revised behavior approved in the 2026-10-25 discussion.

## Goal and Scope

A new guest uses the same complete `too --version` identity as its controller.
Users build development wheels themselves. This supersedes the earlier automatic
build/cache proposal; no automatic builds or package lookup fallback are added.
Host execution, sandbox selection, and model behavior retain their contracts.

## Decisions

- Without `--dev`, install `toolang==<distribution version>`, preserving alpha,
  beta, rc, and dev versions. Before starting the guest workload, compare its
  `too --version` output with the controller's complete identity, including Git
  revision and dirty marker. Distribution versions choose downloads; they do not
  prove source identity. Unknown controller identities fail before index launch.
- An explicit `--dev [PATH]` selects an existing Toolang wheel. Read distribution
  metadata and embedded `_build_info.json` without importing wheel code. Compare
  its displayed source identity with the controller before guest preparation.
  Missing/unusable source provenance is unknown and cannot establish a match.
- A trailing `!` on the supplied wheel or directory path confirms a different or
  unknown source identity. Strip one marker before filesystem resolution. It
  bypasses only the controller/wheel identity comparison, never wheel validation
  or existing-server path/content binding checks. A bare `!` selects the working
  directory with confirmation.
- Directory selection retains the latest modification-time rule and absolute
  path tie break. Validate the selected wheel; do not silently choose an older
  matching wheel. `--dev` still means the invocation directory, not agent home.
- Pass a concrete requirement/wheel and expected CLI identity to the sandbox.
  Docker checks the installed CLI against the selected identity; an explicitly
  confirmed wheel with unknown provenance skips only that identity check.
- The old source/index warning is removed. Missing index versions, malformed
  wheels, and identity mismatches fail explicitly; no alternate version is tried.

## Touchpoints

- `common/version.py`: exact distribution requirements and wheel source identity.
- `up/sandbox.py` and sandbox request types: path confirmation and version checks.
- Docker preparation and `guest.sh`: install selection and runtime identity check.
- `docs/design/hosting.md` and focused version, sandbox, bootstrap, and CLI tests.

## Acceptance Tests

1. Exact distribution requirements preserve stable, alpha, beta, rc, and dev
   versions; the installed guest identity must also match the controller.
2. Same distribution number with a different revision/dirty marker fails wheel
   selection; matching complete identities pass regardless of the filename.
3. File and directory `!` confirmation accepts a different/unknown identity,
   while malformed or non-Toolang wheels still fail. Existing-server content
   checks remain effective after confirmation.
4. Recursive selection, timestamp ties, bare paths, spaces, and missing paths
   preserve their behavior; a newer mismatching wheel does not select an older
   match. Host execution rejects `--dev` before inspecting files.
5. Guest version mismatch stops before the workload and retains expected/actual
   identities in diagnostics. A confirmed wheel uses its own known identity.
6. Source execution never builds a wheel or creates a package cache. Manual wheel
   selection works in a Rust guest without requiring a model-provider request.

## Risks and Verification

A displayed source identity is not a content hash of dirty files. Equality follows
existing CLI semantics. Confirming another version can expose incompatible runtime
arguments; confirmation does not promise compatibility. Missing provenance needs
explicit confirmation. Build tooling is needed only for a user-requested manual
build. The user excluded a local full-suite run; run lint, formatting, type checks,
and focused offline regressions. No open design questions remain.

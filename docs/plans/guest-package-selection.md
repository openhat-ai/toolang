# Guest Package Selection

Status: Approved for implementation in the 2026-10-25 discussion.

## Goal and Scope

A new guest uses the controlling Toolang distribution version or its current
development source without requiring a manual wheel build. This supersedes the
unversioned default and source-warning decisions in the development-wheel plans.
Explicit `--dev [PATH]` wheel selection, host execution, attachment, sandbox
selection, and model behavior retain their existing contracts.

## Decisions

- An explicit development wheel takes precedence over installation detection.
- Detect editable execution from `direct_url.json` and its local source URL.
  Without distribution metadata, validate the project containing the imported
  Toolang module. Neither the working directory nor a prerelease suffix implies
  development execution.
- For an installed distribution, use `toolang==<distribution version>`, including
  alpha, beta, release-candidate, and development versions. Local non-editable
  and VCS installs are installed artifacts, not live source. Their exact version
  must be available from the configured index; otherwise installation fails and
  the caller may supply `--dev PATH`. Never fall back to another version.
- For development execution, build a wheel from the validated source project.
  Cache wheels under the controller root's `.cache/packages`, keyed by source
  contents, build configuration, and source provenance. Include added package
  files and exclude bytecode. Serialize builds and publish only successful
  wheels; source changes during a build fail with a retry instruction.
- Use uv for builds when available, otherwise the current interpreter's pip.
  Missing build tools, invalid source metadata, and build errors fail before
  guest launch. Report build progress instead of the old package-index warning.
- Resolve the package after loading the sandbox plugin and checking its location.
  Host launches and attachment do not inspect or build a guest package. Pass
  the concrete requirement or wheel into the sandbox request; Docker does not
  independently choose a package version.

## Touchpoints

- `common/version.py`: distribution version and development provenance.
- `up/packages.py`, `up/sandbox.py`, and sandbox request types: cached builds and
  concrete guest-package selection.
- Docker preparation: use the resolved requirement or stage the selected wheel.
- CLI startup: remove the obsolete source/index warning.
- `docs/design/hosting.md` and focused package, sandbox, and CLI tests.

## Acceptance Tests

1. Stable, alpha, beta, rc, and dev distributions produce exact requirements;
   Docker receives them unchanged rather than an unversioned package name.
2. Editable and direct-source execution select the correct source project from
   any working directory; ordinary installed artifacts do not become editable.
3. Unchanged inputs reuse one wheel; changed, added, and deleted source files or
   build configuration invalidate it. Concurrent builds share a completed wheel.
4. Failed or interrupted builds and invalid output do not publish cache entries;
   changed inputs during a build require retry rather than caching stale output.
5. Explicit wheels bypass detection/builds. Host launches, existing-server
   attachment, and unknown sandbox plugins do not trigger an automatic build.
6. Package installation failures retain their actual diagnostic and do not retry
   an unpinned requirement. A Rust guest can parse the current `_serve` arguments.

## Risks and Verification

The first source launch requires build tools and may need network access for
build dependencies. Source builds snapshot the launch; they do not provide hot
reload or synchronize the host's dependency environment. A package version alone
does not prove that a custom artifact matches an indexed artifact.

Run lint, formatting, type checks, and focused offline regression tests. The user
explicitly excluded a local full-suite run. Live-provider tests are excluded.
No open design questions remain.

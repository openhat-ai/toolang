# Docker Guest Bootstrap

Status: Implementation requested for review on 2026-10-24.

## Goal And Scope

Make Docker guest installation self-contained in `guest.sh`, delete
`guest.py`, and execute the installed Toolang CLI directly. Keep the
bootstrap flow explicit, share installation/validation steps, and preserve
Docker lifecycle, diagnostics, startup events, and workload environment behavior.
This supersedes the installer and helper design in
[Docker Guest Harness](docker-guest-harness.md#guest-core).

## Decisions

- Probe `python3`, then `python`; select an interpreter only when it is Python
  3.11+ and its own `-m pip --version` succeeds. Install the wheel or `toolang`
  directly into that environment with pip, even when uv is present.
- Otherwise reuse a capable standalone uv or install pinned uv 0.12.6 using
  curl, then wget. Use uv to install/find managed Python 3.13 and install Toolang
  in the private runtime. Remove pip-installed uv and uv module mode.
- Both installers receive the package source as one literal argument and replace
  existing packages. Installation failure is terminal; do not change installers.
- Expose the installed CLI under the private runtime's `bin/too`; the pip path
  links the selected interpreter's scripts directory entry point there. Validate
  that executable with `_serve --help` before executing the workload.
- Bootstrap runs in a subshell. Its variables and installer overrides cannot
  overwrite arbitrary image environment values. The parent shell retains launch
  arguments in positional parameters, redirects the workload log, prepends the
  private bin directory, sets container identity, and replaces itself with the
  real command using `exec`.
- Mount `guest.env` at the existing root agent policy path. Only Toolang's common
  environment loader reads it; the shell neither parses nor sources it.
- Allow 180 seconds for guest bootstrap and API readiness, while keeping the
  host startup budget at 30 seconds. Readiness remains independent of progress.
- Keep concise progress, private diagnostics and workload logs, and closed event
  tokens. Permit `python.use` as the pip path's first startup event.

## Touchpoints

Guest shell, helper deletion, host staging and command construction, standalone
harness, startup readiness budgets, focused sandbox/harness tests, opt-in Docker
image and lifecycle checks, and current hosting documentation.

## Acceptance

1. Python 3.11 with pip selects pip; Python 3.10, missing pip, and missing Python
   select uv. Available uv does not override qualifying pip.
2. Package names and wheels with spaces install through the selected installer;
   curl and wget use the pinned official uv installer when needed.
3. Image environment values survive both paths, including names used internally
   by bootstrap; installer overrides stay private. Existing mounted environment
   tests continue to use Toolang's common loader.
4. The installed `too`/`toolang` and custom workloads retain arguments, PID, exit
   status, private log permissions, ordered events, and diagnostic cleanup.
   An unrelated image CLI cannot shadow the installed executable.
5. Installation failure retains private diagnostics and never retries via another
   installer. Unsupported images and invalid container identity fail clearly.
6. A guest becoming ready after 45 seconds succeeds; an over-budget guest is
   cleaned up. Host startup still times out after 30 seconds.
7. Live Docker checks cover pip, uv-only, and curl-only images. Default offline
   Ruff, formatting, ty, and pytest checks pass.

## Compatibility

Direct pip installation requires a writable interpreter environment and can
replace image packages. Externally managed or read-only environments fail without
policy overrides. Installers use image package-index/proxy configuration;
mounted workload grants are applied later by Toolang. No open decisions remain.

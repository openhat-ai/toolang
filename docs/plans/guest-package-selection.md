# Guest Package Selection

Status: Approved in discussion; simplified to one identity check per package path.

## Scope and Behavior

- Docker selects guest packages during preparation. Host execution and existing
  server attachment keep their behavior. No automatic wheel builds or cache.
- Without `--dev`, install `toolang==<controller distribution version>`, preserving
  alpha, beta, rc, and dev releases. Check the installed CLI's complete
  `too --version` identity once in the guest. Unknown controller identity fails.
- `--dev [PATH]` retains existing recursive newest-wheel selection and path tie
  break. Strip one trailing `!` before resolving the path; bare `!` means cwd.
- Copy the selected wheel into launch materials, then read its distribution and
  embedded source metadata without executing it. Compare with the controller's
  complete identity, including revision and dirty marker. Mismatch or unknown
  identity fails unless the supplied path ends with `!`. Invalid wheels still fail.
  Local wheels undergo no later identity comparison.
- Confirmation does not bypass existing-server wheel path/content bindings or
  promise runtime compatibility. Source users build and select wheels manually.

## Implementation and Acceptance

Use existing `common/version.py` readers, Docker preparation, and guest bootstrap.
The generic launch/request types carry only the wheel path and confirmation flag.
Update `docs/design/hosting.md` and focused tests for exact prerelease pins,
complete identity matching, file/directory/cwd confirmation, invalid wheels,
staged copies, guest index rejection, and existing-server bindings.

Equality follows CLI identity semantics; dirty identities are not content hashes.
Run lint, formatting, types, and affected offline tests. The user excluded a local
full-suite run. No open questions remain.

# Guest Package Selection

Status: Approved scope reduced to the two package-selection changes below.

- Without `--dev`, Docker passes `toolang==<controller distribution version>` to
  the existing guest installer, preserving prereleases.
- With `--dev`, retain existing wheel resolution and check the selected wheel's
  embedded source identity against the current `too --version`, including revision
  and dirty marker. A trailing `!` on the supplied file/directory path confirms a
  different or unknown identity. Strip the marker before resolution. This is one
  argument check; subsequent preparation, installation, and startup stay unchanged.

Reuse `common/version.py` for wheel build-info parsing; change only the Docker
package requirement and `up/sandbox.py` argument validation. No automatic builds,
cache, runtime checks, or protocol fields. CLI identity is not a dirty-file hash.

Verify exact stable/prerelease pins, matching/mismatching/unknown wheel identities,
file/directory confirmation, and existing path/content bindings with focused offline
tests and static checks. No local full suite, as requested. No open questions.

# Shared host and guest launch environment

Status: approved in discussion on 2026-10-23; implemented.

## Goal and scope

Use the same dotenv selection and parser for host and Docker agent launches.
Retain arbitrary image/entrypoint variables and isolate Toolang's installer
environment. This implements the environment portion of the Docker mount
definition in PR #943; Hub routing, registry layout, and other mounts are outside
this change.

## Contract

- Managed launches originate on the host. Resolve root inputs and agent policy
  using the existing `keep`/`pick` rules. A selected agent file, including an
  empty file, replaces lower-priority inputs and disables `pick`.
- For residents, `ROOT/.<name>.env` precedes `HOME/.env`. Preserve existing
  source-based root policy filenames for roaming and visiting agents. Compute
  host/guest paths at orchestration and carry source identity into `_serve`.
  Encoded filenames over 255 bytes use `.source-<sha256>.env` on both sides.
- Host processes receive the reduced environment before plugin construction.
  Managed host children consume that exec snapshot without rereading dotenv;
  standalone internal serving resolves the ordinary host policy.
  Docker receives only resolved guest grants and mapped workload bindings;
  host-only `keep` values are not staged automatically.
- Always generate mode-0600 `guest.env`, including an empty result, and mount it
  read-only at the guest's highest-priority root policy path. Keep the full home
  mount without masking or creating `HOME/.env`. Guest launch uses the ordinary
  source selector/parser before plugin construction; no bootstrap dotenv parser.
- Preserve native image/entrypoint environment as the guest baseline, overlay
  loaded bindings, then restore the finite launcher-owned controls. Do not
  reserve whole variable prefixes or introduce language-specific allowlists.
- Bootstrap runs installation with private `UV_CACHE_DIR`,
  `UV_PYTHON_INSTALL_DIR`, `UV_PYTHON_BIN_DIR`, `UV_TOOL_DIR`, `UV_TOOL_BIN_DIR`,
  and `UV_NO_PROGRESS`. These overrides and temporary `PYTHONPATH` or
  `UV_UNMANAGED_INSTALL` settings never replace workload values. Bootstrap uses
  the image's environment for installation; host grants load with the agent.
- Resolve internal executables by absolute paths and prepend the private
  Toolang bin to the selected guest PATH without adding an empty component.
  Preserve host PATH exactly. Shell tools use `/bin/sh -c`.
- Model calls, setup refreshes, tools, and children share the captured final
  process environment. Authored file edits take effect after restart.

## Acceptance checks

| Scenario | Pass condition |
| --- | --- |
| Host boundary | Existing root/agent priority and `keep`/`pick` tests remain valid; plugins and children see the reduced snapshot. |
| Host exec handoff | Editing, emptying, or corrupting dotenv after controller selection does not change the child snapshot or prevent its startup. |
| Guest boundary | Picked, explicit, empty, and absent credentials produce a mounted root policy file without changing authored host dotenv files. |
| Common loading | Resident, roaming, and visiting guest launches select generated root data over local fallbacks, with no interpolation or duplicate-name serialization. |
| Native baseline | Custom image variables, Rust toolchain homes, and configured UV variables survive; installation-only values are absent when not supplied by the image or user. |
| Controls and snapshot | Authored controls cannot replace launcher facts, and setup refreshes or children do not recover lower-priority dotenv values. |
| Execution | A PATH-only executable runs through the shell tool; a fresh Rust 1.75 guest uses Cargo/rustc 1.75 without installation. |

Default tests remain offline. Opt-in Docker checks use isolated files, images,
and stub model providers; they do not restart user agents. Run the repository's
default lint, format, type, and test checks before committing.

## Touchpoints and risks

Touchpoints: common environment selection, sandbox request/orchestration,
internal CLI serving, Docker staging/bootstrap, shell tool execution, hosting
documentation, and focused unit/integration tests.

Image and entrypoint variables are trusted as part of selecting that image.
Authored home dotenv files remain readable through the home mount. Installer
network settings must come from the image environment. Existing native Linux
single-file bind replacement semantics remain unchanged; generated files are
immutable startup inputs, not live configuration.

# Align Docker container names with agent instances

Status: proposed; awaiting human approval. Definition only.

## Goal and scope

Make each newly created Docker container visibly and stably correspond to the
instance address shown by `too top` and accepted by agent commands. Success means
the same home keeps its container name across launches, distinct homes/Toolang
roots can coexist, and stale control records cannot affect another launch.

Current Docker names are `toolang-<agent_name>-<launch_id>`; they omit the place
code. Instance identity follows [agent addresses](../design/agent-addresses.md),
where addresses are root-scoped and `launch_id` is a separate generation fence.

Change Docker naming and the ownership checks required for stable names. Preserve
agent addresses, storage paths, CLI syntax, host sandbox behavior, bootstrap,
logging, and placement lifetimes. No configurable naming template or in-place
rename of existing containers is included.

## Decisions

- Use `toolang-<root_code>-<agent_name>[-<place_code>]`. Preserve the full agent
  name and place code, and use the existing seven-character `root_code` derived
  from the canonical Toolang root. Resident names omit the place code. Do not add
  a launch suffix or silently choose another name on conflict.

  | Placement | Instance address | Container name |
  | --- | --- | --- |
  | Resident | `alice` | `toolang-<root_code>-alice` |
  | Visiting | `lex@<t_code>` | `toolang-<root_code>-lex-<t_code>` |
  | Roaming | `aid@<s_code>` | `toolang-<root_code>-aid-<s_code>` |

  Angle-bracket terms denote complete codes, not literal characters. Docker's
  [container-name validation](https://github.com/moby/moby/blob/master/daemon/names/names.go)
  excludes `@`; agent names contain no hyphens, so the separator preserves an
  unambiguous mapping. Root codes prevent ordinary cross-root name conflicts;
  a short-code collision must fail without touching the existing container.
- Add optional `agent_address` and canonical `toolang_root` inputs to
  `SandboxRequest`, defaulting to `None` for existing plugin callers. The hosting
  call site always supplies both. Docker requires them and rejects missing or
  inconsistent identity before staging; it must not infer identity from guest
  paths, cwd, or user environment. Keep the existing generation input and unique
  launch-specific staging/diagnostic paths.
- Derive the name through one Docker-owned helper shared by `intent_reference`
  and preparation. `SandboxRef.runtime_name` always carries that name. Successful
  launch references continue to carry Docker's immutable container ID in
  `runtime_id`; pre-creation intent references use the stable name.
- Create containers with labels `io.toolang.root`, `io.toolang.address`, and
  `io.toolang.launch-id`, containing the canonical Toolang root path, original
  address, and full launch ID. Persist these expectations in
  `SandboxRef.meta.docker_identity` with `version: 1`, `root`, `address`, and
  `launch_id` before creation, including failure/recovery references. Malformed
  or unsupported identity metadata fails validation.
- Before using a new-format reference, inspect ownership labels. Resolve a
  matching name to an immutable container ID and use that ID for subsequent
  inspection, log access, waiting, stopping, and removal. Never mutate by stable
  name after resolving it. Apply the same ownership check to batched liveness;
  a replacement container must not make a stale reference appear alive.
- A missing container is absent only after the existing pending-creation check.
  Label mismatch or inconclusive inspection produces an ownership/unknown error;
  it never authorizes cleanup. Name conflicts, cancellation, failed startup, and
  recovery may clean up only the matching generation. Preserve pending Docker
  helper detection and existing management locks.
- Leave existing containers and persisted references unchanged. References
  without `docker_identity` retain their existing immutable-ID or
  generation-specific-name behavior. After normal stop/release, the next launch
  uses the new format. No scanning, adoption, or deletion by naming pattern.

## Implementation touchpoints

- `src/toolang/base/types/sandbox.py`: explicit request identity inputs.
- `src/toolang/up/sandbox.py`: pass the bound address and canonical Toolang root.
- `src/toolang/plugin/sandboxes/docker/{sandbox,_cli}.py`: shared naming,
  creation labels, reference metadata, and generation-aware resource access.
- `src/toolang/up/observation.py`: preserve ownership during batched probing;
  keep Docker parsing and identity validation in the Docker adapter.
- `tests/unit/plugin/test_sandboxes.py` and
  `tests/unit/up/{test_sandbox,test_runtime_observation,test_process}.py`:
  acceptance coverage. Update [hosting](../design/hosting.md) with the resulting
  naming and ownership contract.

## Acceptance and verification

- Naming: resident, visiting, and roaming names match the table, satisfy Docker's
  name grammar, and agree across intent, plan, launch reference, and `info`.
  Underscores and maximum-length agent names are preserved. Missing/inconsistent
  request identity fails before creating staging files or containers.
- Isolation and restart: same home/new launch retains the name and changes the
  generation; different places or roots produce different names. A forced root
  code collision fails without modifying the existing container.
- Ownership: stale intent, stale immutable-ID reference, foreign/missing labels,
  and a name replaced between inspection and cleanup cannot stop, delete, read
  logs from, or report liveness for the replacement. Matching ownership works
  through individual and batched probes.
- Recovery: interrupted/cancelled creation, preparation failure, daemon timeout,
  existing-name conflict, automatic removal, and pending creation preserve the
  recoverable reference and clean up only owned resources. A completed prior
  generation can be released before reusing its name.
- Existing references still support inspect/stop/release without renaming their
  containers; a subsequent start uses the stable name. Host sandbox behavior and
  foreground/background output remain unchanged.

Keep acceptance tests offline with mocked Docker calls and deterministic process
states. Run the repository's default verification before implementation commits;
definition changes require link/statement verification and `git diff --check`.

## Risks and open questions

Stable names remove the current name-level generation isolation, so ownership
checks and immutable-ID operations are part of this change, not optional follow-up
work. Unexpected ownership leaves an actionable conflict rather than guessing.
Root-code collisions remain possible and fail safely through full-path labels.
Direct callers constructing Docker requests must supply the new identity inputs.

No unresolved design questions. Human approval of the proposed stable naming
contract is required before implementation.

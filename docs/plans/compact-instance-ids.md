# Compact instance IDs

Status: approved for implementation by the user on 2026-10-21. The user
requested consistent IDs across generation, all CLI displays and selection,
and authorized increasing the proposed width after evaluating collisions.

## Goal and scope

New visiting and roaming IDs use their placement marker (`v` or `r`) plus
16 lowercase hex digits. CLI names, paths, registrations, JSON and `--instance`
use that actual ID. Resident names and private launch-generation IDs do not
change. No display-only aliases or shortened selectors are introduced.

Visiting keeps its existing deterministic root/source hash and paths. Roaming
uses a SHA-256 prefix over the canonical source path and a fresh UUID nonce;
startup order is persisted separately as `request_order_ns`. Default selection
uses that metadata, including failed and exited instances, rather than lexical
ID order, completion time or file mtime.

Existing 32-digit roaming IDs remain readable and selectable at their original
paths and retain their complete names in every interface. Do not rename or
rewrite retained roots. Their encoded startup order remains validated against
their metadata; new IDs require a valid nonnegative integer startup order.

## Collision policy and rationale

For uniformly distributed IDs, the birthday estimate is
`1 - exp(-n * (n - 1) / (2 * 16**digits))` within one identity namespace.

| Hex digits | 10,000 instances | 1,000,000 instances |
| --- | --- | --- |
| 8 | 1.16% | approximately 100% |
| 12 | 0.0000178% | 0.177% |
| 16 | 0.000000000271% | 0.00000271% |

Use 16 digits to preserve visiting paths and keep the generated names compact
without relying on collision probability alone. Under the source launch lock
and registry publication lock, roaming retries candidates that already have a
retained root or a registry reference, up to 128 attempts. Collision exhaustion
must fail without replacing history or publishing staging directories.
Registry publication must reject the same name bound to a different status
path, while allowing launch replacement for the same status path.

## Ownership and implementation

- `common.instance_ids`: pure token generation and validation, including the
  legacy roaming format; no filesystem reads or runtime dependencies.
- `common.layout`: concrete paths and layout validation; derive startup order
  from legacy IDs only, accepting explicit metadata for new IDs.
- `up.identity`: bounded allocation, retained-record validation and selection.
- `up.observation`: registry lock and publication conflict enforcement.
- Existing CLI renderers consume canonical names. Keep selection, display and
  machine-readable identities consistent without an extra formatting layer.

Likely files: the modules above, their unit tests, CLI rendering/routing tests,
`docs/design/layout.md`, `docs/design/cli.md`, `docs/design/top.md` and the
generated changelog. This change is independent of Top statistical scope.

## Acceptance tests

1. Direct codec/layout tests accept both new placements and legacy roaming IDs;
   malformed, cross-placement and unsupported-width IDs fail consistently.
2. Parallel allocations preserve unique roots and monotonic startup metadata;
   forced local/global collisions retry, and exhaustion preserves existing data.
3. Default selection follows metadata across random ID order, clock rollback,
   failed instances and mixed legacy/new roots; explicit legacy selection works.
4. Missing, corrupt or inconsistent retained metadata fails without mutation.
   Publication cannot overwrite another status path or remove its reference.
5. Hub and Top display the same complete new IDs as JSON and CLI selection;
   existing legacy names remain intact. All default offline checks pass.

## Risks and open questions

Older executables cannot parse new roaming IDs. Avoid mixing executable versions
against one running instance; existing long IDs remain supported by the new
runtime. Short IDs carry no ordering information, so missing order metadata is
an error rather than permission to select by filename.

Open questions: none.

# Packed nonresident place codes

Status: implemented; approved on 2026-10-24.

## Goal and scope

Replace literal `s`/`t` prefixes with a two-bit type inside one fixed-width
lowercase Base32 value. Keep 48-bit source hashes and millisecond ordering.
Root codes, placement directories, runtime ownership, and other ID families
retain their contracts.

## Format

| Placement | High-to-low bit layout | Raw width | Encoded width |
| --- | --- | --- | --- |
| Roaming | `00`, 48-bit canonical path hash, 45-bit elapsed milliseconds | 95 bits | 19 chars |
| Visiting | `11`, 48-bit canonical requested-ref hash | 50 bits | 10 chars |

Pack the entire value before encoding with the existing ordered lowercase
Base32 alphabet. Keep leading zeroes. Reject reserved types `01` and `10`,
incorrect widths/types, and noncanonical spelling. Roaming starts with `0–7`;
visiting starts with `rstvwxyz`. Addresses remain `NAME@CODE` or
`SOURCE.too@CODE`, without an additional marker.

Reuse the existing ID epoch, `2026-01-01T00:00:00Z`. The time field lasts about
1,115 years, through December 3140. Encode epoch-relative milliseconds and
restore absolute Unix milliseconds when decoding. Reject pre-epoch times and
overflow. Keep the existing BLAKE2s hash domains; extend roaming's path hash
from 32 to 48 bits. Both source IDs are raw 48-bit hashes encoded in 10 chars.

Preserve guarded per-source monotonic allocation, clock-rollback handling,
creation-order selection, and full source/root/home collision checks. Roaming's
first 10 chars encode its type and source hash; retained lookup uses that
canonical source prefix. No compatibility codec or migration is added for
prior literal-prefix codes; retained data is not deleted.

## Implementation and acceptance

Touchpoints: `common/{ids,place_codes,layout}.py`, `up/{addresses,allocation,
discovery}.py`, address/ID design documentation, and identity/placement tests.

- Exact 19/10-char widths and type ranges hold at zero, maximum, and representative hashes.
- Round trips preserve all 48 hash bits and absolute Unix milliseconds; fixed
  vectors pin hashing, epoch, field order, and canonical spelling.
- Invalid types/widths, whitespace, uppercase, pre-epoch times, and overflow fail.
- Repeated clocks, rollback, parallel allocation, timestamp exhaustion, and
  colliding hashes preserve ordering and cannot select or overwrite another owner.
- CLI routing and source-path/short-address lookup accept new codes; discovery
  retains display identity when runtime records are damaged.
- Default verification passes. No open design questions remain.

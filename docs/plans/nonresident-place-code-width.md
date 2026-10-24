# Root and packed place codes

Status: implemented; approved on 2026-10-24.

## Goal and approved scope

Use the `R8`, `P10`, and `P19` formats defined in
[Agent addresses](../design/agent-addresses.md#short-codes). That document owns
bit layouts, hash domains, canonicalization, epoch, and encoded widths.

- Widen ROOT hashes to 40 bits; directories, Docker names, and Hub keys share R8.
- Encode each complete nonresident code with its type inside the value, retaining
  48-bit source hashes and millisecond creation order. Add no literal prefix.
- Preserve guarded monotonic allocation, source/root/home ownership checks,
  retained selection, runtime generations, and other ID families.
- Add no compatibility lookup or migration for previous code formats; leave
  retained data untouched.

## Implementation and acceptance

Touchpoints: `common/{ids,place_codes,layout}.py`, `up/{addresses,allocation,
discovery}.py`, Docker name bounds, Hub namespaces, and identity/placement tests.

- Fixed vectors and boundary cases pin exact widths, all hash bits, field order,
  epoch, leading zeroes, and round trips to absolute Unix milliseconds.
- Reserved types, incorrect widths, noncanonical spelling, pre-epoch times, and
  overflow fail without accepting partial or previous codes.
- Rollback, repeated clocks, parallel allocation, and exhaustion preserve order;
  collisions cannot select or overwrite another owner.
- CLI routing, retained lookup, and damaged-status discovery retain full identity.
- ROOT directory names, container names, and Hub namespaces use the same R8.
- Default verification passes. No open design questions remain.

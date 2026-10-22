# Unify root, place, and home terminology

Approved by the human for pre-release implementation. No compatibility aliases,
fallback lookup, or migration are included. hub namespaces are a separate PR.

## Goal and scope

Use one vocabulary in code, persisted bindings, CLI help/output, and current
documentation. `root`, `place`, and `home` name paths; `root_code` and `place_code`
name short codes. `placement` is resident, visiting, or roaming; host/guest
`location` is independent. Keep runtime generation fencing separate.

For every placement, `HOME = PLACE/agents/NAME`:

| Placement | PLACE | Place code |
| --- | --- | --- |
| Resident | ROOT | None; addresses use NAME |
| Visiting | /tmp/toolang/ROOT_CODE/PLACE_CODE | t + 10 Base32 characters |
| Roaming | DIR/.toolang/ROOT_CODE/PLACE_CODE | s + 7 source-hash characters + 10 timestamp characters |

Keep canonicalization, raw digest truncation, field widths, ordered Base32,
allocation ordering, source-path addresses, ownership checks, active discovery,
and home binding. Use hash domains `place:v1` for root codes, `visiting:v1` for
visiting place codes, and `roaming:v1` for roaming place codes. Root-code hashes
are 32 bits, visiting hashes 48 bits, roaming source hashes 32 bits, and roaming
timestamps 48 bits. The domain changes deliberately select new paths.

## Touchpoints and verification

Update common layout/code helpers, address parsing, preparation and ownership,
runtime discovery/bindings, internal startup arguments, sandbox path mappings,
CLI help/output, their callers/tests, and related design documents. Remove
superseded terminology from historical identity definitions. Preserve unrelated
authored root/home scopes and execution-tree roots.

Acceptance scenarios:

- All placements derive HOME from PLACE; only resident PLACE equals ROOT.
- Exact hash vectors, bounded fields, canonical t/s decoding, and ordered roaming
  allocation agree across independent roots and canonical source aliases.
- Previous markers and persisted field layouts are rejected without fallback;
  full ownership still rejects collisions and mismatched roots/sources.
- Host/guest startup and discovery use place paths and new binding names while
  retaining the selected root and runtime-generation fences.
- Help and copyable output use the new vocabulary; current docs/examples match
  code and contain no superseded identity terms.

Run focused acceptance tests, default repository checks, and `git diff --check`.
Risk: existing pre-release addresses, retained homes, and status records become
incompatible and remain untouched. No open decisions remain in this scope.

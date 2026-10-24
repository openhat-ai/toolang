# Unify root, place, and home terminology

Status: implemented; approved for pre-release implementation.

## Goal and approved scope

Use the vocabulary and placement contract in
[Agent addresses](../design/agent-addresses.md#vocabulary). ROOT, PLACE, and HOME
are paths; root/place codes identify them; placement is independent of host/guest
execution location. Public wording is Toolang root, agent root, and agent home.

Global root selection, Docker's mapped root, and prompt environment paths retain
their distinct owners. Runtime generations remain independent of placement
identity. No compatibility aliases, fallback lookup, or migration are included.
Hub storage namespaces are owned by a separate implementation.

## Implementation and acceptance

Touchpoints: common layout/codes, source preparation, retained selection,
registry/discovery, sandbox bindings, CLI output, and related tests/docs.

- All placements derive HOME from PLACE and preserve root/source/home ownership.
- Host/guest paths differ without changing canonical address or generation.
- Paths, code notation, configuration scopes, and public labels agree with the
  current design; previous identity formats have no fallback.
- Address routing, retained history binding, ordered allocation, source edit
  guards, and runtime recovery keep their established behavior.

Verify focused acceptance tests, default checks, and documentation links/examples.
Current code formats are owned by Agent addresses rather than restated here.

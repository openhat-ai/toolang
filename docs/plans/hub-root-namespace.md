# Hub storage namespaces

Scope approved for namespace-only implementation. Guest transport and the
root/place/home vocabulary and address-prefix changes belong to separate work.

## Goal

Isolate Hub datasets by the host-selected canonical Toolang root, including when
two roots share one backend URL and use identical participant/conversation IDs.
Reuse the root hash from [agent addresses](../design/agent-addresses.md).

## Namespace and identity

Host setup resolves `T = str(root.expanduser().resolve(strict=False))`.
Resolved backend configuration requires T; core modules never resolve paths or
read environment defaults. Reuse `common.place_codes.root_code(T)`: 32 hash bits
encoded as seven lowercase Crockford Base32 characters under the `place:v1` domain.

Every key uses `toolang:<root_code>:v0:<suffix>`, preserving existing suffixes.
Each backend owns its key builder, shared by messaging and execution events.
Lua-generated keys, scans, cursors, staging, and cleanup use the same namespace.

`toolang:<root_code>:v0:root` stores T without expiry. Initialize ownership and
schema atomically; conflicting ownership, partial stores, and corrupt records
fail without writes. Operations validate root ownership before data access.

Backend identity is the first 20 hex characters of SHA-256 over compact UTF-8
JSON `["hub-backend:v1", url, T]`, with `ensure_ascii=False`. Existing discovery,
Hub API headers, and Talk connections use that identity without wire changes.

## Compatibility

Keep namespace version `v0` and HTTP API `0`; use Hub store schema `5`.
Legacy unscoped data is left untouched, without import or fallback. Restart Hub
and reopen Talk after upgrading; old Hub conversation history is not migrated.
Moving the canonical host root selects a new dataset. Equal root hashes cannot
adopt another owner; identical canonical paths on different hosts share identity.

## Acceptance and delivery

- Canonical host path aliases agree; two roots on one backend remain isolated.
- Identical names/DM IDs have independent messages, leases, cursors, and events.
- Collisions, partial data, ownership loss, and bad schemas fail without mutation.
- Hub API rejects other-root identities; Talk uses the selected root's dataset.
- Legacy and other-root data do not block initialization or change during operations.

Touchpoints: `setup/teaming.py`, `teaming/config.py`, backend keys/Lua/events,
Hub/Talk/API tests, and [messaging documentation](../design/messaging.md).
Run repository checks and isolated wire tests; generate the changelog through
`too aid.too update_changelog`. No guest routing or place/address changes ship here.

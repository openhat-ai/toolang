# Hub storage namespaces

Status: implemented; namespace-only scope approved in conversation.

## Goal and contract

Isolate Hub datasets by the host-selected canonical Toolang ROOT, even when
roots share a backend URL and participant/conversation IDs. Reuse R8 from
[Agent addresses](../design/agent-addresses.md#short-codes); guest transport and
placement terminology are separate concerns.

Setup canonicalizes ROOT before passing concrete backend configuration. Every
key uses `toolang:<root_code>:v0:<suffix>`, including Lua keys, scans, cursors,
staging, events, and cleanup. Each backend owns its key builder.

The namespace records full ROOT ownership without expiry. Schema and ownership
initialize atomically; conflicting ownership, partial stores, and corrupt records
fail without writes. Operations validate ownership before data access.

Backend identity is the first 20 hex characters of SHA-256 over compact UTF-8
JSON `["hub-backend:v1", url, canonical_root]`, with `ensure_ascii=False`.
Discovery, Hub API headers, and Talk use that identity. Namespace version remains
`v0`, HTTP API remains `0`, and Hub store schema is `5`. Previous datasets remain
untouched without import or fallback. Identical canonical paths share identity.

## Implementation and acceptance

Touchpoints: `setup/teaming.py`, `teaming/config.py`, backend keys/Lua/events,
Hub/Talk/API tests, and [Messaging](../design/messaging.md).

- Canonical aliases agree; different roots sharing one backend remain isolated.
- Identical participant/conversation IDs retain independent messages, leases,
  cursors, and execution events.
- Collisions, partial data, lost ownership, and bad schemas fail without mutation.
- Hub API rejects mismatched roots; Talk uses the selected dataset.
- Legacy and other-root data remain unchanged during operations.

Run default checks and isolated wire tests. Namespace changes do not alter local
execution history or guest networking.

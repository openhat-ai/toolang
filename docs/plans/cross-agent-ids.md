# Agent-keyed short IDs

Status: implementation scope confirmed by the user on 2026-10-09: add the agent
name to the existing reversible codec and defer API/generalization refactors.

## Goal and scope

Diversify independently generated IDs across agents while retaining the
existing eight-character suffixes, reversible `(tick, seq)` encoding, hourly
buckets, `local`/`run` families, prefixes, and locked `ids.json` allocator.
Same-agent writers continue to share one allocation file. Different agent
encodings share the same 40-bit space; residual cross-agent collisions are
accepted and Hub references remain qualified by agent.

Only add agent-aware encoding and connect the existing callers. Do not split
counters by prefix, change the snapshot format, move allocation to `runs.db`,
add automatic recovery, introduce a generic core/wrapper architecture, or add
new format configuration. Existing group/message IDs are outside this delivery.
Historical codec compatibility is not required; stored IDs are not rewritten.

## Design

The current [codec](../../src/toolang/common/ids.py) generates identical suffixes
for the same family/tick/sequence in separate agent homes. Retain its reversible
affine transforms and Feistel structure, replacing the round function with a
stable keyed hash. Derive the key from the family name and canonical agent name
with unambiguous framing. Names are case-sensitive; paths, PIDs, sandbox names,
and per-process random seeds must not affect encoding.

Add a required `agent_name` argument to `encode_id`, `decode_id`,
`reserve_next_id`, `allocate_id`, and `archive_prefix`, and a required field on
`IdIssuer`. Preserve their other signatures, return types, and responsibilities.
Use the same key in forward/inverse rounds. Pin deterministic codec test vectors.
Reject empty or noncanonical whitespace-padded names instead of inventing an
identity or falling back to the old codec. This is not a security/authentication
API; the wrong name cannot reliably be detected by decoding.

All composition sites pass `layout.name` explicitly: agent core services, CLI
execution resources, local Chat, roaming scripts, and authored job allocation.
Existing runtime, child, compaction, scheduling, and thread operations reuse
that issuer. Task/chore/thread suffixes still share the `local` sequence; runs
use the `run` sequence. Keep file locking, durable writes, time rollback,
capacity limits, and catalog/database duplicate safeguards unchanged.

Manual recovery may decode current-codec IDs from records using the original
agent name and family, then take the maximum `(tick, seq)` per family to help
reconstruct `ids.json`. Record insertion order is not allocation order. Deleted
records and issued-but-unpersisted IDs are not recoverable from `runs.db`; other
sources such as catalogs and scheduler claims may also contain IDs. Recovery,
including these gaps and stopping writers, remains manual. A previously unused
hour has disjoint outputs under the same codec/key; clock jumps can invalidate
that assumption. No automatic migration or old-codec inference is added.

## Touchpoints and acceptance

Change `common/ids.py`, its five production composition/authoring sites, affected
test constructors, codec tests, and focused integration checks. Update
`docs/ids.md` to describe the changed codec/API and manual-recovery boundary.
Generate the Unreleased entry with the repository changelog runnable.

| Scenario | Pass condition |
| --- | --- |
| Round trip | Both families recover boundary and deterministic sampled pairs exactly with the same agent name; suffixes remain eight lowercase base32 characters. |
| Agent separation | Fixed vectors for distinct names, including case differences, diversify equal input pairs; separate homes at the same time use their own agent encoding. |
| Stable identity | The same name and inputs encode identically across process restarts and different host/guest paths. |
| Existing allocation | Same-agent concurrent processes retain unique reservations; persisted counters, hourly rollover, clock rollback, exhaustion, and duplicate checks still work. |
| Integration | Existing issuer construction and authored job allocation pass the resolved canonical agent name; no creation path uses an unkeyed fallback. |
| Manual reconstruction | Out-of-order stored IDs decode to the correct per-family maximum without claiming recovery of unpersisted reservations. |

Run focused tests, then the default ruff, formatting, ty, and offline pytest
checks. Actual shared-mount locking remains a precondition of existing sandbox
allocation, not a newly implemented coordination mechanism. Cross-agent
collisions, agent renaming, and incomplete recovery evidence remain limits.

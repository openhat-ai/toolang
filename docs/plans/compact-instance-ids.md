# Scoped and ordered instance IDs

Status: approved in PR #899. This definition builds on merged PR #897.
Existing formats and layouts are not supported.

## Goal and scope

Use the actual instance ID consistently in CLI output, selectors, JSON, agent
roots and discovery filenames. Top displays `A@I`; `-i` / `--instance` takes
only `I`, including its `v`/`r` marker. Keep visiting identities deterministic from
their scope and URL, and roaming identities ordered by launch within a source.
Resident names and private UUID launch-generation fences remain unchanged.
Top's membership and window semantics from #897 do not change.

## Encoding

Let `R` be the resolved Toolang root, `U` the canonical requested Source Ref,
and `P` the resolved source file path. Use existing source alias normalization
for `U`; do not substitute a fetched commit or invent URL normalization rules.

| Placement | High 32 bits | Low 48 bits |
| --- | --- | --- |
| Visiting | Hash of `R` | Hash of `U` |
| Roaming | Hash of `P` | Assigned launch timestamp in Unix milliseconds |

Hash with the existing framed `derive_id_key` helper, using separate namespaces
`instance-root:v1`, `visiting-url:v1` and `roaming-path:v1`; take the most
significant required bits. Reuse the existing lowercase Crockford Base32 codec.

```text
body = encode_short_id((prefix32 << 48) | suffix48, width=16)
instance_id = ("v" or "r") + body
instance_name = agent_name + "@" + instance_id
```

The body is 16 characters; the placement marker makes the ID 17 characters.
An `aid@r...` name is 21 characters. Encode all 80 bits together: 32/48 are bit
boundaries, not separate Base32 character boundaries. Do not scramble the value.
Before packing, require strict integer components with `0 <= prefix < 2**32`
and `0 <= suffix < 2**48`; checking the packed value alone is insufficient.
Reject noncanonical spellings and invalid characters.
The generic Base32 decoder normalizes case/whitespace; the instance-ID boundary
must require that re-encoding the decoded value reproduces the exact input.

For visiting, the same `R,U` pair always produces the same ID. Across roots,
the URL component stays equal while the root component can differ. Hashes are
compact tags; full source bindings, not short prefixes, establish ownership.

For roaming, serialize allocation under the existing source-cache launch lock:

```text
timestamp_ms = max(wall_time_ms, greatest_retained_launch_ms_for_P + 1)
```

The assigned timestamp is a monotonic logical launch time; record actual wall
time separately. Persist `request_order_ns = timestamp_ms * 1_000_000` and
validate this equality against the decoded suffix on every retained read.
Only fully validated format-4 records bound to the same full `P` can supply
the lower bound; missing/corrupt existing ownership is an error, not zero.
The monotonic guarantee depends on retaining that source's allocation history;
deleting all of it resets the namespace. Fixed-width IDs sort by launch for
the same canonical path.
Across different paths, compare decoded timestamps for chronological sorting:
whole-ID ordering first compares path tags. A 48-bit millisecond field spans
approximately 8,920 years; exhaustion fails explicitly and never wraps.

## Agent roots, homes and instance files

Keep the existing directory structure and place new visiting roots below
`/tmp/toolang`, separate from unrelated temporary files.
For agent name `A` and complete instance ID `I`:

| Placement | Agent root | Agent home |
| --- | --- | --- |
| Resident | `R` | `R/agents/A/` |
| Visiting | `/tmp/toolang/A@I/` | `/tmp/toolang/A@I/agents/A/` |
| Roaming | `<source_dir>/.toolang/A@I/` | `<source_dir>/.toolang/A@I/agents/A/` |

`<agent_home>/.runtime/status.json` remains the authoritative instance file.
Its control bindings record the complete ID/name, placement, canonical root,
full source URL or path, and assigned startup order. Claim and verify the full
visiting source binding under its home write guard before mutating cached
programs or history. Existing unbound or unreadable homes must not be adopted.
Pure layout derivation and inspection do not create homes or claim ownership.
Recompute hash components from the full bindings on read; a stored ID alone
does not prove its root or source. Require the name, root/home/status paths and
discovery filename to agree with that validated identity.

`R/.runtime/instances/A@I.json` remains a discovery reference containing only its
format version, absolute status-file path and UUID `launch_id`. Residents use
`A.json`. Do not copy activity or workload state into this reference. Removing
it changes discovery membership, not retained history.

Derive every displayed instance label, root basename and discovery filename
from the same canonical `instance_name`. Keep the agent name `A` as the inner
home component; do not introduce display aliases or alternate ID spellings.
For existing-instance commands, pass the complete `instance_id` as
`-i I` / `--instance I`; reject `A@I` and bodies without the placement marker.
The command's target already supplies the agent name or source.

## Collisions and security

The structured ID does not have 80 independent random bits. Birthday estimates
for uniformly distributed tags are:

| Tag | 1,000 distinct inputs | 10,000 distinct inputs |
| --- | --- | --- |
| 32-bit root/path prefix | 0.0116% | 1.16% |
| 48-bit URL suffix within one root | 0.000000177% | 0.0000178% |

Across a million URLs in one root, the 48-bit suffix collision estimate reaches
0.177%. Names further partition actual home/registry namespaces, but safety
must not rely on that reduction. Repeated launches of one roaming path do not
consume new random path tags; their timestamp uniqueness is enforced by locks.
For roots sharing the same URL, the suffix is identical and the complete
visiting collision probability depends on the 32-bit root tag alone.

- For both placements, a detected complete-ID ownership collision immediately
  terminates the launch with an explicit error. Do not retry, increment the
  timestamp, randomize, extend the ID or attempt any other recovery. Preserve
  existing files and references; publish no provisional instance. Verify full
  canonical root/URL bindings for visiting and check roaming candidate roots
  and current references under source and registry publication guards.
- Create an absent `/tmp/toolang` atomically as an effective-user-owned `0700`
  directory. Accept an existing private `0700` directory owned by that user,
  or an administrator-provisioned root-owned `01777` shared parent. Reject
  every other owner/mode and all symlinks without chmod, deletion or fallback.
  Resolve the platform's `/tmp` alias first, then use link-safe operations
  under the verified parent. Instance roots are user-owned `0700`; records
  are `0600`. Recheck bindings under the write guard before reuse. Predictable
  names still permit pre-creation denial of service; fail without adoption.
- IDs and hashes are public names, not authorization secrets or content
  signatures. Keep credentials out of filenames and normal logs. URL content
  integrity remains governed by source receipts and captured State.
- Preserve UUID generation fencing. Validate registration/status versions,
  paths and full ownership before use or cleanup. Rejected reads/publications
  must preserve existing records. Run publication-failure cleanup only after
  attempting this caller's write; fence cleanup by both status path and UUID
  so a collision cannot remove another owner, even with equal UUID values.

## Format cutover and module boundaries

Read and write only instance/reference format 4. Format 3 and earlier fail
with an explicit unsupported-format diagnostic; never treat them as absent,
rewrite them, or clean their references as a side effect of rejection. Update
all status/reference consumers, including observation fallback paths, together.

Use only the names and paths defined above. No older-ID codecs, old-home
search/reuse, mixed-format ordering, migration or automatic renaming/deletion.
Stop old workloads before upgrading; retained files can be kept outside the
new runtime's scope. An overlapping hexadecimal-looking ID spelling does not
override the required format version or full source/path bindings.

- `common.id_codec`: existing Base32 primitives, unchanged.
- `common.instance_ids`: pure bit packing, hashing, decoding and comparison.
- `common.layout`: concrete names and paths; no discovery or runtime I/O.
- `up.source`: existing URL canonicalization and source preparation; pass
  resolved inputs to identity code rather than duplicating source parsing.
- `up.identity`: source ownership, locked allocation and retained selection.
- `up.instance` and `up.observation`: versioned status/reference parsing,
  publication and generation-safe cleanup.
- CLI renderers/selectors: Top consumes canonical names; `-i` / `--instance`
  consumes complete IDs. No local shortening or duplicate parsing rules.

Likely changes also include source preparation, their direct unit tests,
CLI boundary tests, [layout](../design/layout.md), [CLI](../design/cli.md),
[Top](../design/top.md) documentation, and the generated changelog.

## Acceptance tests

1. Codec round trips cover 32/48-bit boundaries, leading zeroes, malformed
   values, noncanonical spellings and overflow; body length is 16 and same-path
   ordering is preserved.
2. Visiting URL aliases map consistently; root changes affect only the root
   component. Instance names, home paths, selectors and reference names agree.
3. Roaming path aliases agree; parallel launches, identical clocks and clock
   rollback produce distinct increasing IDs with matching persisted units;
   other paths and file mtime do not determine a source's lower bound.
4. Forced complete-ID ownership collisions immediately fail visiting/roaming
   launches without retry or alternate allocation. Collisions, timestamp
   overflow and foreign `/tmp` paths preserve all existing records and never
   mix source history. Direct filesystem tests cover both accepted parent
   modes, wrong owners/modes and symlink races.
5. Direct lifecycle tests preserve UUID replacement fencing and reject invalid
   records without destructive cleanup. Observation creates no instances.
6. Old formats/paths are neither adopted nor rewritten; unsupported records
   remain unchanged and produce explicit errors, including when their ID
   spelling also matches the new Base32 alphabet.
7. Hub, Top and JSON expose identical IDs. Top displays `A@I`; both `-i I` and
   `--instance I` select that instance, including the `v`/`r` marker. Selectors
   reject `A@I` and markerless bodies. #897 scope semantics remain intact.
   Default static checks and the offline test suite pass.

Open questions: none.

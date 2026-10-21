# Scoped and ordered instance IDs

Status: proposed for user review. This definition builds on merged PR #897 and
ships no implementation. It replaces the earlier random short-ID proposal.

## Goal and scope

Use the actual instance ID consistently in CLI output, selectors, JSON, agent
roots and discovery filenames. Keep visiting identities deterministic from
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
Reject noncanonical spellings, invalid characters and out-of-range components.

For visiting, the same `R,U` pair always produces the same ID. Across roots,
the URL component stays equal while the root component can differ. Hashes are
compact tags; full source bindings, not short prefixes, establish ownership.

For roaming, serialize allocation under the existing source-cache launch lock:

```text
timestamp_ms = max(wall_time_ms, greatest_retained_launch_ms + 1)
```

Include compatible legacy startup orders when calculating the lower bound.
The assigned timestamp is a monotonic logical launch time; record actual wall
time separately. Fixed-width IDs sort by launch for the same canonical path.
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
visiting source binding before mutating cached programs or history.

`R/.runtime/instances/A@I.json` remains a discovery reference containing only its
format version, absolute status-file path and UUID `launch_id`. Residents use
`A.json`. Do not copy activity or workload state into this reference. Removing
it changes discovery membership, not retained history.

Derive every displayed instance label, root basename and discovery filename
from the same canonical `instance_name`. Keep the agent name `A` as the inner
home component; do not introduce display aliases or alternate ID spellings.

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

- Visiting collisions cannot be resolved by randomizing or extending the ID:
  that would break deterministic mapping. Verify the full canonical root and
  URL; report conflicting ownership without adopting or modifying its files.
- Check roaming candidate roots and current registry references under source
  and registry publication guards. Advance the timestamp on a complete-ID
  collision, up to 128 attempts. Never overwrite a retained root or another
  status binding; exhaustion fails without publishing a provisional instance.
- Verify owner, private permissions and filesystem bindings for `/tmp/toolang`
  and its instance roots; reject unexpected symlinks and foreign-owned paths.
  A shared multi-user parent needs trusted provisioning; do not weaken its
  permissions to bypass ownership failures. Predictability still permits
  pre-creation denial of service in shared `/tmp`;
  do not silently delete or adopt the conflicting directory.
- IDs and hashes are public names, not authorization secrets or content
  signatures. Keep credentials out of filenames and normal logs. URL content
  integrity remains governed by source receipts and captured State.
- Preserve UUID generation fencing. Validate registration/status versions,
  paths and full ownership before use or cleanup. Rejected reads/publications
  must preserve existing records; stale cleanup must not remove a replacement.

## Compatibility and module boundaries

New instances and references use format 4; retain read/selection support for
format 3. New visiting Base32 bodies can also look hexadecimal, so use the
recorded version rather than guessing the encoding from its characters.
Resolve an explicit selector to its record before interpreting legacy fields.

Do not rename, delete or automatically rewrite legacy roots. Reuse a verified
legacy visiting home for its existing source rather than splitting history or
creating a parallel replacement. New encoding applies to newly created homes;
legacy history migration is outside this PR. Mixed-format roaming selection
compares validated startup orders, not raw strings from different formats.

- `common.id_codec`: existing Base32 primitives, unchanged.
- `common.instance_ids`: pure bit packing, hashing, decoding and comparison.
- `common.layout`: concrete names and paths; no discovery or runtime I/O.
- `up.identity`: source ownership, locked allocation and retained selection.
- `up.instance` and `up.observation`: versioned status/reference parsing,
  publication and generation-safe cleanup.
- CLI renderers/selectors: consume canonical names and IDs; no local shortening
  or duplicate parsing rules.

Likely changes also include source preparation, their direct unit tests,
CLI boundary tests, [layout](../design/layout.md), [CLI](../design/cli.md),
[Top](../design/top.md) documentation, and the generated changelog.

## Acceptance tests

1. Codec round trips cover 32/48-bit boundaries, leading zeroes, malformed
   values and overflow; body length is 16 and same-path ordering is preserved.
2. Visiting URL aliases map consistently; root changes affect only the root
   component. Instance names, home paths, selectors and reference names agree.
3. Roaming path aliases agree; parallel launches, identical clocks and clock
   rollback produce distinct increasing IDs without depending on file mtime.
4. Forced tag/full-ID collisions, exhaustion and foreign `/tmp` paths preserve
   all existing records and never mix source history.
5. Direct lifecycle tests preserve UUID replacement fencing and reject invalid
   records without destructive cleanup. Observation creates no instances.
6. Legacy visiting homes are reused, old roaming history remains selectable,
   mixed-format latest selection works, and ambiguous hexadecimal-looking
   Base32 values are interpreted using their recorded version.
7. Hub, Top, JSON and CLI selection expose identical IDs; #897 scope semantics
   remain intact. Default static checks and the offline test suite pass.

Open questions: none; the proposed definition awaits user approval.

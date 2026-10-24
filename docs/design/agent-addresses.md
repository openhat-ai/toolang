# agent addresses

[Layout](layout.md) and [Hosting](hosting.md) describe storage and execution;
[CLI command recognition](cli-command-recognition.md) defines command parameters.

## Vocabulary

| Term | Meaning |
| --- | --- |
| root | Selected Toolang root path, defaulting to `~/.toolang`; owns configuration, resident agents, active registrations, and hub discovery. |
| place | Internal name for the agent root path; contains configuration, prepared data, and agent homes. |
| home | `PLACE/agents/NAME`; owns one agent's Threads, Runs, and history. |
| placement | How an agent is placed: resident, visiting, or roaming. |
| `root_code` | Short code derived from the canonical root path; groups its nonresident places. |
| `place_code` | Short code selecting a visiting or roaming place; resident places need none. |
| agent address | Selects a place/home pair within the selected root. |
| location | Where a runtime executes: host or guest; independent of placement. |
| `launch_id` | Private runtime-generation fence; changes independently of the home/address. |

`ROOT`, `PLACE`, `HOME`, and `DIR` are paths; codes are directory-safe strings.
User-facing wording uses Toolang root, agent root, and agent home. Global
`--root`/`TOOLANG_ROOT` selects the Toolang root. Docker's `root` setting and
agent prompt environment `root` denote the agent root. Internal bindings use
`root` for the Toolang root and `place` for the agent root.

For every placement, `HOME = PLACE/agents/NAME`:

| Placement | PLACE | Named address |
| --- | --- | --- |
| Resident | `ROOT` | `NAME` |
| Visiting | `/tmp/toolang/ROOT_CODE/PLACE_CODE` | `NAME@<10-char code>` |
| Roaming | `DIR/.toolang/ROOT_CODE/PLACE_CODE` | `NAME@<19-char code>` |

The table describes host storage; sandboxes map place/home paths into the guest.
`DIR` is the canonical source file's parent. Resident lookup uses its fixed path
without allocating a place code.

## Short codes

```text
root_code           = E(H32("place:v1", canonical_root), 7)
visiting place_code = E((0b11 << 48) | H48("visiting:v1", canonical_url_or_ref), 10)
roaming place_code  = E((H48("roaming:v1", canonical_source_path) << 45) | elapsed_ms, 19)
```

`Hn` selects the leading `n` bits of BLAKE2s
`derive_id_key(domain, canonical_input)`; `E(value, width)` encodes the complete
packed integer using fixed-width ordered lowercase Base32 `encode_short_id`.
Keep leading zeroes and use raw digest bits, without hex shortening.

| Code | High-to-low bit layout | Raw width | Encoded width |
| --- | --- | --- | --- |
| Root | 32-bit root hash | 32 bits | 7 chars |
| Visiting | `11`, 48-bit requested URL/ref hash | 50 bits | 10 chars |
| Roaming | `00`, 48-bit source-path hash, 45-bit elapsed milliseconds | 95 bits | 19 chars |

The two high bits identify placement; `01` and `10` are reserved. Roaming starts
with `0–7`, visiting with `rstvwxyz`. No literal `s`/`t` marker is prepended.
Reject incorrect widths, type/width combinations, and noncanonical spelling.
Both source IDs are raw 48-bit hashes encoded in 10 chars; roaming's first
10 chars also form its stable type/hash prefix.

`elapsed_ms` is measured from the shared ID epoch, `2026-01-01T00:00:00Z`.
Its 45 bits cover about 1,115 years, through December 3140. Decode back to
absolute Unix milliseconds for allocation and request ordering. Reject
pre-epoch times and overflow rather than wrapping. Prior complete literal-prefix
codes are unsupported; retained data is left untouched.

The ordered alphabet, high digits first, and zero padding preserve numeric
order. Within one source, lexical roaming-code order is creation order.

Canonical paths use `expanduser().resolve(strict=False)`. Remote identity uses the
parser-rendered requested ref: equivalent explicit GitHub refs/URLs share identity;
shorthands keep their requested identity. Generic HTTP(S) URLs preserve parser
spelling, including path/query. Fetched content and resolved commits do not replace
the requested identity. Agent names and root identity are excluded from place
codes; equal addresses may exist under different roots.

For new source-only local execution, hold the source/root creation guard and choose
`timestamp = max(clock_ms, highest_retained_timestamp + 1)`. Create exclusively;
empty unpublished candidates advance the timestamp; conflicting ownership fails.
Publish complete ownership before selection. Restarting a retained home preserves
its place code and creation order. Validate full source/root/place/home bindings
on every access; hash collisions cannot overwrite or select another owner.

## Layout and addresses

```text
ROOT/                                     # Resident place
  agents/alice/                           # Resident home
  .runtime/active/
    alice/info.json
    lex@<place_code>/info.json  # 10-char visiting code
    aid@<place_code>/info.json  # 19-char roaming code
    <address>/register.lock                # Exists only while owned

/tmp/toolang/<root_code>/
  root.json                               # Canonical ROOT
  <place_code>/                           # Visiting place: 10-char code
    agents/lex/                           # Visiting home

DIR/.toolang/<root_code>/
  root.json                               # Canonical ROOT
  <place_code>/                           # Roaming place: 19-char code
    agents/aid/                           # Roaming home
```

Each root-code directory records the full root path in `root.json`; collisions
fail. Source-edit locks remain shared across roots editing the same authored
files. Preserve ownership/link validation.

| Address | Lookup |
| --- | --- |
| `name` | Resident home directly under ROOT. |
| `name@<10-char code>` | Visiting home derived directly from ROOT and the place code. |
| `name@<19-char code>` | ROOT's active registry supplies the source path; then validate the home. |
| `path/to/file.too@<19-char code>` | Derive the roaming place from the canonical source path; works offline. |

Require complete place codes; angle-bracketed values above are placeholders.
A code alone is not an address. Nonresident place directory names contain only codes; addresses add a
name or source path. `agent:name` and `agent:name@CODE` escape command-name
collisions. Explicit remote schemes parse before suffixes, so Git revisions and
URL `@` retain their source meaning.

Resident/visiting lookup always uses paths. Short roaming lookup requires active
registration even from the source directory; after exit use the source-path form.
No cwd fallback, cross-root scan, or historical global roaming index is provided.
Retained inspection needs ownership metadata/store, not source contents.

## Binding and execution

The command contract determines whether an undecorated input denotes a source or
an existing home. Home commands accept these shortcuts without preparing sources:
resident name selects its fixed home, ref/URL derives its visiting home, and a bare
local path selects its last-created matching roaming home. Missing homes fail.
Source inspection/workspace edits never allocate retained roaming places or change
that default. New source-only local execution allocates a fresh place.

An explicit address fixes root/place/home/store throughout Chat, rerun, fork, rewind,
and `--chat`. New Runs, Threads, or runtime generations stay there. A Thread/Run
reference also binds its owning home: for a roaming source, search only that
source's retained places under the current Toolang root. One match binds; no match
fails; multiple matches require an address. Explicit address/record mismatches
fail. Preserve imported-history control restrictions.

Bare `-t` selects the latest Thread in the selected home; an unqualified roaming
source first selects its last-created home. Missing Threads/history never cause
fallback to an older home. Source/default changes cannot override record binding.

Lookup fixes storage. Runtime operations separately validate owner liveness,
endpoint, settings, drivers, and `launch_id` under the existing management guards:

| Owner state | Chat/rerun action, including `fork/rewind --chat` |
| --- | --- |
| Live, compatible | Attach to the same executor; retain its lifetime owner. |
| Live, incompatible | Fail without changing settings or enabling drivers. |
| Conclusively stopped | Recover under guard and start in the same home with a new `launch_id`. |
| Preparing/starting/shutting down | Use the existing bounded wait, then recheck under guard; timeout fails. |
| Unknown or replaced during the command | Fail without starting a second owner. |

Bound startup uses the home's workload and required inputs; missing/invalid inputs
fail without fetching a replacement source, copying history, or allocating a place.
Preserve placement lifetimes: resident persistent, visiting automatic, roaming
foreground. Attachments do not gain shutdown ownership. Existing Run admission
and Thread locks govern concurrency; one home has at most one executor owner.

`start/serve/stop` cover resident/visiting only. Roaming foreground shutdown belongs
to its owner through Ctrl-C/signals/kill. `steer/cancel` control a live Run, including
roaming, and never start/stop its executor. Chore commands currently accept
resident names only; controls require a live chore driver. `chore list` combines
authored definitions, saved scheduler checkpoints, and recorded Runs without
requiring a running server.

`fork/rewind` edit an idle Thread using existing guarded offline transactions when
stopped. Fork includes its anchor; rewind removes the anchor and suffix. `--chat`
follows the committed edit; Chat failure reports and preserves that edit.

## Discovery and copyable output

Active records live at `ROOT/.runtime/active/<address>/info.json`, fenced
by the `launch` field. They may describe startup/shutdown and do not prove liveness.
Remove them on exit; preserve places/history. Known-path lookup is independent of
registration. A later runtime at that home reuses the address with a new generation.

`top` shows full resident names or `name@CODE` addresses. The address equals the
active directory name; a nonresident place basename supplies only the place code.
`info` shows `Root` (the agent root), `Home`, and `Agent`, plus runtime details
when available. The output ends with the table.
Addresses copied from `top` require the same Toolang root in another command.

Expanded `top`/`chore` scope, home-based `cd`, JSON binding metadata, and Toolang-root
scoping of hub backend namespaces remain follow-ups; namespace formats are not
defined here. Old instance flags, IDs, and placement paths remain unsupported.
Format-5 registry references remain readable for normal resource recovery.

# Agent addresses

[Layout](layout.md) and [Hosting](hosting.md) describe storage and execution;
[CLI command recognition](cli-command-recognition.md) defines command parameters.

## Vocabulary

| Term | Meaning |
| --- | --- |
| root | Selected Toolang root path, defaulting to `~/.toolang`; owns configuration, resident agents, active registrations, and hub discovery. |
| agent root | Shared root-scope inputs, prepared State, agent definitions, and workspaces. |
| runtime root | Root of instance-owned runtime data; `AgentLayout.place` stores this path. |
| agent home | `AGENT_ROOT/agents/NAME`; owns source/configuration and home/agent State. |
| runtime directory | `RUNTIME_ROOT/agents/NAME/.runtime`; owns status, Threads, Runs, history, and logs. |
| placement | How an agent is placed: resident, visiting, or roaming. |
| `root_code` | Code identifying the selected canonical ROOT. |
| `place_code` | Short code selecting a visiting or roaming place; resident places need none. |
| agent address | Selects an agent runtime instance within the selected root. |
| location | Where a runtime executes: host or guest; independent of placement. |
| `launch_id` | Private runtime-generation fence; changes independently of the runtime instance/address. |

`ROOT` selects the Toolang root through `--root`/`TOOLANG_ROOT`. Placement
resolves shared agent paths and independent runtime paths. Runtime identity fields
`place` and `home` identify `RUNTIME_ROOT` and
`RUNTIME_ROOT/agents/NAME`; address selection below always binds runtime storage.

| Placement | Agent root | Runtime root | Named address |
| --- | --- | --- | --- |
| Resident | `ROOT` | `ROOT` | `NAME` |
| Visiting | `/tmp/toolang/<R8>/<P10>` | `/tmp/toolang/<R8>/<P10>` | `NAME@<P10>` |
| Roaming | `DIR/.toolang` | `DIR/.toolang/<R8>/<P19>` | `NAME@<P19>` |

`DIR` is the canonical Program's parent. Agent home is `AGENT_ROOT/agents/NAME`;
lab is `AGENT_ROOT/workspaces/NAME/lab`. Roaming shares these paths across
instances and selected Toolang roots. R8/P19 continue isolating runtime records
and ownership. [Guest placement](layout.md#agent-placement) maps both roots into
the guest while preserving host identities.

## Short codes

```text
root_code           = E(H40("place:v1", canonical_root), 8)
visiting place_code = E((0b11 << 48) | H48("visiting:v1", canonical_url_or_ref), 10)
roaming place_code  = E((H48("roaming:v1", canonical_source_path) << 45) | elapsed_ms, 19)
```

`Hn` selects the leading `n` bits of BLAKE2s
`derive_id_key(domain, canonical_input)`; `E(value, width)` encodes the complete
packed integer using fixed-width ordered lowercase Base32 `encode_short_id`.
Keep leading zeroes and use raw digest bits, without hex shortening.

| Code | High-to-low bit layout | Raw width | Encoded width |
| --- | --- | --- | --- |
| Root (`R8`) | 40-bit root hash | 40 bits | 8 chars |
| Visiting (`P10`) | `11`, 48-bit requested URL/ref hash | 50 bits | 10 chars |
| Roaming (`P19`) | `00`, 48-bit source-path hash, 45-bit elapsed milliseconds | 95 bits | 19 chars |

`R8`, `P10`, and `P19` denote complete codes in examples; their letters and widths
are notation, never literal prefixes. The same `R8` identifies nonresident
directories, Docker names, and Hub namespaces.

In place codes, the two high bits identify placement; `01` and `10` are reserved.
Roaming starts with `0–7`, visiting with `rstvwxyz`. No literal `s`/`t` marker
is prepended.
Reject incorrect widths, type/width combinations, and noncanonical spelling.
Both source IDs are raw 48-bit hashes encoded in 10 chars; roaming's first
10 chars also form its stable type/hash prefix.

`elapsed_ms` is measured from the shared ID epoch, `2026-01-01T00:00:00Z`.
Its 45 bits cover about 1,115 years, through December 3140. Decode back to
absolute Unix milliseconds for allocation and request ordering. Reject
pre-epoch times and overflow rather than wrapping. Previous code formats have no
compatibility lookup or migration; retained data is left untouched.

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
    lex@<P10>/info.json                     # Visiting address
    aid@<P19>/info.json                     # Roaming address
    <address>/register.lock                # Exists only while owned

/tmp/toolang/<R8>/
  root.json                               # Canonical ROOT
  <P10>/                                  # Visiting place
    agents/lex/                           # Visiting home

DIR/.toolang/<R8>/
  root.json                               # Canonical ROOT
  <P19>/                                  # Roaming runtime root
    agents/aid/.runtime/                   # Roaming runtime directory
```

Each root-code directory records the full root path in `root.json`; collisions
fail. Source-edit locks remain shared across roots editing the same authored
files. Preserve ownership/link validation.

| Address | Lookup |
| --- | --- |
| `name` | Resident home directly under ROOT. |
| `name@<P10>` | Visiting home derived directly from ROOT and the place code. |
| `name@<P19>` | ROOT's active registry supplies the source path; then validate the home. |
| `path/to/file.too@<P19>` | Derive the roaming place from the canonical source path; works offline. |
| `path/to/file.too@` | Latest existing runtime instance for the canonical source and current ROOT; missing fails. |

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
an existing runtime instance. History commands resolve without preparing sources:
resident name selects its fixed runtime, ref/URL derives its visiting runtime,
and a bare local path selects its last-created matching roaming instance.
Missing history fails. Source inspection/workspace preparation uses that
ROOT-scoped instance resolution; only preparation commands may allocate when none exists. Reuse never changes
creation order. Unnumbered legacy homes are preserved and ignored.

| Local source operand | Script execution | `info` |
| --- | --- | --- |
| `file.too@P19` | Run in that instance. | Inspect that instance. |
| `file.too@` | Run in the latest instance. | Inspect the latest instance. |
| `file.too` | Create a fresh instance. | Inspect the latest instance, or prepare one if absent. |

Explicit selectors never allocate or fall back. Latest means instance creation order
under the canonical source and ROOT, independent of age, status or reuse. Preparation
creates durable identity and State without an executor, Run or active registration.
An owned instance's `info` observes status and available saved resource metadata without
projecting inputs, rebinding grants or renewing ownership.

An explicit address fixes root/place/home/store throughout rerun, fork and rewind,
and Chat. New Runs, Threads, or runtime generations stay there. A Thread/Run
reference also binds its owning home: for a roaming source, search only that
source's retained places under the current Toolang root. One match binds; no match
fails; multiple matches require an address. Explicit address/record mismatches
fail. Preserve imported-history control restrictions.

Bare `-t` selects the latest Thread in the selected runtime instance. Missing Threads/history never cause
fallback to an older home. Source/default changes cannot override record binding.

Lookup fixes storage. Runtime operations separately validate owner liveness,
endpoint, settings, drivers, and `launch_id` under the existing management guards:

| Owner state | Resident/visiting Chat/rerun action, including `fork/rewind --chat` |
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

Roaming Script, Chat, and rerun instead claim the runtime instance exclusively from preparation
through cleanup: preparing, running, closing or unverifiable ownership fails
immediately. Each invocation has a new launch identity, current grants and startup
settings; history remains in its P19, while lab and prepared State are shared.
Different instances may run concurrently on host/guest.
Bare roaming Chat allocates a new P19; explicit addresses and `-t` select retained
instances through the same lookup rules. `fork/rewind --chat` requires a stopped
instance and continues its resulting Thread without allocating another P19.

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

Hub namespace behavior belongs to [Messaging](messaging.md). Runtime record
formats and recovery belong to [Layout](layout.md#runtime-state-and-discovery)
and [Hosting](hosting.md#runtime-observation-and-retirement).

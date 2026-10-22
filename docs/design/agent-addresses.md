# agent addresses

Identity, area layout, instance selection, and bound continuation are implemented.
[Layout](layout.md) and [Hosting](hosting.md) describe their storage and execution;
[CLI command recognition](cli-command-recognition.md) defines command parameters.

## Vocabulary and identity

| Term | Meaning |
| --- | --- |
| Toolang root | Command namespace for configuration, resident agents, active registrations, and hub; selected by defaults or supported root options. |
| `area_id` | Directory-safe identifier of the canonical Toolang root; groups its nonresident roots. |
| agent root | Environment root; the Toolang root for resident agents, a separate root for visiting/roaming agents. |
| agent home | `<agent_root>/agents/<name>`; owns Threads, Runs, and history. |
| agent address | Selects an existing root/home pair within the command's Toolang root. |
| runtime | Current executor process/container. |
| `launch_id` | Private startup-generation fence; changes independently of the home/address. |

Use snake_case identifiers/placeholders. Visiting root IDs are `hotel_id` (`h`);
roaming root IDs are `site_id` (`s`). These name IDs, not extra directory layers.

```text
area_id  = E(H(canonical_toolang_root))
hotel_id = "h" || E(H(canonical_url_or_ref))
site_id  = "s" || E(H(canonical_source_path)) || E(timestamp)
```

`H` selects leading bits of BLAKE2s `derive_id_key(domain, canonical_input)`;
`E` is fixed-width lowercase Base32 `encode_short_id`. Encode raw digest bits,
without hex shortening. Fields are independent:

| Field | Hash domain | Bits | Base32 width |
| --- | --- | --- | --- |
| Toolang root hash | `area:v1` | 32 | 7 |
| Requested URL/ref hash | `hotel:v1` | 48 | 10 |
| Source-path hash | `site:v1` | 32 | 7 |
| Unix timestamp, milliseconds | None | 48 | 10 |

`area_id`, `hotel_id`, and `site_id` are 7, 11, and 18 characters. Validate field
bounds and canonical lowercase encoding. Never scramble the timestamp: the
codec's ordered alphabet, high digits first, and zero padding preserve numeric
order. Within one source, lexical site-ID order is creation order.

Canonical paths use `expanduser().resolve(strict=False)`. Remote identity uses the
parser-rendered requested ref: equivalent explicit GitHub refs/URLs share identity;
shorthands keep their requested identity. Generic HTTP(S) URLs preserve parser
spelling, including path/query. Fetched content and resolved commits do not replace
the requested identity. Names and Toolang-root identity are excluded from hotel/site
IDs; equal addresses may exist under different Toolang roots.

For new local execution, hold the source/Toolang-root creation guard and choose
`timestamp = max(clock_ms, highest_retained_timestamp + 1)`. Create exclusively;
empty unpublished candidates advance the timestamp; conflicting ownership fails.
Publish complete ownership before selection. Restarting a retained home preserves
its ID and creation order.
Validate full source/root/home bindings on every access; hash collisions cannot
overwrite or select another owner.

## Layout and addresses

With `T` the canonical Toolang root:

```text
T/                                        # Toolang root; resident agent root
  agents/alice/                           # Resident agent home
  .runtime/active/
    alice.json
    lex@<hotel_id>.json
    aid@<site_id>.json

/tmp/toolang/<area_id>/                    # Visiting roots belonging to T
  root.json                               # Canonical T
  <hotel_id>/                             # Visiting agent root
    agents/lex/                           # Visiting agent home

<source_dir>/.toolang/<area_id>/            # Roaming roots belonging to T
  root.json                               # Canonical T
  <site_id>/                              # Roaming agent root
    agents/aid/                           # Roaming agent home
```

Each area records its full Toolang-root identity; collisions fail. Source-edit
locks remain shared across areas editing the same authored files. Preserve
existing ownership/link validation.

| Address | Lookup |
| --- | --- |
| `name` | Resident home directly under T. |
| `name@h...` | Visiting home directly under T's visiting area. |
| `name@s...` | Current T's active registry supplies the source path; then validate the home. |
| `path/to/file.too@s...` | Derive the roaming root from the canonical source path; works offline. |

Require complete IDs; ellipses above are notation. An ID alone is not an address.
Nonresident root directory names contain only IDs; addresses add name/path. `agent:name` and
`agent:name@ID` escape command-name collisions. Explicit remote schemes parse
before suffixes, so Git revisions and URL `@` retain their source meaning.

Resident/visiting lookup always uses paths. Short roaming lookup requires active
registration even from the source directory; after exit use the source-path form.
No cwd fallback, cross-root scan, or historical global roaming index is provided.
Retained inspection needs ownership metadata/store, not source contents.

## Binding and execution

The command contract determines whether an undecorated input denotes a source or
an existing home. Home commands accept these shortcuts without preparing sources:
resident name selects its fixed home, ref/URL derives its visiting home, and a bare
local path selects its last-created matching roaming home. Missing homes fail.
Source inspection/workspace edits never allocate retained roaming roots or change
that default. New source-only local execution allocates a fresh root.

An explicit address fixes root/home/store throughout Chat, rerun, fork, rewind,
and `--chat`. New Runs, Threads, or runtime generations stay there. A Thread/Run
reference also binds its owning home: for a roaming source, search only that
source's retained roots under the current Toolang root. One match binds; no match
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
fail without fetching a replacement source, copying history, or allocating a root.
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

Active records live only at `T/.runtime/active/`, one filename per address, fenced
by `launch_id`. They may describe startup/shutdown and do not prove liveness.
Remove them on exit; preserve roots/history. Known-path lookup is independent of
registration. A later runtime at that home reuses the address with a new generation.

`top` shows full resident names or `name@ID` addresses. The address equals the
active filename stem; a nonresident agent-root basename supplies only the ID.
`info` shows `Toolang root`, `Agent root`, `Home`, and `Agent`, plus runtime
details when available. An unwrapped, shell-quoted `Command` line follows the
table; it includes `--root T` and a source-path address for roaming, remaining
usable offline. Resident names that collide with commands use `agent:NAME`.
Addresses copied from `top` require the same Toolang root in another command.

Expanded `top`/`chore` scope, home-based `cd`, JSON binding metadata, and Toolang-root
scoping of hub backend namespaces remain follow-ups; namespace formats are not
defined here. Old instance flags, IDs, paths, and registry formats have no
compatibility readers or migration behavior.

# Team member colors in Talk

Status: approved for this plan and implementation in one PR on 2026-10-20.
The human confirmed that colors belong in member
records, are assigned only at initial creation, and can subsequently be changed
through `valkey-cli`. Keep the implementation simple; no new command or endpoint.

## Goal and scope

Give Talk members automatic colors with as little repetition as the existing
palette allows, and support manual changes. Apply color to sender names and
markers for agents and humans in DMs, GCs, and observer views. Preserve message
bodies, emphasis, backgrounds, layout, and scrollback behavior. Other CLI views,
configuration settings, model tools, and saved default/override state are outside
scope.

## Design

Add one required `color` property to the existing member JSON in the Hash
`too:teaming:v0:team:members`:

```json
{
  "display_name": "alice",
  "owner": "human:bryan",
  "created_at": "2026-10-20T00:00:00.000Z",
  "lease": null,
  "color": "cyan"
}
```

- At initial member creation, count current member colors against the existing
  palette: `red`, `green`, `yellow`, `blue`, `magenta`, `cyan`. Select the color
  with the lowest count, breaking ties by palette order. This prefers unused
  colors and reuses colors evenly after exhaustion, with one directory scan.
- Allocate inside the existing atomic member-creation operation, covering both
  registration and roster discovery. Do not add an allocator key, hash-based
  probing, background assignment task, or reservation state.
- Subsequent registrations, lease changes, and restarts preserve `color`.
  Only creating a new directory record assigns a value; deletion removes it.
- Users change that same field. Support standard ANSI color names and `#RRGGBB`;
  explicit duplicates are allowed. Member values must follow the current schema;
  add no compatibility handling for missing or invalid color fields.
- Carry color through the existing team snapshot. Reuse Talk's snapshot refresh
  for both color and presence, including DMs; add no separate polling service or
  notification protocol. Read one bounded snapshot before initial history and
  refresh it once per second. Lookup failures retain cached colors and preserve
  the existing unknown-presence behavior.
- Renderers receive resolved colors explicitly. Updates affect subsequent
  rendering; reopening Talk applies current colors to retained history. Committed
  scrollback stays unchanged and no-color output stays ANSI-free.
- Preserve existing agent tool and execution-publication shapes. Explicitly
  exclude the new field from publication participant metadata. Extend contracts
  through the existing Hub schema/API owners under repository version policy;
  add no new version counter or migration/reset workflow.
- All teaming keys use the prerelease `too:teaming:v0` namespace, reserving
  `v1` for stabilization. Support only current v0 state; add no migration or
  handling for older Valkey or local state.
- Before rendering a batch with unknown senders, try one bounded team refresh
  for the whole batch; preserve cached/fallback rendering if lookup fails.

## Manual edit

Use the Hub's configured server/database. This example atomically
changes only `color`, preserving the current lease and other member properties:

```sh
valkey-cli EVAL '
local raw = redis.call("HGET", KEYS[1], ARGV[1])
if not raw then return redis.error_reply("Unknown team member") end
local member = cjson.decode(raw)
member.color = ARGV[2]
return redis.call("HSET", KEYS[1], ARGV[1], cjson.encode(member))
' 1 too:teaming:v0:team:members agent:alice magenta
```

Replace `magenta` with another ANSI name or quoted RGB value such as `'#7aa2f7'`.
Do not overwrite JSON captured by an earlier separate `HGET`, or `HDEL` a member
just to change its color. Manual edits require an existing member.

## Touchpoints and verification

- [Member schemas](../../src/toolang/teaming/schemas.py), existing Valkey
  `backend.py`/`scripts.py`: color data and atomic creation paths.
- `src/toolang/teaming/backend/valkey/events.py` and existing team clients:
  snapshot handling and unchanged publication/tool projections.
- [Talk rendering](../../src/toolang/cli/toolang/commands/talk/rendering.py) and
  `tui.py`: sender/marker styling and shared snapshot refresh.
- Existing messaging and Talk tests; update [Messaging](../design/messaging.md)
  and [Terminal UI](../design/terminal-ui.md). Generate the implementation's
  changelog entry through `too aid.too update_changelog`.

Acceptance scenarios:

1. The first six automatically assigned members have different colors; later
   creation selects a least-used color, including mixed human/agent identities.
2. Concurrent creation cannot consume the same previously free color; roster
   creation and direct registration follow the same rule.
3. Manual ANSI/RGB edits affect agent/human names and markers after refresh;
   duplicates work, and restart/re-registration/lease changes preserve edits.
4. The documented edit preserves other JSON properties and fails without writing
   for an unknown member. Removing/recreating a member gets a fresh assignment.
5. Lookup failures preserve cached colors, message delivery, and drafts;
   member records follow the current schema.
6. Body/layout/scrollback/no-color contracts and tool/publication shapes remain
   unchanged, with no additional key, command, or API endpoint.
7. An unknown sender's first batch uses the assigned color after one successful
   refresh; a failed lookup still renders the whole batch and advances its cursor.

Use offline tests and controlled clocks. Implementation requires the default
repository checks. Verify the source, links, and manual-edit example alongside
the implementation.

## Limits and open questions

Six palette entries require reuse in larger teams; manual values can reduce
contrast or duplicate colors. Color follows directory-member lifetime, and raw
edits must preserve authoritative fields. No open design questions remain.

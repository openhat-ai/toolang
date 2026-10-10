# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This record starts at the v0.3.4 baseline; earlier history is not backfilled.

## [Unreleased]

### Added

- Conversations now use canonical IDs and optional names: direct messages use
  `dm_` plus eight base32 characters derived from the two typed participant
  names, custom conversations (GCs) use `gc_` plus eight backend-allocated
  base32 characters, and either kind accepts an editable name of 1–128
  characters, duplicates included, that a participant may set or clear against
  the conversation's current revision. Human observers can read every
  conversation while agents read only ones they joined, and the bundled `msg`
  toolset gains `msg/resolve` (wire name `msg__resolve`) to resolve a
  participant, canonical ID, or, with `by_name`, a conversation name, returning
  candidate IDs when a name is ambiguous.

- `too talk` with no target prints a one-shot `Team` and `Convos` directory and
  exits, without requiring a TTY and without opening a session or tmux window:
  `Team` lists visible agents and humans with canonical IDs, display names,
  owners, and agent online state, while `Convos` lists accessible conversations
  with separate ID and Name columns (an unnamed conversation shows an em dash),
  kind, participants, and latest-message state.

- The Hub serves the resumable team feed at
  `GET /team/events?after=t1.<epoch>.<stream-id>`: an initial checkpoint
  precedes snapshots, about 100,000 entries retain member, presence, and
  conversation-membership changes, and a trimmed, ahead, or previous-epoch
  cursor emits `resync_required` and closes for a cursorless reconnect.
  Conversation statistics are exposed at `GET /msg/stats` (human-only global
  totals including the system GC) and
  `GET /msg/conversations/{conversation}/stats` (participants, lifetime and
  retained messages, and the last stream ID).

- `too top` and `too AGENT top` open a full-screen dashboard on the alternate
  screen, restoring the terminal on exit, with Header, Table, Details, and
  Status bar regions: Header shows the scope, the local clock, and the selected
  Stats and Activity windows, Table adds an `S` presence column (`+` online,
  `-` offline, `?` unknown), Details renders on-demand result text as Markdown,
  and F7/F8 cycle the Activity and Stats windows.

- `too AGENT top` reads the selected agent's local run store, so it needs no
  running agent, Hub, or Redis/Valkey and keeps retained history readable while
  the agent is offline. The Hub likewise serves activity pages, streams, and
  on-demand results from a managed local run store when one is available,
  otherwise from the online source.

- Hub scans its root every 5 seconds with `too list` discovery: new homes are
  offline with unavailable activity and vanished online homes show `home
  missing`; after two absent scans with no live lease, only the
  roster/participant entry and ordinary group memberships are removed,
  preserving DM mappings and membership, messages, event history, activity
  caches, and last-seen. A 5-second heartbeat under a 15-second lease marks
  agents offline within 15 seconds of a crash or graceful stop.

- `too top` adds IN, CACHED, and OUT token columns (CACHED is cache-read input
  included in IN; OUT is normalized output) in compact k/M/G units, with exact
  values in Details and `-` for unknown; SPEND replaces COST and `--sort spend`
  aliases `--sort cost`.

- `too top` adds activity views, filters, sorting, and consumption statistics:
  `--view agent|thread|execution` selects the row object, `--tree` (with
  `--view execution`) follows confirmed running paths, `--sort activity|spend|time`
  orders whole objects by displayed values, `--filter TEXT` matches IDs, names,
  statuses, and summaries literally and case-insensitively, `--active` keeps
  unfinished work only, and `--recent DURATION|all` (default `30m`) and
  `--since session|TIMESTAMP|DURATION|all` (default `session`) select visible
  recent work and the stats start shared by MODEL, TOOL, IN, CACHED, OUT, SPEND,
  and TIME+. Each row's metrics cover only its own and transitive calls, and
  incomplete coverage is labeled instead of shown as idle.

- Agents expose activity over HTTP at `GET /api/v1/activity`,
  `/api/v1/activity/batch`, and the SSE `/api/v1/activity/stream`, and enabled
  agents publish the same snapshots to the Hub, which serves `GET /activity` and
  `/activity/stream` from last-observed coverage while an origin is offline.

- `too top` observes a running team or agent without starting execution: `too top`
  follows the running Hub for the whole team, while `too AGENT top` reads the
  selected agent's local run store. `--once`
  prints one snapshot and exits, as does redirected output; interactive views
  apply asynchronous updates and render on the `--refresh` interval (default
  `0.1` seconds), reconnect from a fresh baseline with backoff, and exit on `q`
  or Ctrl-C. Rows show agent summaries and active root trees, retain work in the
  rolling Recent range (default `30m`) with running paths visible regardless of
  age, label offline, incomplete, and reconnecting origins explicitly, and omit
  content bodies.

- Enabled agents export their canonical event stream to the teaming backend
  independently of execution and messaging, including retained finals from
  offline origins, and the Hub serves it at `GET /events/stream`.
  Optional `agent=agent:alice` selects one origin, and `thread=ID` or `run=ID`
  selects a thread or root tree under it; no filter observes the whole team.
  Execution frames keep their event name and payload and add `agent`,
  `source_cursor`, and the Hub `cursor` as the SSE `id`; structural context
  carries `context: true` and no `id`, `stream_prefill` stages replacements that
  `stream_checkpoint` commits, and `stream_status` reports `{agent, online,
  complete, reason}`. Invalid filters and cursors return `400`, unknown or
  unretained scopes `404`, and an unavailable backend `503`, while a live
  subscription reports `overflow`, `snapshot_limit`, `backend_unavailable`,
  `scope_unavailable`, or `protocol_error` before closing.

- Execution streams resume from a durable cursor: the run and retry/rerun POST
  streams, `GET /api/v1/runs/{run_id}/stream`,
  `GET /api/v1/threads/{thread_id}/stream`, and the new agent-scoped
  `GET /api/v1/stream` accept `after=CURSOR` and emit SSE `id`s, so a client
  can reconnect and continue after its last committed event instead of
  repeating POST. Invalid or future cursors return `422` before admission.
  Recovery replays retained events or rebuilds structure from records, bounded
  to 10,000 records, 16 MiB, and five seconds, and otherwise ends the
  subscription with `stream_error` code `snapshot_limit`.

- Chat and the TUI recover an interrupted run stream with up to three `GET`
  reconnections from the last committed cursor before falling back to durable
  run detail, and clear partial rendering when structure is replaced.

- Interactive Text renders each agent's name and its `•` message marker in the
  same non-dim ANSI color derived from the name, kept stable across restarts
  and conversations.

- The root-scoped teaming Hub is managed with `too hub start`, `serve`,
  `stop`, and `status`: `start` runs it in the background and waits for
  backend readiness, `serve` runs it in the foreground, `stop` leaves agents
  and Redis/Valkey running, and `status` reports the endpoint and current
  backend readiness. Hub binds `127.0.0.1` and records its actual endpoint,
  human, and backend identity in the root's private
  `.runtime/hub.json`.

- Hub `start`/`serve` ports resolve as `--port` > `TOOLANG_HUB_PORT` >
  `teaming.hub.port` > `7000`, and resident agent `start`/`serve` resolves
  its API port as `--port` > `TOOLANG_AGENT_PORT` > home `[api] port` > a
  free recorded port, otherwise an available port in `7001`–`7999`;
  temporary agents keep their existing selection.

- Humans can observe an agent DM in Interactive Text read-only: both agents'
  messages appear, the composer is removed, and the footer shows `Read-only`,
  with existing history and drafts retained. Text continues to present the
  configured human identity; no agent-identity CLI option was added.

- Interactive Text reserves a dedicated blank row above its input box in
  addition to the message separator, preserving message-to-message spacing, and
  omits the gap on terminals shorter than five rows to keep editing space.

- Interactive Text bounds its input box and footer to the same content width
  as its messages, so both stay inside the terminal and line up with the
  scrollback on wide terminals.

- Text renders every sender name at full brightness, with agent names in bold,
  instead of dimming all of them.

- Interactive Text keeps connection state (`Connecting…`, `Connected`,
  `Reconnecting…`, or `Stopped`) persistently in its footer, with `Sending…`
  while in flight and `Sent` for about two seconds, and reconnects no longer
  print repeated scrollback notices. Failures show `Send failed` or `Send not
  confirmed` so an unconfirmed message is checked before retrying.

- `too text` with no target lists conversations, pinning `group:all` first and
  ordering the rest by recent activity, listing each conversation's participants
  with online/offline badges for agents only and a preview of the latest message
  beside the target that opens the conversation. (#708)

- Interactive Text starts left-aligned names and the first body line's `•` marker
  in column 1, with body text in column 3. Owner names sit outside and above the
  background at the upper right; owner bodies have two cells of horizontal
  padding on each side and one row above and below, and their first body line
  carries Chat's cyan `▮` marker in the outermost right padding cell.
  Right-aligned agent DMs carry a `•` at the right edge of their first body line,
  and very narrow terminals reduce the decoration to preserve content.

- Interactive Text lays out messages within Chat's maximum content width (120
  columns by default, configurable with `TOOLANG_PROGRESS_MAX_WIDTH`), capped by
  the current terminal width.

- Teaming is enabled by default: an agent participates unless its `config.toml`
  sets `[teaming].enabled = false`, and a disabled agent makes no backend
  connections. The backend URL defaults to `redis://localhost:6379/0`, so a
  local Redis or Valkey server needs no URL, and local execution works while
  the backend is unavailable. (#708)

- `too text TARGET [MESSAGE...]` opens a conversation or sends a message and exits.
  It resolves a bare name that matches exactly one participant or conversation, plus
  canonical `agent:`, `human:`, and `group:` targets (`all` means the public
  `group:all`), with `--dm`/`--group` to disambiguate. Omitting the message opens
  interactive Text with a live input below the scrollback (Enter sends, Ctrl+J inserts
  a newline, Ctrl+P/Ctrl+N browse sent input), preserves drafts on a failed send, and
  keeps per-conversation history. Inside tmux each root/connection/human gets a session
  and each conversation a reusable window; `TOOLANG_TMUX=0` stays in the invoking pane. (#708)

- The bundled `msg` toolset exposes `msg/targets` (wire name `msg__targets`) to
  list registered participants and accessible conversations with their canonical
  targets, `msg/send` (wire name `msg__send`) to send a message immediately, and
  `msg/create_group`, `msg/join_group`, and `msg/leave_group` to create and manage
  custom groups. Enabled hosted agents poll the Hub and
  handle incoming batches through `agic:msg`, or the default agic, replying in the
  source group; own messages do not re-trigger handling and failed or malformed
  batches are logged and skipped. (#708)

- Teaming is configured in the root `config.toml` under `[teaming]`: `human`
  (defaulting to the OS username) and `[teaming.backend].url` naming an externally
  running Redis or Valkey server. Membership, presence, and message streams live in
  that backend, so agents sharing it communicate across machines, and root settings
  cannot appear in an agent's `config.toml`. Adds the `valkey` runtime dependency;
  see `docs/messaging.md` for setup. (#708)

### Changed

- `too top` Header `Runs` now counts every Activity-eligible run before
  filters, including completed runs, showing `0` when empty and `-` when
  unavailable, while active, failed, and `idle` summaries stay in Table
  ACTIVITY.

- `too top` shows only function-key hints in its Status bar — F1 Help, F4
  Filter, F5 View, F6 Sort, F7 Activity, F8 Stats, and F10 Quit — so `F5`
  cycles the Agent, Thread, Run, and Tree levels, `F10` exits like `q` and
  Ctrl-C, and letter and Enter shortcuts remain available through Help; the
  Status bar no longer shows `Incomplete`, whose coverage and freshness
  detail stays in Details.

- `too top` keeps a fixed Header height for each terminal width, so changing
  counts, filters, and windows no longer moves the Table while navigating,
  and the mouse wheel moves the Table selection with mouse reporting
  released on exit.

- **Breaking:** messaging stores canonical conversations instead of groups under
  schema `2` of the `too:teaming:v1` backend space: `group:` IDs, the
  `/msg/groups` routes, and the group record fields and aliases are replaced by
  `dm_`/`gc_` IDs and `/msg/conversations` with `/{id}/participants`,
  `/{id}/messages`, `/{id}/cursor`, and `/{id}/stats` subresources plus
  revision-checked PATCH renames. Upgrade Hub and clients together on a fresh
  Redis/Valkey dataset; missing or corrupt initialized conversation data fails
  closed without resetting counters or rebuilding records, and old history,
  drafts, and checkpoints are neither migrated nor imported.

- **Breaking:** `too talk` accepts positional targets only: `alice` opens the
  current human's DM with that agent and creates it only on the first accepted
  send, showing empty history until then; `alice,bob` observes an existing
  agent-agent DM read-only; and `dm_...` or `gc_...` opens that conversation
  directly. `--dm`, `--group`, and `group:` targets are removed, bare names
  always select agents, missing agent-agent DMs and unknown IDs fail without
  creating anything, and the system GC is initially named `all` and opened by ID.

- **Breaking (tool API):** the `msg` conversation tools are renamed:
  `msg/create_group` becomes `msg/create_conversation`
  (`msg__create_conversation`), which now creates a GC or explicitly creates or
  reuses a two-participant DM, while `msg/join_group` and `msg/leave_group`
  become `msg/join_conversation` (`msg__join_conversation`) and
  `msg/leave_conversation` (`msg__leave_conversation`); the corresponding
  `MessagingClient` and `HubClient` create, join, and leave methods use the
  same new conversation names, and their `resolve` returns the conversation,
  its participants, and whether it exists. Update tool calls, prompts, and
  integrations to the new names.

- Presence is now the backend lease deadline alone: a five-second heartbeat
  renews the deadline to at least 15 seconds ahead, an expired deadline grants
  no authority even before cleanup, readers compare deadlines with their own
  clocks and correct only their local view, and the Hub reconciles up to 128
  due agents per batch, checking once a second and draining additional full
  batches with cooperative yields. The `last_seen` field is removed from activity
  snapshots, responses, and displays, local messaging checkpoints move to
  `v2-<backend-identity>.json`, and Talk state moves to `.runtime/talk-v2/`.

- **Breaking:** the `too text` command is renamed to `too talk`, and its tmux
  windows now share one `talk` session: a window is reused by canonical
  conversation ID and connection context (root, backend, viewer, and Hub
  endpoint) instead of one session per root, connection, or human, and a new
  window is named by its canonical ID. Scripts and habits must switch to
  `too talk`, while `TOOLANG_TMUX=0` still keeps Talk in the invoking terminal.
  Talk also shows conversation identity: the footer shows the conversation, its
  canonical ID, and the viewer's login, and publishes a compact label
  (`@alice`, `@alice,bob`, or `#dev`) as the terminal title, cleared on exit.
  (#719)

- `too top --since DURATION` selects a rolling Stats window that is
  re-evaluated as time passes instead of one fixed at start; pass a
  timezone-aware timestamp for the previous fixed start.

- `too top` uses one responsive three-region layout: header totals and a context
  line, a full-width one-line-per-row body, and a fixed bottom key bar, with
  shared column widths, right-aligned numerics, and MODEL/TOOL hidden below 110
  columns. `--refresh SECONDS` (default 0.1) replaces the fixed redraw, while
  keys, filters, view changes, and resize repaint immediately from cached state.

- Hub activity streaming updates agents independently with shared per-query
  projections, so one slow or offline agent no longer stalls the others; roster
  changes and each agent's replacement are atomic, omitted agents are never
  treated as removed, and reconnects baseline first.

- Schema 54 upgrades automatically to 55, adding per-attempt input, cached, and
  output token facts with a resumable backfill that settles tokens at the same
  accounting boundary as spend; historical attempts without durable accounting
  stay explicitly unknown.

- `too top` retention follows `--recent`: completed root runs and threads stay
  while the rolling range covers them, running paths remain visible regardless
  of age, and larger results load through pagination with loaded and available
  counts.

- **Breaking:** reversible short IDs for runs, threads, tasks, and chores are
  now keyed by the canonical agent name, so equal tick/sequence pairs from
  independent agents usually encode differently and collide less often while
  the permutations still share one 40-bit space, so cross-agent collisions
  remain possible; the suffix length, alphabet, `ids.json` format, and
  existing stored IDs are unchanged. Pass the agent name to `encode_id`,
  `decode_id`, `reserve_next_id`, `allocate_id`, `archive_prefix`, and
  `IdIssuer`, and decode historical unkeyed IDs with the previous unkeyed
  codec, since supplying an agent name to the new codec does not recover them.

- The Hub HTTP API no longer requires bearer-token authentication: requests may
  identify the backend and human with optional `X-Toolang-Backend` and
  `X-Toolang-Human` headers, and a mismatch fails with `409` code `hub_changed`.
  The private `.runtime/hub.json` no longer records a token, though records that
  still carry one load normally, and Text reconnects across a Hub restart,
  needing a reopen only when the Hub's endpoint, backend, or human changes.

- **Breaking (teaming transport):** agents now reach messaging, presence, and
  event export through the Hub HTTP API instead of connecting to Redis/Valkey
  directly, so only Hub talks to the backend. Start the Hub with `too hub
  start` before using agent `msg` tools or exporting agent events; stopping it
  leaves agents running, and they resume without a
  restart, while an uncertain send is never retried automatically. Isolated
  guests without access to the local Hub report unavailability. (#721)

- Interactive Text draws a dim bright-black `┄` rule on its own row above
  each agent's complete name, starting at the body inset and reaching the
  message area's right edge; it is present even at narrow widths and with
  wrapped names. Left-aligned messages, including human bubbles, now fill the
  available configured content width, while own right-aligned messages keep
  their gutter. Sender name and marker colors and non-dim styling are
  unchanged.

- **Breaking (streaming API):** execution SSE is no longer live-only. Source
  events carry `cursor` in `data` and an SSE `id`, while structural context
  events carry `context: true` and no new `id`; new `stream_prefill` and
  `stream_checkpoint` controls delimit a bounded structural replacement that
  commits one cursor, and retry streams begin with `run_retried` and their
  retained prefix. Consumers that read id-less live events or assumed no
  replay must commit cursors and reconnect with `after`; `Last-Event-ID` alone
  selects no cursor, and a POST must never be repeated. Each stream write,
  including headers, has a five-second deadline; idle waits have none.

- Interactive Text moves the message marker (`•` for agents, `▮`
  for humans) onto the sender-name row and insets names by the same
  horizontal padding as the message body, instead of placing the marker
  on the first body line.

- Root help groups `hub` and `text` under a new `Teaming Commands` panel
  instead of `Work Commands`; command names, options, and routing are
  unchanged.

- Live run and thread subscriptions and caller run tracers now share one
  bounded canonical event stream, so observation no longer blocks execution.
  A subscription that falls behind receives a `stream_error` with code
  `overflow` and closes while the run keeps executing. (#713)

- The run store schema 52 upgrades automatically to 53, adding durable event
  cursor columns to runs, steps, and controls while preserving records whose
  cursors are null; schema 52 stores remain readable in read-only mode. (#713)

- Execution commands apply `--catalog` and `--compact-model` only when
  starting an agent runtime, and now reject `--catalog` when attaching to
  a running one instead of ignoring it; stop the agent before changing
  those startup settings.

- `too chat`, `too retry`, `too rerun`, `too steer`, `too cancel`, `too fork`,
  and `too rewind` now ensure a persistent agent runtime and use its API
  instead of embedded host execution. The runtime starts when needed, is
  shared by concurrent starters, waits for an in-progress startup to report
  readiness, and keeps running after the command exits or fails; stop it with
  `too stop <agent>`. `--dev` still applies only when starting a new non-host
  runtime, and script runs keep their existing embedded-host and stop-on-exit
  lifecycle.

- `too text` and Interactive Text now reach messaging through the Hub HTTP
  API instead of connecting to Redis/Valkey directly, so run `too hub start`
  before using Text and reopen Text if the Hub's endpoint, backend, or human
  changes. Agents and
  their `msg` tools use the same Hub API.

- **Breaking:** the experimental `coop` toolset (wire names `coop__contacts` and
  `coop__send`), the public `CoopToolContext` type, the `too team` command,
  `[human]` and `[messaging]` configuration, and old conversation IDs are replaced
  by the `msg` toolset and `MsgToolContext`, typed `agent:`/`human:`/`group:`
  targets, and `too text`, with no data migration or compatibility aliases.
  Configure `teaming.human` and `teaming.backend.url` in the root `config.toml`
  and `teaming.enabled` in each agent home; see `docs/messaging.md`. (#708)

- Direct conversations accept messages only from their two participants, and
  custom groups only from their members. Nonparticipant sends, including a human
  inserting into an agent DM, are now rejected by the shared messaging core and
  one-shot `too text` sends. Use a shared custom group or `group:all` to include a
  third participant; existing history and drafts are retained. (#708)

- Text aligns messages by the current identity — the reader's own on the right
  and every other sender on the left — with names aligned to the outer-edge
  marker, and footer labels name the conversation: its group name (or `all`) or a
  direct conversation's `agent:alice ↔ agent:bob` participants. (#708)

### Fixed

- Active-tree selection no longer rescans all runs for every root: parent run
  IDs are materialized once so recursive lookups are indexed, keeping a large
  completed history from exhausting the five-second snapshot budget before
  optional history is read, with existing tree selection and cursor semantics
  unchanged. (#723)

- Team event observation keeps a large recovery history from stalling or
  replacing the active snapshot: optional SQL reads and structural
  reconstruction now share one deadline of half the five-second snapshot
  budget instead of five seconds each, so a slow optional tree cannot delay or
  evict the active run's snapshot, while the active tree and fully
  reconstructed history are preserved.

- Hub send responses are now validated before a message is treated as
  delivered: a 2xx response with a missing or malformed receipt, a
  different message ID or sender, or an invalid group or empty cursor is
  rejected, and the send is reported as unconfirmed with its message UUID
  and never resent automatically.

- `msg` calls made while handling a received batch now stay on the Hub
  connection that delivered it: a Hub or backend identity change rejects
  the tool request instead of mixing conversations across backends, and other
  roots and independent tasks remain isolated.

- Text renders agent messages with the shared terminal Markdown layout:
  fenced and inline code use the resolved terminal surfaces, tables fill
  the message body width and fold long cells instead of truncating them,
  and headings, lists, quotes, and rules align with the body text.

- `too retry` and `too rerun` keep an accepted run executing when observing
  it fails and report the accepted run ID, instead of canceling it; only an
  explicit interrupt or cancellation still cancels the accepted run.

- The Hub now listens before backend access, reserving its endpoint so
  another process cannot claim the same port while startup is in
  progress. (#710)

- Hub API requests restore the human's registration and the
  system membership when the backend restarted empty, without retrying the
  failed message append. (#710)

- A Hub process that is still starting is reported as `starting` by `too hub
  status` and can be stopped with `too hub stop --force` before the backend
  is ready. (#710)

## [0.4.0a2] - 2026-10-07

### Added

- The `_toolang/runnables` tool (wire name `_toolang__runnables`) discovers
  runnable documentation and complete signatures: omitting `name` returns every
  module-visible declaration, and an exact `name` returns one target. Results
  carry the caller's `current` runnable, its active `ancestors` in root-to-parent
  order, and ref-sorted `runnables` entries with `ref`, State `revision`, the
  full authored `doc`, and a complete `signature` (primary input, ordered
  parameters, output, and reachable structs with field docs, without the former
  512-character truncation). Discovery is descriptive data: it neither filters
  by routes or active-path eligibility nor grants execution.

- The `Tool` plugin protocol gains `bind_arguments(arguments)`, a synchronous,
  side-effect-free hook that returns a new mapping of supplied arguments. The
  executor binds after resolving the tool and before `paths`, passing the bound
  values to `paths` and `invoke`; a binding error fails the Tool Step without
  calling either. The default copies the mapping, so hand-written tools keep
  their argument semantics, and raw call input stays available to `summary` and
  call records.

### Changed

- **Breaking:** the model-facing route declarations replace the per-call
  `<toolang:hands>` and `<toolang:handoffs>` signature snapshots and the
  requested-only policy with one optional self-closing
  `<toolang:routes hands="..." handoffs="..." spawns="..."/>`, where each value
  is `ALL`, `NONE`, or comma-separated exact refs and `spawns` follows `hands`.
  Runnable documentation and signatures are no longer sent on every call;
  discover them with `_toolang__runnables`. Readers of the removed tags or the
  `requested_only` flag must read the new attribute and treat an absent tag as
  `ALL`.

- Runtime facts are grouped into one recurring user message per Model Call,
  ordered `workspace`, `workdir`, optional `routes`, optional `context`, then the
  `execution` declaration, kept separate from authored messages and the primary
  input. The default context carries date, timezone, model provider, and model
  name as `<toolang:context>` attributes, while named and authored contexts keep
  their rendered bodies; skill and service trigger declarations move their
  description into a `description` attribute and the remaining capability
  metadata into a JSON `metadata` attribute.

- **Breaking:** the public `ToolRuntime` protocol gains `runnables(name=None)`,
  so custom tool-runtime implementations must add it.

- **Breaking:** `@tool` function tools now bind supplied arguments against their
  resolved signature before path preflight and invocation. Values follow
  Pydantic's lax conversions (`"7"` becomes `7`), numeric branches reject
  Booleans and non-finite values, strict types stay strict, and callers cannot
  supply the reserved `context`. Unknown names, missing required values, and
  invalid values now fail the call instead of being silently dropped or passed
  through. Migration: declare intended inputs or a real `**kwargs`, and remove
  raw numeric/Boolean string parsing from function `paths` hooks, which now
  receive bound values.

- Function-tool JSON schemas now derive from resolved input annotations, so
  postponed annotations such as `int`, `bool`, and `list[str]` produce typed
  schemas instead of empty ones. Unresolved or unsupported annotations,
  positional-only parameters, `*args`, and wrong-typed defaults now fail tool
  preparation with an error naming the parameter. Explicit
  `@tool(parameters=...)` schemas are preserved and neither disable binding nor
  add validation.

### Fixed

- Every agic Model Call now carries a runtime-owned
  `<toolang:execution runnable="..." entered_by="run|exec"/>` declaration even
  with `context = none`, naming the current runnable and whether it was entered
  by `run` or a committed `exec`, to prevent repeating the original invocation.

- Model-requested `exec` now records the target-bound values in its control and
  builds its replacement locals from them, fixing successful passthrough Runs
  that exposed unreadable typed output. Already-corrupt historical exec records
  are not repaired; invoke the affected work again.

- `rerun` now binds restored native source inputs to the selected target in its
  captured fresh State before admission, so a stored Text `"7"` can satisfy a
  fresh Number parameter. Newly required, removed, unknown, or incompatible
  arguments reject the whole rerun without modifying the source Run.

## [0.4.0a1] - 2026-10-06

### Added

- Flow `async run RUNNABLE` starts a child Run and returns once it is admitted,
  without waiting: `let job = async run RUNNABLE` binds a handle, while
  `async run RUNNABLE` and `let async run RUNNABLE` launch and discard it
  without changing other locals. Named, inline, and typed-inline targets are
  supported.

- Flow `await HANDLE` waits for one retained `async run` or `spawn` handle and
  binds its complete result: `let value = await job` binds only `value`,
  `await job` binds `_`, and `let await job` waits and discards. Scalar, array,
  struct, Part, and null results keep their complete value and provenance, and
  repeated waits reuse the outcome without relaunching the target.

- The `_toolang/run` tool accepts `async=true` and returns its committed
  `{id, thread, status}` admission snapshot without waiting. The new
  `_toolang/await` tool (wire name `_toolang__await`) waits for one async run or
  spawned root admitted by the caller Run and returns its complete
  `{type, value}` or a tool error.

- Repeat statements accept a named Boolean condition as a bare runnable name,
  such as `until is_done`, in addition to the inline `until: BODY` form. Named
  conditions must declare `Boolean` and reject kind or module qualification,
  arguments, and a colon body.

- A `repeat:` with neither a count nor a condition runs unbounded until `exec`,
  cancellation, or failure, and is labeled `Repeat indefinitely` in script help
  and progress. Bounded and condition-only repeats keep their existing limits.

- Flow `spawn RUNNABLE` and `spawn [-> T]: BODY` start an independent root in a
  new empty thread and return once it is admitted, without waiting. Each root
  gets a new run and thread identity with a null parent, inherits the source's
  captured State, settings, limits, cwd, and workspaces, and continues under the
  same executor when its source finishes, fails, is canceled, or hands off.
  Each root completes, fails, or is explicitly canceled on its own, and a root
  still unfinished when the executor stops is canceled then (a script host
  cancels unfinished roots on exit). No completion message is injected.

- `let job = spawn RUNNABLE` binds a Run handle readable through templates as
  ordinary data: `{{job.id}}`, `{{job.thread}}`, and `{{job.status}}`, with one
  status snapshot per statement and no waiting. Unknown fields fail, an authored
  struct named `Run` remains ordinary data, and handles cannot be passed as
  runnable inputs.

- The `_toolang/spawn` tool (wire name `_toolang__spawn`) starts an independent
  root through `run`'s input decoder and hands policy, and returns the committed
  admission snapshot `{id, thread, status: "pending"}`. It waits for no result
  and injects no completion message; inspect progress and results by ID with the
  existing tools.

- Root Runs with no active descendants may `exec` their own entry runnable,
  replacing the run binding with the latest published implementation whose
  normalized contract matches, while preserving the Run, captured Setup,
  authority ceilings, limits, and entry output contract. `run self`, child
  self-exec, and ancestor calls remain rejected.

### Changed

- Bumped the pinned `tree-sitter-toolang` grammar to 0.4.0a4, which provides the
  `async run` and `await` syntax.

- Progress for `run`, `exec`, `async run`, `spawn`, and `await` now uses one shared
  vocabulary, with background events kept separate from linear caller Steps.

- **Breaking:** the internal Run handle encoding is replaced by the shared
  `_Awaitable` type, whose native payload is `{kind, id, thread, result_type}`.
  `_Run` and `_Run<T>` type tags and the two-field `{id, thread}` payload are
  rejected rather than migrated; update integrations that read the handle type
  tag or payload, and rerun work recorded with the old encoding.

- Async children are owned by the Run that launches them: its completion,
  failure, cancellation, `exec`, and executor shutdown drain or cancel
  unfinished owned work, while spawned roots keep independent lifetimes.
  Discarding or awaiting a handle never transfers ownership.

- The public `ToolRuntime` protocol gains an `asynchronous` keyword on `run()`
  and an `await_target(target)` method, so custom tool-runtime implementations
  must add them.

- `SpawnContext` and the `spawn_context` Run control field are renamed to
  `LaunchContext` and `launch_context`, with no alias retained.

- Repeat conditions are positional: an `until` clause may precede, separate, or
  follow its body statements, and at most one condition is allowed. A condition
  binds inputs from current locals at its authored position; a true condition
  exits only its repeat, keeping committed prefix effects and skipping the
  suffix, and never replaces parent locals or the Flow output. Count and
  condition are independently optional.

- **Breaking:** regular local and parameter names must fully match
  `[a-z][a-z0-9_]*` and must not be a grammar keyword. Rename existing
  keyword-named variables together with their references. Same-line `let` text
  beginning with `until` must move into an indented value body, where it
  remains literal text. The `_` primary-input parameter keeps its special
  role, and data fields remain unrestricted.

- **Breaking:** the runtime call tool `_toolang/run` (exposed as
  `_toolang__run`) now runs an authorized hand synchronously, matching flow
  `run`: its Tool Step stays open until the child ends, and its one tool reply
  returns the child's `{type, value}` on success or a tool error on failure or
  child-only cancellation. The scheduling receipt `{run_id, controls}` and the
  separate `run-result` completion message are removed. Integrations that read
  run outcomes from the receipt or the `run-result` context must read the tool
  reply, find the child Run whose parent names the Tool Step, and inspect its
  applied entry control by run ID. A child-only failure or cancellation leaves
  the caller running with a failed Tool Step; caller cancellation or immediate
  steer unwinds the child and cancels the Tool Step.

- Run controls are applied when admission commits, independent of Run
  execution status; runtime-created controls are never left pending. `spawn`
  and `exec` commit their successful source Step together with their controls,
  so later dispatch, execution failure, cancellation, or interrupted delivery
  cannot undo them. Only external `steer` and `cancel` requests stay pending,
  closing as `wontapply` if the Run ends first. A `StepEnd` cut short by a lock
  wait keeps its persisted finish time and is still delivered.

- **Breaking:** Run Control kinds `execute` and `cwd` are renamed to `exec` and
  `chdir`, and their payload types to `ExecControlPayload` and
  `ChdirControlPayload`; `RunStore.accept_exec_control` replaces
  `accept_execute_control`. Update integrations and control-kind filters to the
  new names; the `cwd` location field is unchanged. RunStore schema 52 rejects
  older stores unchanged, with no migration or old-name aliases, so inspect old
  records with their matching runtime and start new runs on a fresh store.

- Flow `spawn` Step outputs use the same `{"type", "value", "binding"}` envelope
  and `output/value` references as ordinary outputs. The native handle's runtime
  type tag is `_Awaitable` and its value holds `kind`, `id`, `thread`, and
  `result_type`; user-authored struct names cannot begin with `_`. The complete
  accepted result contract stays on the spawned root entry; historical ordinary
  stored outputs without an explicit `type` remain readable. Flow `spawn`
  records a spawn-kind Step, while agic `_toolang/spawn` remains an ordinary
  tool Step. Once admission commits, the Step succeeds and keeps its receipt
  even if delivery is interrupted or the caller is canceled, and a dispatch
  failure is recorded on the new root.

- Hands now authorizes both `run` and `spawn`, while handoffs still authorizes
  `exec`. The public `ToolRuntime` protocol adds `spawn(runnable, input)`, so
  custom tool-runtime implementations must add it, and the model-facing runtime
  protocol documents spawn and Run handles.

- Bumped the pinned `tree-sitter-toolang` grammar to 0.4.0a2, which provides the
  `spawn` syntax.

- `reduce` with a `from:` initializer accepts an empty outer array and returns
  that initializer coerced to the reducer output type without any child calls;
  without an initializer it still rejects an empty array.

- **Breaking:** `storm` is renamed to `generate` and `settle` to `reduce`, and
  `generate`, `map`, and `reduce` now require `using` for named targets and must
  omit it for inline bodies, so `map using [-> T]: BODY` becomes
  `map [-> T]: BODY`. `reduce` keeps its optional `from:` initializer.
- **Breaking:** flow arrays use the actual outermost array and there is no
  separate item/list shape. `run` makes exactly one child call with the complete
  input, including arrays, without iterating, wrapping, or flattening; `map`,
  `keep`, `drop`, `sort`, and `reduce` operate on outer items. Arrays from
  parameters, calls, helper flows, `exec`, and restored values behave
  identically, and array-valued child results stay nested.
- **Breaking:** the `Local` wrapper is removed. Stored and HTTP/event outputs
  share the `type`, `value`, and `binding` envelope; update output references
  from `output/local/value` to `output/value`. RunStore schema 52 rejects older
  stores before mutation with no compatibility reader or migration, so keep the
  matching runtime to inspect old records and start new runs on a fresh store.

- **Breaking:** the `me` tool `me__loaded(receipts)` is replaced by `me__sync()`,
  which waits for one State publication and returns `{revision, files}` for the
  agent's tracked root and home sources. Integrations that poll `me.loaded`
  receipts must call `me.sync()` / `me__sync({})`, handle operational error codes,
  and use the scoped `files` manifest as the receipt.

### Removed

- **Breaking:** the `scatter` and `gather` flow statements are removed. Rewrite
  `scatter using R` and `gather using R` as `run R`, and give a former `scatter`
  body an explicit `run -> Text[]` type; a former `gather` may now receive `[]`.
  Source, snapshots, retries, and reruns using them are rejected with migration
  guidance, and older records remain available only to their matching runtime.

### Fixed

- Historical repeat trees now place `until` at its authored position,
  consistently in Human and JSON inspection. (#695)

- Repeated `until` evaluations now refresh Run handle status, and named
  Flow conditions retain their correct progress boundary with nested loops.
  (#695)

## [0.3.6] - 2026-10-04

### Added

- `me` adds `me__loaded(receipts)`, which compares home-file `{key, digest}`
  receipts with the State bound to the calling Run and returns `loaded`, the
  loaded `revision`, and only mismatching keys. (#679)

### Changed

- **Breaking:** the `me` tools now address the current agent's home files by
  home-relative path instead of resource kind and name. `me__list()` takes no
  arguments; `me__get(key)` reads whole files; `me__create(key, content)` fails
  if the file exists; `me__update(key, content, if_digest)` replaces whole
  content; and `me__delete(key, if_digest)` deletes one file and no longer
  manages task/chore lifecycle. Integrations using the old `kind`/`key`
  arguments or field-level updates must migrate; restart older writers to
  release the retired `.authored-flows.lock` and `.project.lock`. (#679)

### Fixed

- Flow and prompt templates retain typed values (Boolean, struct, array, and
  `Part`) instead of coercing them to text, and native strings such as `"false"`
  are not re-parsed into other types. (#680)

## [0.3.5] - 2026-10-03

### Added

- Flow `exec` statements replace the current runnable within the same Run and
  never return on success. Named and inline targets use the same forms and input
  binding as `run`, and named targets resolve from the latest published State. (#675)

### Changed

- Resource selectors (`models`, `tools`, `psyches`, `skills`, `services`,
  `prompts`) now filter the latest State within their declaring modules. Children
  inherit restrictions rather than an earlier selected list, and `+=` restores
  only items still allowed by ancestors and authority ceilings. (#675)
- Hands/handoffs snapshots now omit only the current runnable and its ancestors
  on the calling branch, so earlier handoffs, completed children, and siblings are
  callable again. (#675)
- **Breaking:** the runtime call tool `_toolang/execute` (exposed as
  `_toolang__execute`) is renamed to `_toolang/exec` (`_toolang__exec`), and the
  public `ToolRuntime.execute()` method to `ToolRuntime.exec()`. Integrations that
  invoke the tool or implement the method must switch to `exec`. (#675)
- Execution progress dividers are unified: Run boundaries use `┌`/`└`, `Run` and
  `Execute` captions use sentence case, and loop iterations show centered counters
  such as `1/3`. (#672)

### Fixed

- The formatter normalizes `exec` statements consistently with other flow
  statements. (#676)

[Unreleased]: https://github.com/openhat-ai/toolang/compare/v0.4.0a2...HEAD
[0.4.0a2]: https://github.com/openhat-ai/toolang/compare/v0.4.0a1...v0.4.0a2
[0.4.0a1]: https://github.com/openhat-ai/toolang/compare/v0.3.6...v0.4.0a1
[0.3.6]: https://github.com/openhat-ai/toolang/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/openhat-ai/toolang/compare/v0.3.4...v0.3.5

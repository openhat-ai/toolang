# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This record starts at the v0.3.4 baseline; earlier history is not backfilled.

## [Unreleased]

### Added

- Roaming script and Chat launches each keep a fresh executor and a retained
  `<source_dir>/.toolang/<agent>@<instance_id>/` root, while `--instance/-i`
  selects retained history instead of restarting an old root.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- Host scripts expose the full shared Agent API on their existing event loop,
  restoring Top live activity and generation-fenced controls.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- Roaming reruns and explicit Chat continuations snapshot SQLite into fresh
  roots, keeping imported history inspectable without duplicate activity,
  usage, or replayed events.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

### Changed

- **Breaking:** Stop every running agent with `too AGENT stop` or Ctrl+C before
  upgrading or rolling back, because instance layouts and RunStore schema 63
  are incompatible with older releases.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- **Breaking:** Visiting roots move from `/tmp/toolang-<agent>-<source_id>` to
  `/tmp/<agent>@v<source_id>`, and legacy directories stay in place for you to
  retain separately.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- **Breaking:** Legacy roaming shared and nested histories stay put without an
  automatic migrator, so retain them separately and start fresh roots under
  `<source_dir>/.toolang/<agent>@<instance_id>/`.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- **Breaking:** Resident and visiting launches with different effective options,
  including `--enable`, now fail before preparation instead of joining the
  existing root; align those options or run `too AGENT stop` first.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- **Breaking:** Roaming `serve` is removed; run `too FILE.too` instead, or
  `too FILE.too chat --enable chat` for Chat.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- Resident ports reuse their previous value only when available, otherwise
  7002-7999; visiting ports reuse theirs only when available, otherwise an
  OS-assigned port; roaming launches get a fresh OS-assigned port.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- OpenAPI documents name the former `top` group `activity`, while HTTP paths
  and contracts stay unchanged.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- Hub snapshots align status and version with agent fields, widen the name
  column, color agent names by placement, and dim instance IDs.
  ([#886](https://github.com/openhat-ai/toolang/pull/886))

- **Breaking:** `too [AGENT] home` becomes `too [AGENT] cd` with no alias, so
  scripts that call `home` must call `cd`; `cd` prints the resolved agent name
  and destination path before starting the shell.
  ([#887](https://github.com/openhat-ai/toolang/pull/887))

- **Breaking:** runnable references use `module::runnable`, with builtin module
  `_`, main module `agent`, and modules named after `runnables/<module>.too`,
  while `agent.too` and `_.too` are reserved module filenames. Rename a
  conflicting runnable module and its named export before upgrading.

- **Breaking:** a `runnable` field keeps the runnable's full name, and the
  separate `runnable_kind` field carries its kind, so API and event consumers
  read `runnable_kind` instead of parsing the kind from `runnable`.

### Fixed

- Failed preparation no longer leaves active registrations; runtime references
  remain until resource release, Stats works for embedded scripts, and
  incomplete replay seeds are rejected.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- CLI controls attach to foreground roaming executors on their real published
  port, covering steer, cancel, profile, and Top.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- Roaming `fork --chat` and `rewind --chat` enable chat in their fresh
  continuation executor.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- Copied chore checkpoints and imported Talk receipts stay inert in the new
  owner: they drive no destination work and never advance live Hub cursors.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- A roaming launch whose registry publication fails ends as a failed root you
  can select, not a stuck preparing owner.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- Chat and `start` promotion retain the running executor's drivers when
  `--enable` is omitted, while explicit differences still reject before
  preparation.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

- The Hub label in `too hub` now uses bright cyan to match resident agent
  names.
  ([#888](https://github.com/openhat-ai/toolang/pull/888))

- Talk keeps human sender names in the default foreground with bright cyan
  markers, applies member colors only to agents, and uses a thin dotted
  divider in default dim.
  ([#889](https://github.com/openhat-ai/toolang/pull/889))

- Help describes `top` as "Monitor agent runs", and the Work commands panel
  lists `chore` immediately above `talk`.
  ([#891](https://github.com/openhat-ai/toolang/pull/891))

- Agent startup and Chat shutdown no longer leak coroutines, omit running
  peers' port reservations, or hang waiting on a submission to a stopped loop.
  ([#892](https://github.com/openhat-ai/toolang/pull/892))

- Automatic compaction binds to builtin `_::compact`, fixing checkpoint
  ownership failures from the builtin Talk handler, while a user
  `runnables/compact.too` still runs as `compact::compact`.

- Retained `runnables::<module>` route restrictions keep authorizing run and
  handoff targets, and targets named `ALL` or `NONE` are no longer mistaken for
  protocol sentinels.
  ([#895](https://github.com/openhat-ai/toolang/pull/895))

- Earlier Run and exec labels follow the current naming rules, including
  anonymous entry runnables, and new Run captions show the accepted runnable
  name.

- Top and Hub no longer warn about availability when an instance unregisters
  mid-discovery, keeping durable usage and flagging only current
  registrations' failures.
  ([#894](https://github.com/openhat-ai/toolang/pull/894))

### Security

- The locked certifi trust store advances to 2026.10.19, resolving trust-store
  vulnerability GHSA-6q48-6c7w-9gw2.
  ([#884](https://github.com/openhat-ai/toolang/pull/884))

## [0.4.0a11] - 2026-10-20

### Fixed

- Fresh macOS installs of `0.4.0a10` failed because mstache `0.4.0` produced
  prerelease metadata that uv rejected; releases now require mstache below
  `0.4`.

## [0.4.0a10] - 2026-10-20

### Changed

- `too AGENT info` lists enabled drivers below `Status`, showing `none` when
  none are enabled, `-` when metadata is unavailable, and omitting the row for
  stopped agents.
  ([#878](https://github.com/openhat-ai/toolang/pull/878))

- **Breaking:** `ThreadInfo` and `ThreadDetail` drop `channel`, and thread-list
  filters omit it, so derive classification from the Thread ID and filter by
  `kind`.
  ([#874](https://github.com/openhat-ai/toolang/pull/874))

- **Breaking:** `ThreadPrefix` and prefix creation arguments become
  `ThreadKind` and `kind`, thread records and `ThreadCreated` payloads drop
  `origin`, and thread HTTP responses expose neither field, so derive kind
  from the ID prefix; replace `origin` filters with `kind` on `GET /threads`.
  ([#874](https://github.com/openhat-ai/toolang/pull/874))

- **Breaking:** thread IDs must start with `chat_`, `talk_`, `chore_`, `work_`,
  or `exec_`, and invocation threads move from `script_` to `exec_`.
  ([#874](https://github.com/openhat-ai/toolang/pull/874))

- Fork and rewind work for every thread kind: fork keeps the source kind
  without Talk bindings or chore definitions, and rewind keeps message
  cursors and chore checkpoints.
  ([#874](https://github.com/openhat-ai/toolang/pull/874))

- **Breaking:** `SourceFile.origin` becomes `layer` on root and agent sources,
  so code reading `origin` must read `layer`.
  ([#874](https://github.com/openhat-ai/toolang/pull/874))

- Dulwich 1.2.17 or newer is now required in runtime and build
  environments, so an older pinned release must be upgraded.
  ([#870](https://github.com/openhat-ai/toolang/pull/870))

- In editable/development source checkouts, root `too` help and
  `too --version` start faster and select the release version despite
  unrelated refs or branches named like tags.

- Chat terminal titles use `AGENT>` before a thread title exists and
  `AGENT> title` afterward, while Top titles read `top` and `AGENT top`.
  ([#873](https://github.com/openhat-ai/toolang/pull/873))

- Talk members keep the least-used of six assigned colors across registration,
  leases, and restart, and manual ANSI or RGB edits survive.

- **Breaking:** all teaming keys move from `too:teaming:v1` to
  `too:teaming:v0`, so restart Hub, agents, and Talk together; existing v1 data
  stays untouched and is not imported.

- `too top` and `too AGENT top` show a THREAD column between AGENT and RUN
  in live trees, lists, snapshots, and recent completions.
  ([#869](https://github.com/openhat-ai/toolang/pull/869))

- **Breaking:** reducer initializers use `initial:` instead of `from:` in both
  named and inline reducers. Replace each `from:` clause with `initial:`,
  because an inline `from:` line is now literal text.

- **Breaking:** a literal `initial` word now starts reducer syntax at a flow
  statement boundary or an inline reducer's text baseline; write such prose
  after `run:` in a flow or indent it deeper inside an inline reducer.

- **Breaking:** raw CST consumers read `*_stmt` nodes and `stmt` member fields
  instead of `*_statement` nodes and statement paths.

- Syntax errors highlight native recovery fragments and retained diagnostics
  in bold red in terminal and HTML output, while keyword and name captures
  keep their semantic styles.

- **Breaking:** the formatter preserves inline text literally, including `#`
  and `"`, and never extracts or moves comments from it; place intended
  comments on separate structural lines.

- **Breaking:** program documentation moves from `##!` to `#@`, while `##!`
  now documents an item with a literal leading `!`; rewrite existing program
  documentation with `#@`.

- **Breaking:** lane clauses belong before `using`, `if`, and `by`: the
  formatter rejects suffix order without reordering it, so move existing suffix
  clauses ahead of those keywords.

- `lane`/`lanes` and `time`/`times` both parse, and the formatter writes
  canonical unit spelling and removes leading zeros in those counts.

- Parameters and locals may use keyword names where the grammar allows.

- **Breaking:** retired flow words are ordinary prose and names instead of
  errors; rewrite `scatter`/`gather` as `run`, annotating array outputs, `storm`
  as `generate`, and `settle` as `reduce`, with no automatic conversion.
  ([#868](https://github.com/openhat-ai/toolang/pull/868))

- **Breaking:** agic and flow directive prefixes are closed, so an unknown
  `key = value` opening line is a syntax error; put assignment-like opening
  messages in an explicit `user: key = value` text block in agics and
  `run: key = value` in flows.

- Cap and chore prefixes accept arbitrary `key = value` properties, with blank
  lines and comments allowed between them, until the first substantive body
  line; supported names stay validated.

- **Breaking:** raw CST consumers read `program` instead of `source_file`,
  `target` for call targets, `local` for `let` bindings, `param` and
  `description` on direct `param_doc` nodes, and `query_value` and
  `runnable_name` leaves instead of the `directive_value` and `runnable`
  wrappers.

- **Breaking:** CST text values use `content` instead of `text_inline` and
  `text_body` wrappers, with direct `text` leaves replacing `text_line`,
  `indented_raw_text`, and `comment_text`.

- **Breaking:** documentation uses peer `program_doc`, `item_doc`, and
  `param_doc` nodes; `program_doc` belongs to `program`, while structural
  bodies include their leading member documentation.

- **Breaking:** raw CST punctuation, fixed operators, and keywords are anonymous
  terminals, so inspect terminal text instead of named nodes.

- **Breaking:** invalid sources surface malformed targets, directives,
  messages, assignments, and annotation types as native `ERROR` and missing
  terminals, retaining only `invalid_missing_body`, `invalid_missing_content`,
  `invalid_missing_stmt`, `invalid_param`, and `invalid_flow_stmt`; every
  `invalid_*` node is an error even when `has_error` is false.
  ([#868](https://github.com/openhat-ai/toolang/pull/868))

- `too top` and `too AGENT top` count agent instances separately from root
  Runs: the first summary reads `N running, M total: R resident, V visiting,
  O roaming`, where a running idle agent still counts as running, preparing or
  starting agents are not yet running, and uncertain liveness leaves only
  `running` unknown while total and placement counts stay exact. Snapshot JSON
  gains `running` and `total`, and `live_error` reports a source's failed live
  observation.

- Top summaries drop `partial`, `updating`, and `unavailable` suffixes and the
  separate bottom status line: only an actual observation failure adds a
  compact yellow notice between the scope and the clock, the function-key bar
  keeps the bottom row, and summaries use two cells of padding with dim
  captions and cyan only on known nonzero numbers.

- The bundled model catalog refreshes to the 2026-10-18 release with 232
  providers and 2,547 models, so newly published models are selectable
  without an external catalog.

- `too start` reports only the agent endpoint, without `drivers=` and
  `lifetime=`, and `too AGENT info` omits the Lifetime and Launch rows.

- **Breaking:** agent HTTP endpoints move from `/api/v1` to `/api/v0`,
  including execution, capabilities, chores, threads, and top routes. Lifecycle
  payloads report `api_version` instead of `lifecycle_protocol`, pagination and
  history cursors carry the agent API version, so previously issued cursors are
  rejected, and statistics endpoints move from `/api/v1/stats` to
  `/api/v0/stats` with independent versioning. Update hard-coded client paths
  and upgrade the CLI and agent servers together.

- **Breaking:** the Hub wire protocol uses hub API version 0: team events,
  publication, and staging payloads carry `v: 0`, and `GET /info` reports
  `api_version`. Upgrade the Hub and its clients together, because other values
  are rejected.

- **Breaking:** persisted contracts are consolidated and advanced: agent state
  format 23, agent instance format 3, run store schema 62, and hub store schema
  4. Older data is rejected without rewriting, while derived State rebuilds
  from authored inputs. Stop agents before upgrading and delete a stale
  `AGENT_HOME/.runtime/status.json`; start from fresh
  `AGENT_HOME/.runtime/runs.db` storage
  (`AGENT_HOME/.runtime/instances/<id>/runs.db` for isolated instances), keeping
  required old runtime data separately; and select a fresh Redis/Valkey dataset
  for the Hub. Existing history, drafts, checkpoints, and cursors are not
  migrated.

- **Breaking:** execution records, HTTP payloads, and stream events use `spec`
  and `report` instead of `given` and `noted`, with `StepSpec`, `StepReport`,
  `ModelStepSpec`, `ModelStepReport`, `ToolStepSpec`, `ToolStepReport`,
  `CollectionStepReport`, and `LoopStepReport` types, together with `RunReport`,
  `ExecStepReport`, and stored `Spec` type families, and controls expose
  `request_id` instead of `request`. Step records drop `created_at`, which HTTP
  detail derives from `started_at`, and an exec Step's report stores only its
  Control reference, so read the target from `payload/runnable`. Update Pointer
  paths to `STEP/spec`, `RUN/report`, `STEP/report`, and `CONTROL/request_id`;
  schema 62 rejects older execution stores without mutation, so keep required
  old data separately and start with fresh storage.

- **Breaking:** chore scheduler checkpoint records and `chores` columns rename
  `chore_id` and `active_run_id` to `id` and `active_run`; code reading a
  `ChoreRecord` must use `id` and `active_run`, while chore lookup parameters
  and the HTTP `active_run_id` projection are unchanged. Checkpoints share the
  schema-62 execution store, so the fresh-storage requirement above applies.

- `AGENT_HOME/.runtime/stats.db` uses schema `2` with `roots`, `buckets`, and
  `state` tables; discard an older cache while the server is stopped and let it
  rebuild from compatible execution records.

### Fixed

- Pending Runs report their admission time and thread lists order by each
  thread's latest visible Run change, so recently updated work stays first.
  ([#874](https://github.com/openhat-ai/toolang/pull/874))

- Thread summaries, status filters, and CLI `steer`/`cancel` selection treat
  pending and running Runs as active, while each Run keeps its actual status.
  ([#874](https://github.com/openhat-ai/toolang/pull/874))

- `too AGENT top` scopes discovery to the selected resident's status document
  and its matching runtime registrations: stopped or unresolved temporary
  instances still registered to that resident stay in its totals, while
  unrelated agents and foreign placements, including roaming instances that
  share the same home, are excluded so their discovery errors cannot invalidate
  those totals.

- `too top` pages completed roots on dedicated workers, so a slow page no longer
  occupies live polling, and a failed supplemental history lookup preserves the
  fresh snapshot and its totals, records `page_error`, and stays retryable
  through normal PgDn paging.

- `too top` fits short terminals: it keeps the function-key bar in the last
  row, hides the column heading and its sort targets below three rows, and
  compacts embedded help and details panels. In very short terminals table
  rows and their mouse targets can be hidden; the selection is retained and
  the selected row becomes visible after expansion.

- `too top` observes a published endpoint independently of local run-store
  metadata: an unreadable store no longer blanks a reachable agent, and a
  successful HTTP sample keeps history established, so a stopped agent
  reports statistics unavailable only when its established local history
  store is missing or unreadable; readable stopped history still contributes
  exact totals.

- `too top` keeps agent membership and readable history when a Docker batch
  inspection fails, reports no snapshot-level diagnostic, and leaves the
  cause to the affected instance.

- `too top` reports no error for a newly installed inactive agent without a
  history store, which now contributes zero completions and usage without
  creating a database, for a readable usage sample older than five seconds, for
  a failed completion page, or for embedded execution without an HTTP endpoint;
  preserved totals, rows, and retry behavior are unchanged, and raw causes stay
  in the JSON diagnostics.

- `too top` treats instances reported as `not running` as zero active roots
  without live-coverage warnings or partial diagnostics, while their durable
  completions and usage stay visible; running or unknown instances retain
  unavailable and stale live diagnostics.

- Preparation progress uses sentence case for state in labels such as
  `Prepared state in 2s`, `Waiting for root state...`, and
  `Failed to prepare agent state`.

- Automatic agent runtime port selection skips a local peer whose
  `.runtime/status.json` is unreadable or written by an unsupported version,
  so starting an agent no longer fails because of another agent's runtime
  status. The selected agent's own incompatible status still fails with
  `unsupported runtime status version`.

- Unnamed entry runnables read `agic:` or `flow:` in file help, signature
  help, run headers, and chat status in place of the previous `agic:_` and
  `_ agic` labels.

- Formatting preserves template hashes such as `{{#flag}}` in program, item,
  and parameter documentation.
  ([#868](https://github.com/openhat-ai/toolang/pull/868))

- Chore properties named `tools`, `models`, `skills`, `services`, `psyches`,
  or `prompts` stop at structural `#` comments instead of including comment
  text in AST property values.
  ([#868](https://github.com/openhat-ai/toolang/pull/868))

- Non-query directive comments such as `context=none# Keep,exact,comma,,` keep
  their punctuation instead of being rewritten as CSV values.
  ([#868](https://github.com/openhat-ai/toolang/pull/868))

- Flow statement formatting keeps punctuation and internal spacing in inline
  comments, including bound and asynchronous operations, and separates them with
  the canonical two spaces.
  ([#868](https://github.com/openhat-ai/toolang/pull/868))

- Incomplete calls such as `generate 2 using` report malformed syntax instead
  of a speculative hint that an already-present `using` keyword is missing.
  ([#868](https://github.com/openhat-ai/toolang/pull/868))

- Documentation attachment measures tabs and spaces by visual columns, so mixed
  indentation no longer detaches member documentation or hides it from the
  formatter.

- `too top` reports zero active roots for embedded instances whose pinned local
  store has no unfinished physical root, while unfinished or unreadable data
  keeps counts unknown.
  ([#872](https://github.com/openhat-ai/toolang/pull/872))

- `too top` uses fixed `▵` and `▿` sort markers, so heading labels and
  numeric columns keep their positions when sorting changes.
  ([#872](https://github.com/openhat-ai/toolang/pull/872))

## [0.4.0a9] - 2026-10-18

### Added

- `too top` sorts interactively: F6 opens a picker for Default, MODEL, TOOL,
  INPUT, CACHED, OUTPUT, SPEND, and TIME+, Left/Right choose ascending or
  descending, and F6/Enter apply while Esc cancels. Clicking a heading sorts by
  that column and reverses on the next click, clicking a row selects it, and
  default order lists the most recently changed active trees first, followed by
  finished roots in completion order.

- `too top` shows a live dashboard over the selected root's local resident,
  visiting, and roaming instances, and `too AGENT top` observes one resident
  agent: placement counts, one table of active LiveTrees and root Runs
  completed within `--recent` (`1m`, `5m`, or `15m`; default `5m`), and
  `--period` rolling usage (`1h`, `4h`, or `8h`; default `1h`) of model and
  tool calls, input, cached, and output tokens, and USD spend. Readable
  sources still contribute completions and usage while their instances are
  offline, and source reads neither open nor initialize `stats.db`; unknown
  values stay `-` and stale live data freezes. `--refresh` sets the
  interactive interval in seconds (default `1`), while `--once`, `--json`,
  and non-TTY output print one snapshot. The agent API serves the same data
  at `GET /api/v1/top/snapshot` and `GET /api/v1/stats/recent`.
- Agent servers expose read-only execution statistics at
  `GET /api/v1/stats/summary`, `/api/v1/stats/models`, and `/api/v1/stats/daily`,
  scoped to the agent or one `thread_id` or `root_run_id`. Summary reports
  all-time and 365d, 30d, 7d, 24h, 4h, 1h, and 30m windows; models groups one
  window by historical model ref; and daily returns timezone-aware rows for
  contribution heatmaps. Rows report input, output, and cached tokens, USD spend
  with its approximation flag, model and tool calls, and root Runs. A background
  worker archives completed root trees older than the agent-home `[stats]`
  setting (default `archive_after_seconds = 86400`, minimum 60) into the derived
  `AGENT_HOME/.runtime/stats.db`; the first historical request can return
  `data=null` while initialization runs, and an unavailable statistics store
  returns 503 without affecting execution.

- Resident execution exposes LiveTree snapshots for active Runs:
  `GET /api/v1/runs/live` returns `LiveSnapshot(epoch, observed_at, trees)` from
  executor memory without reading history or the Store, and local and remote
  `RunClient` objects expose `await live_trees(root_run_id=None)`. Each tree
  holds parent-first Run and Step nodes with compact identity, runnable or
  operation, status, timestamps, and Run subtree call, token, and USD cost
  totals, excluding call contexts, arguments, outputs, provider metadata, and
  references. An optional `root_run_id` selects one root; unknown, completed,
  and child IDs return no trees, invalid IDs return HTTP 422, and a stopped
  executor returns 503. Treat each response as a replacement sample and use the
  event stream when every transition is needed.

- A conversation carries one optional Hub-owned `MEMBERS.md`, separate from
  workspace `AGENTS.md`: `convo.get_rules` reads the document and its revision,
  `convo.set_rules(target, content, revision)` replaces or clears it, and the
  bound source conversation is declared in the execution message and recalled as
  runtime-owned `convo-rules` before handling and before operating on another
  conversation. Agent edits require membership, a live write lease, and human
  authorization covering the change; forwarded or quoted text and the rules
  themselves cannot grant it, and a stale revision fails without mutation.
  Human `GET`/`PUT /msg/conversations/{id}/rules` and the agent equivalents
  return `{conversation, filename, revision, content}`; conflicts are HTTP 409
  `conversation_rules_conflict`, and agent sends may carry a transport-only
  `rules_revision` checked atomically with the append.

- `too convo` manages conversations as the configured human: `list` shares
  Talk's directory and `JOINED` indicator, `create [NAME]` creates a group
  containing the human, `join`/`leave` change ordinary group membership, and
  `rules <id> [-o PATH] [-u [PATH] --revision REVISION]` downloads or uploads
  the shared document. DMs and the system conversation keep fixed membership,
  leaving preserves rules and history, absent rules read as empty content with
  revision `0`, and unknown conversations and read failures exit nonzero.

- Runtime exposes `runtime.steer(target, input, expected_control)` and
  `runtime.cancel(target, expected_control, reason?)` to model calls, and
  `runtime.spawn` accepts an inspected idle local `thread` of any prefix,
  reusing its history and compaction while launch resources and workdir still
  come from the caller; `runtime.honor` also accepts `conversations`.

- Chat, Talk, and Chore resolve their handler automatically: an explicit
  invocation selection, then a user runnable named after the driver, then a
  runtime handler named after the driver, then the user's unnamed entry. A
  driver with none of these fails before the run is accepted, and only an absent
  candidate falls through, so an invalid or failing selection is reported
  instead of running a different runnable.

- Runtime ships a packaged `talk` agic that handles messaging batches by
  default: it takes precedence over an unnamed user entry and runs even when the
  agent declares no runnable, uses the receiving agent's normal resources and
  limits, carries its handling instructions in its own Program, and is recorded
  under a distinct qualified identity so inspection and restart keep the
  original source.

- Authored run requests may supply `driver` (`chat`, `talk`, or `chore`) with a
  null `runnable.ref` to select the handler automatically, and
  `POST /api/v1/runs/input-references` accepts the same `driver`; a request still
  requires a ref or a driver.

### Changed

- `too hub` simplifies its snapshot: a `hub <version>` or `hub not running`
  line sits above a headerless three-column tree of agent name, status, and
  Talk; a nonempty list ends with instance, running, and talk-enabled counts,
  an empty list prints `no agents` instead, and negative states are dim.
  Incomplete inspection marks only a nonempty summary `partial` and prints
  diagnostics after the list. ([#835](https://github.com/openhat-ai/toolang/pull/835))

- **Breaking:** terminal execution statistics are recorded directly in Noted: a
  model Step stores `usage` (input, output, cache-read, and reasoning tokens),
  `cost`, `invoked`, and `cont` without an `accounting` envelope, a tool Step
  stores `summary` and `invoked`, and each Run record gains terminal `noted`
  with its subtree `calls`, `usage`, USD `cost`, and `cost_approximate`. Step
  completion event summaries reuse `TokenUsage` and move USD cost to sibling
  `cost` and `cost_approximate` fields. Replace record reads of
  `noted.accounting` with `noted.usage` and `noted.cost`, and replace event
  reads of `noted.usage.cost` and `noted.usage.approximate` with `noted.cost`
  and `noted.cost_approximate`. Writable execution-store schema 59 rejects older
  schemas for reads and writes with no migration or compatibility decoder, so
  retain any required old runtime data separately and use fresh storage;
  subscribers must upgrade and reconnect.

- **Breaking:** runtime operation interpretation now uses the current `runtime`
  plugin and canonical `runtime__*` names for event metadata and receipts, CLI
  progress, honor reply reconstruction, and compact checkpoint validation.
  Retired `_toolang` aliases, `_toolang__execute`, and unqualified short tool
  names are no longer interpreted as runtime operations. Existing records retain
  their original names without a schema change. Use current runtime selections
  and calls, restart hosts, and prepare fresh Setup; do not resume pre-upgrade
  runs.

- **Breaking:** Run subscription events now carry compact client-owned operation metadata, direct outputs without bindings or pointers, text errors, and usage summaries. Part payloads retain text/media display data and explicit runtime receipts, but exclude provider signatures, native metadata, full tool arguments, and arbitrary result bodies. EventOutput contains final JSON; serialization does not project values again or query storage. Complete model/tool calls, Flow ASTs, State/input references, and accounting internals remain in unchanged durable records and explicit inspection/query APIs. External subscribers must upgrade with the runtime and reconnect. Run/Step IDs are scalar; control application uses Run/index facts.

- **Breaking:** thread peer metadata is removed from execution records, events,
  and HTTP schemas. Thread creation no longer accepts a peer, and integrations
  must stop sending or reading peer fields. Empty thread titles now use the
  thread origin. Peer fields are gone from storage with no upgrade path or
  compatibility decoder; follow the schema-59 fresh-storage requirement in the
  execution statistics entry above.

- **Breaking:** the public `ToolRuntime` tool protocol adds
  `steer(target, input, expected_control)` and
  `cancel(target, expected_control, reason)`, and `spawn` and `honor` gain the
  `thread` and `conversations` keyword arguments, so custom tool runtimes must
  implement the new methods.

- **Breaking:** only one pending or running independent root may occupy a thread.
  Spawning into or admitting a root into a busy thread fails without creating
  replacement work, and HTTP root admission reports it as 409; processing
  children keep their existing lifecycle.

- **Breaking:** Talk reuses one bound `talk_` thread per agent and conversation
  instead of creating a thread per batch, and admits each batch by committing its
  exact input, cursor range, and Run together before handling, so restart and
  recovery resume recorded ranges without replaying effects or rebuilding a
  changed message tail. A handler may reply, ignore, steer or cancel an inspected
  local Run, or spawn independent work; old per-batch threads are not adopted, and
  an occupied deterministic thread ID with another or no binding is rejected
  rather than remapped.

- **Breaking:** chore identities are now eight lowercase Base32 characters
  derived from the exact name and the owning agent, so one name yields a
  different ID per agent and `chore_<id>` thread names are shorter.
  `chore new` generates `chore-<8 chars>.md` names, and two names deriving the
  same ID are rejected during authoring and State acceptance. Renaming a chore
  or moving it to another agent creates a new identity, so checkpoints and
  earlier `chore_<id>` runs recorded under the old ID are not carried over;
  the `chore/name` ref is unchanged. Integrations constructing
  `AuthoredChores` must pass `agent_name=`, and `ChoreFile.id` becomes
  `id_for(agent_name)`.

- **Breaking:** conversation IDs move to messaging schema `3`: generated DM and GC
  suffixes now occupy disjoint bit domains under the shared 40-bit permutation,
  so a DM suffix can never equal a GC suffix, and GC allocation permits 524,288
  reservations per backend per hour instead of 1,048,576. The changed suffixes
  mean an initialized backend written on an older schema is rejected without
  mutation: stop writers, preserve the old runtime and dataset, and select a
  fresh Redis/Valkey dataset for schema `3`. Conversation IDs, messages,
  memberships, and talk cursors are neither migrated nor imported, while
  execution and thread IDs keep their formats.

- **Breaking:** the Talk driver no longer prepends its default handling
  instructions to a selected handler, which now receives only the JSON message
  batch. A user runnable that relied on the injected instructions must author
  them in the selected runnable; the packaged runtime handler carries them in
  its Program.

- **Breaking:** there is no global runnable default. `[default].runnable` in root
  or agent configuration, `TOOLANG_DEFAULT_RUNNABLE`, and the runtime startup
  `--default runnable=REF` option are rejected with replacement guidance; declare
  a handler named after its driver (`chat`, `talk`, or `chore`) instead, or, for
  an interactive Chat-only choice, set the Chat session entry. `--default
  runnable=REF` and `:runnable REF` now select only the current Chat session's
  entry, with reset restoring automatic Chat handler resolution; other sessions,
  Talk, and Chore are unaffected.

- **Breaking:** `too hub` now replaces the prior endpoint/status line with a
  Hub and agent instance snapshot. The Hub endpoint is omitted; scripts that
  parsed the old line must be updated, and the CLI has no machine-readable status
  format. The snapshot lists installed residents and discoverable preparing,
  starting, running, or unresolved visiting, roaming, and temporary instances,
  each with its inspected state and the `talk` driver recorded for its launch.
  Finished nonresident instances and history are excluded, and incomplete
  inspection marks the summary `partial` and prints diagnostics below the list.

- **Breaking:** `too hub` now prints the Hub version and readiness when
  run without a command, and the `too hub status` command is removed. Use
  `too hub` instead of `too hub status`; `too hub start`, `too hub serve`, and
  `too hub stop` are unchanged, and messages that pointed to `too hub status`
  now point to `too hub`.

- `too -V` and `too --version` print only the version, such as `v0.4.0a8`,
  without the preceding `toolang` CLI name.

### Removed

- **Breaking:** the execution Activity and statistics surfaces are removed:
  the earlier Activity-based `too top` and `too AGENT top` views are gone, the
  Activity HTTP endpoints of the agent API (`/api/v1/activity`) and the Hub are
  removed, the agent profile no longer reports `metrics`, and the Hub neither
  collects nor publishes agent Activity. Use `too inspect` and execution history
  instead. Opening a writable
  execution store drops the derived Activity indexes and discards the
  historical derived totals, so stop old writers sharing that execution store
  before upgrading; unrelated read-only processes and other execution stores
  are unaffected. Records, content, IDs, and cursors are preserved.

- **Breaking:** manual Run retry is removed: the `too retry` command, the
  `POST /api/v1/runs/{run_id}/retry` and `/retry/stream` endpoints, the
  `RunClient`, `LocalRunClient`, `RemoteRunClient`, and `RunExecutor` `retry()`
  methods, `RunStore.accept_retry`, and the `RunRetried` (`run_retried`) stream
  event are gone. Use `too rerun` (or `POST /api/v1/runs/{run_id}/rerun`) to
  execute a new root Run from a source Run; recorded retry controls stay
  readable for historical Runs and usable as rerun sources, but retry can no
  longer be applied. Integrations constructing `RunExecutor` must remove the
  former `load_state` argument and supply paired `setup` and `state` callbacks
  for request resolution.

- **Breaking:** the standalone `caps` command is removed: the `caps` console
  script, `python -m toolang.cli.caps`, and agent-prefixed shortcuts such as
  `caps alice list` no longer exist. Use the `too` equivalents, such as
  `too caps` for the root list and `too AGENT caps` for one agent; kind-specific
  lists (`too skill list`) and cap mutation commands are unchanged.

### Fixed

- `too top` rejects a live sample captured under a previous source binding,
  so a rediscovered or departed instance cannot restore obsolete live rows,
  and completed counts and usage become unknown (`-`) when a current
  instance has no readable source instead of under-reporting.

- `too top` and `too hub` omit visiting or roaming instances that are no
  longer running, so a departed temporary instance is not reported as
  current.

- `too top` counts active roots only from current inventory and keeps retained
  departed instances to completions and usage, so they add no live rows or
  live-coverage warnings and cannot make active counts unknown. USD spend uses
  four decimals without an approximation marker, instance labels omit
  `#runtime_instance`, active roots carry `+` instead of `[+]`, and interactive
  output fills the terminal with a green heading, deep cyan selection and cyan
  status and function-key bars, normal-weight summaries, dim finished roots, and
  a hidden cursor, and publishes `top team` or `top AGENT` terminal titles,
  clearing them on exit.

- Cancellation while `stop()` drains owned tasks still runs terminal fallback and
  monitor/observer cleanup before propagating; cancellation while draining
  observers also releases readers. Rejected control-monitor task creation closes
  its coroutine, and observers canceled before their first turn release their
  subscriptions. Runtime spawn errors after admission preserve the completed Step
  receipt and record the later error on the Run instead of creating an invalid
  `ErrorRef`.

- Root and spawned Runs share startup-failure finalization: a synchronous
  startup error marks the Run failed, closes pending controls, publishes exactly
  one terminal event, then re-raises the original error. Rejected task creation
  releases its coroutine and subscriptions; owned-task initialization failures
  use ordinary cleanup, and observers drain after publication. Talk preserves
  durable receipts, observing pending or running receipts without false failure
  notices or execution-state repair.

- Run event producers close nested Steps and owned child Runs before their parents. Run stream clients preserve producer order and report a protocol error when a Run ends with an active owned child, rather than delaying or reordering terminal events; asynchronous launch admission and owned-child cleanup semantics are unchanged.

- Large run and thread detail responses avoid expensive diagnostics from probing
  unrelated `StepGiven` variants, while preserving existing public payloads and
  stored facts; Chat title queries request existing thread metadata with
  `limit=0`.

- A tool call intercepted by workspace rules stays in the same history unit as
  its reply, so assembled history no longer leaves an assistant tool call
  without its result.

### Security

- The MCP dependency used by the service toolset is updated to 1.30.0 to address
  CVE-2026-59951. ([#825](https://github.com/openhat-ai/toolang/pull/825))

## [0.4.0a8] - 2026-10-15

### Added

- A visiting server started implicitly by Chat, retry/rerun, fork, or rewind
  is automatic: it keeps running while a client is attached or accepted work
  remains, then stops itself, leaving its history and logs on host mounts while
  Docker removes the automatic container on exit. Fork or rewind with `--chat`
  keeps one server acquisition across both phases instead of finishing and
  reacquiring between them. Accepted work, independent spawns, and pending
  terminal cleanup also retain the server with no client attached.

- Automatic clients retain their server with a lease renewed every five seconds
  under a fifteen-second timeout, so a killed client's lease expiry makes an
  idle server eligible to exit while accepted work can still retain it, and
  startup is held for thirty seconds until the first client registers. Losing
  the lease ends the command with an error warning that accepted runs may still
  be running, while losing transport never cancels accepted work.

- `too start` and `too stop` accept visiting targets as well as resident agents.
  `start` promotes a running automatic visiting server to persistent in place,
  keeping its generation, endpoint, and drivers with no second fetch of the
  source, and reports `drivers=... lifetime=persistent`; `stop` ends the current
  generation and its active work. `too info` lists the running server's
  `Lifetime` and `Launch`.

- The agent API adds `PUT` and `DELETE /api/v1/runtime/leases/{client_id}` and
  `POST /api/v1/runtime/promote` for retention and promotion, reports
  `launch_id` and `lifetime` in the profile runtime, and accepts an optional
  `expected_control` on run cancel and steer requests so a control aimed at a
  replaced invocation is rejected instead of reaching a retry.

- A cached remote `agent.too` now keeps an `agent.source.json` receipt beside it
  recording the declared ref, the resolved ref, the entry path, and the SHA-256
  of the Program bytes. Reuse requires receipt and file to agree, so a cache
  written by an older version is fetched again, and the recorded provenance
  travels with the Program into its prepared State module.

- Remote agents and caps accept explicit Source Refs: `gh:owner/name`,
  `github:owner/name`, `gh:owner/repo/name`, and complete HTTP(S) URLs,
  alongside the existing `github://owner/repo/path@rev` form. A `gh:`/`github:`
  ref that carries `@rev` normalizes to one exact `github://` ref without
  repository or path probes. HTTP(S) sources fetch file-backed caps directly:
  a skill from its directory URL or `SKILL.md` URL fetches only `SKILL.md`,
  while GitHub skill sources still include the directory's files. ([#812](https://github.com/openhat-ai/toolang/pull/812))

- Managed agent startup prepares State locally before entering or creating a
  workload, so a preparation failure reports its cause and creates no workload.
  Required work appears on stderr as dim `<ref> fetching`, `parsing`,
  `resolving`, or `updating` rows that reuse one slot across stages and vanish
  as work completes, then a single dim summary such as `Prepared agent and 4
  caps in 1.4s` replaces the region; cached work stays silent with no rows or
  summary, no spinner or running counter appears, and plain output shows
  operation transitions plus that summary without terminal controls. A remote
  `.too` agent is fetched and parsed before its dependencies are prepared, and
  an unparsable agent blocks them.

### Changed

- CLI help is now consistent across built-in and generated commands: command and
  help headings use sentence case, argument labels are uppercased, summaries are
  reworded and shortened, runnable usage shows one `[ARGUMENTS]` group instead of
  `[NAME=VALUE...] [-- <INPUT> | -]`, commands list only their supported field
  sections after Options, and examples render as compact command/description rows.
  Invocation, parsing, and exit codes are unchanged.

- **Breaking:** a server started implicitly by Chat, retry/rerun, fork, or
  rewind now enables only the chat driver for resident and visiting agents, so
  Chat with a resident agent no longer starts the talk and chore drivers.
  Roaming and standalone script behavior is unchanged and deferred. Run
  `too start <agent>` before Chat when background talk or chore work must
  continue unattended; `start` and `serve` keep their placement driver
  defaults, `start` never adds drivers to a running server, and changing a
  running server's drivers or startup configuration requires stopping and
  restarting it.

- **Breaking:** steering or cancelling a resident or visiting run now attaches
  only to the healthy live owner that accepted it and pins that invocation, so
  a stale history can neither start a replacement server nor redirect a control
  to a retry: the server rejects the control with HTTP 409 and the run keeps
  running. Servers started by earlier versions are unmanaged, so stop and
  restart them before `too start` can promote one or steer and cancel can use
  guarded controls.

- `too start` on an agent that is already running attaches to its server
  instead of starting a duplicate, and verifies what it can: a differing
  `--port`, or startup options it cannot verify on attachment, fail with
  stop-and-restart guidance, a foreground `serve` cannot be detached, and the
  command reports the drivers the server actually runs.

- **Breaking:** State revisions move to layer schema 22 and composition schema
  3: accepted bytes are stored under `source/` and fetched bytes under
  `materialized/<acquisition-key>/` instead of `files/`, and `layer.json`
  holds parsed config, Program values, Cap `meta` and `body`, chore values,
  exact file sizes and digests, one `resolution` per fetched item, and a
  `created_at` time included in the revision hash. Older layer and composition
  schemas are unsupported, so current caches rebuild from authored inputs on
  the next preparation; existing revision directories stay unread and
  unmodified, and no manual migration is required.

- GitHub agent and cap sources are pinned to an exact upstream version:
  selectors resolve to a full commit SHA before any file is fetched, so the
  recorded resolution names that commit and a moved upstream commit counts as
  new provenance even when the bytes match, while HTTP(S) sources keep their
  canonical URL and compare fetched bytes on refresh. Preparation also
  rechecks recorded selections against current source policy, so a
  configuration change that invalidates a remote selection rebuilds State
  even when authored files are unchanged.

- **Breaking:** remote agent and cap sources must be explicit Source Refs:
  unprefixed `owner/name`, `owner/repo/name`, and `host/name` shorthand and the
  `oh:` and `openhat:` prefixes are rejected before any network access. Prefix
  former implicit GitHub refs with `gh:` (or `github:`) and replace domain/path
  shorthand with a complete HTTP(S) URL. Cap Ref queries such as
  `skill/reviewer` and local sources are unaffected. ([#812](https://github.com/openhat-ai/toolang/pull/812))

- **Breaking:** Agent Names are validated as 1–64 lowercase ASCII letters,
  digits, or underscores starting with a letter. Explicit names (`too new NAME`,
  `too clone SOURCE NAME`, and `agent:NAME`) are rejected unchanged when
  invalid, while filename-derived names are normalized: lowercase, runs outside
  `[a-z0-9_]` become `_`, surrounding `_` is trimmed, and a leading digit gains
  a `script_` prefix, so `Daily-Report.v2.too` roams as `daily_report_v2` and
  keeps its filename. A home named with dots, hyphens, uppercase letters, or
  more than 64 characters is no longer discovered or addressable, so stop its
  workload with the previous Toolang version, then rename the home and its
  paths manually; State and histories are not moved. ([#812](https://github.com/openhat-ai/toolang/pull/812))

- **Breaking:** Cap records expose `cap` (`kind/name`) and `source` (the
  resolved Source Ref) instead of `ref`, and cap lists show a `CAP` column
  instead of `REF`; `location` remains the human-facing content address.
  Migrate explicit query keys from `ref` to `cap` and API Cap response `ref` to
  `source`; configured cap write requests still use `ref` for the supplied
  Source Ref. ([#812](https://github.com/openhat-ai/toolang/pull/812))

- The directory-wide native guard over discovery references is renamed from
  `instances/update.lock` to `instances/register.lock`, still protecting only
  reference registration, replacement, and conditional removal. Stop all old
  host and guest processes sharing the Toolang root before upgrading, then
  restart. Old guards are left untouched and can be removed only offline.

- **Breaking:** the implicit `lab` workspace is now backed by
  `<agent home>/.lab` instead of `<agent home>/lab`; its `lab://` name is
  unchanged, but existing data in the old directory is neither migrated nor
  used. Move any data you still need into `.lab` and update absolute paths that
  point to the old directory.

- A temporary execution's runtime metadata is retired once its workload has
  stopped and its resources are released, so a finished temporary run leaves no
  leftover entry in its agent's runtime record while its execution history stays
  in the run store and logs; a persistent agent keeps its record across stop,
  and an interrupted instance or unreleased resource reference stays for
  recovery.

- Host foreground `serve` now executes the agent in one process: the command
  initializes the control record and then runs the workload itself instead of
  launching a separate child agent process, so the running agent is the
  foreground process rather than a child of it.

- **Breaking:** an agent's runtime record is consolidated into one
  `agents/<agent>/.runtime/status.json` of version 2, holding an `instances`
  map whose entries have a `control` section (launch intent, recovery
  references, startup bindings, and the last successful endpoint and sandbox)
  and a `workload` section (PID, process creation time, lifecycle state, and
  errors). The authoritative `.sandbox/<agent>/state.json` and the per-instance
  status files are gone; every writer merges only its owned section after
  checking the launch ID, under one transient `status.json.lock` claim
  published by an atomic hard link because kernel locks alone did not
  coordinate a host and a Docker Desktop guest on a shared mount, so an
  old workload or its cleanup cannot change a newer launch. A leftover
  `.sandbox/<agent>/state.json` or
  `.runtime/sandbox.json` blocks start, stop, and removal with a migration
  error, and a legacy `status.json` that still reports a running, starting, or
  preparing workload is not adopted, so stop the workload with the previous
  Toolang version before upgrading; a stopped legacy report migrates with its
  remembered endpoint and sandbox.

- **Breaking:** the observation root is removed: `TOOLANG_OBSERVATION_ROOT` is
  no longer read, `TOOLANG_ROOT` names the Toolang root rather than an agent's
  own root, and the agent root travels with the workload through a hidden
  `--agent-root` argument. Instance discovery records under
  `<root>/.runtime/instances/` are now named `gus.json` for residents,
  `lex@v<hash>.json` for visitors, and `aid@r<hash>.json` for roaming servers,
  where `<hash>` is the first 16 hex characters of SHA-256 over the canonical
  Toolang root, a NUL, and the source selector for visiting, and over the
  canonical host absolute source path for roaming; each record carries
  `version`, the absolute status path, and the launch ID, all references share
  one `instances/register.lock`, and are read through that record, and legacy
  registrations are never adopted; a visiting agent is identified per Toolang
  root as well as source, so the same remote source materializes once per root.
  Stop all old host and guest processes sharing the root before upgrading, and
  do not delete a live guard.

- A stopped agent remembers its last successful launch: `--sandbox` now defaults
  to the agent's configured binding, then its last successful selection, then
  `host`, while an available remembered API port is reused, so a restarted agent
  usually keeps its sandbox and endpoint. These settings are recorded only after
  a launch reaches readiness, so a failed attempt no longer replaces them.

- `too init DIR` now creates `aid.too` instead of `aide.too`, and the
  repository's own root helper is renamed from `aide.too` to `aid.too`; its
  success message, the `too run` help example, and the documented examples
  use the new name. Automation that invokes the repository helper must change
  `too aide.too ...` to `too aid.too ...`; only existing user-created scripts
  retain their names and keep running unchanged.

- **Breaking:** authored edit locks are unified into one persistent guard per
  source scope and the derived State guards are renamed: `agent.too.lock`,
  `runnables.lock`, `caps.lock`, `chores.lock`, `config.toml.lock`, and
  `toolang.toml.lock` become `.state/root/edit.lock` for shared root sources
  and `agents/<agent>/.state/home/edit.lock` for one agent's sources, while
  each layer's `prepare.lock` and the agent's `check.lock` become `build.lock`
  and `publish.lock`. A mutation attempt creates its scope's edit lock before
  its digest or existence checks, but reads never create one: `state`
  `list`/`get` and other readers acquire an existing guard, or read
  optimistically and retry under the guard when a writer creates it during
  that read, and an unchanged roaming projection also leaves the lock absent.
  Legacy sidecars are neither used nor modified, and mixed versions cannot
  coordinate across the renamed paths, so stop all host and guest processes
  sharing the root, upgrade together, then restart.

- **Breaking:** `AuthoredCaps`, `AuthoredChores`, `ConfiguredCaps`, and
  `ConfiguredWorkspaces` now require an explicit `lock_path=`: use
  `layout.root_edit_lock` for root-owned sources, including a shared roaming
  `toolang.toml`, and `layout.home_edit_lock` for one agent's sources.

- Root command descriptions are revised: `workspace` reads `Configure agent
  workspaces`, `chore` reads `Create or schedule tasks`, `talk` reads `Talk
  with agents through the hub`, and `hub` reads `Control the teaming service`.

### Removed

- **Breaking:** the `--title-prefix TEXT` option is removed from Chat, Talk,
  Top, Serve, Script Run (including generated script group and runnable
  commands), Retry, and Rerun; title prefixes now resolve per type from
  `TOOLANG_TITLE_PREFIX_<TYPE>` over agent-home config over root config over an
  empty default. Replace a one-off flag with an environment assignment such as
  `TOOLANG_TITLE_PREFIX_CHAT='[too] ' too alice chat`; existing
  `[terminal.title.prefix]` settings keep working unchanged.

### Fixed

- The `too top` dashboard now shuts down reliably when it is quit while a
  Stats-window or filter change is replacing its activity subscription: the
  outgoing subscription's cleanup is no longer cancelled a second time.

- Remote Mod targets are rejected unless their actual source path is a `.too`
  program: an HTTP(S) URL path is decoded exactly once during parsing while a
  canonical `github://` path is used literally, so a GitHub URL ending in
  `report%252Etoo` or a literal `github://.../report%2Etoo` ref no longer
  passes as `report.too`, while a normally encoded `report%2Etoo` URL still
  resolves. ([#812](https://github.com/openhat-ai/toolang/pull/812))

- Remote caps hosted on ordinary HTTP Markdown servers no longer fail to
  fetch with `406 Not Acceptable`: URL requests send only a `User-Agent`
  header instead of GitHub-only `Accept` and `X-GitHub-Api-Version`
  headers. ([#812](https://github.com/openhat-ai/toolang/pull/812))

- Concurrent starts of the same roaming script no longer mistake a link
  published by the other caller for an authored file: the roaming program
  link's file type comes from one `lstat` observation, while atomic link
  creation still rejects a different source owning the normalized home.
  ([#812](https://github.com/openhat-ai/toolang/pull/812))

- Automatic agent port selection and last-port reuse skip unsupported legacy
  agent directories such as `old.name`, so an invalid peer no longer blocks
  a valid agent from selecting or reusing its port, and valid peers'
  reservations are preserved.
  ([#812](https://github.com/openhat-ai/toolang/pull/812))

- Read-only roaming queries reject a home owned by another source, while
  current State caches must contain supported Source Refs so explicit-ref
  requirements cannot be bypassed and explicitly loaded historical revisions
  stay readable, and GitHub web and raw URLs decode percent-escaped paths
  exactly once before building raw fetch URLs. ([#812](https://github.com/openhat-ai/toolang/pull/812))

- `too skill add` reports the underlying Source Ref error for an unsupported or
  unresolved source instead of only a generic not-found message. ([#812](https://github.com/openhat-ai/toolang/pull/812))

- Cap inspection, publication after saved edits, offline `info`, and local
  scripts now use the same parallel preparation rows and dim summary as managed
  startup. Summaries name the agent or affected cap count, cached work stays
  silent, and preparation completes before command records, Setup inspection,
  or run progress are displayed.

- Existing reference cleanup creates a missing `register.lock`, including when
  only the old `update.lock` remains, so stopped references are removed; pure
  reads and cleanup of an absent reference create no guard. (#808)

- Visiting roots shorten only the readable label to fit a 255-byte filesystem
  component, preserving usable long names, the full logical agent name, and the
  complete 16-character source hash. (#808)

- Visiting agents under `/tmp/toolang-<agent>-<hash:16>/` now use the complete
  16-character source hash already used by their discovery references instead of
  truncating it to eight, while the source key and reference filenames stay
  unchanged. Because this selects a different root, stop old visiting workloads
  before upgrading and restart into the new root; old eight-character roots are
  left untouched for explicit offline history migration, with no automatic move
  or deletion.

- Runtime inspection skips invalid discovery references, status instance maps,
  and entry shapes while keeping valid peers visible. Non-object references
  also no longer break reference cleanup.

- Root `too top --once` now reports local runtime activity when no Hub is
  running instead of failing, so team status can be inspected without the
  teaming service.

- A temporary guest left behind by a caller that is confirmed dead is stopped
  and released by the next `start`, `serve`, `stop`, or removal action, while a
  caller that is still alive or cannot be verified, and a detached persistent
  server, are left untouched.

- A workload that fails during startup or cleanup records a diagnostic even
  when the failure happens before the API starts, so a failed start reports its
  reason instead of leaving a stale or missing state; a cleanup failure is
  appended to the recorded startup error rather than replacing it.

- Removing a stopped temporary run resolves its discovery reference from the
  bindings saved when it registered rather than its current execution settings,
  so cleanup uses the original root and paths.

- Runtime inspection reads each agent's runtime record once and derives both
  the resource reference and the workload report from that one snapshot, so a
  concurrent update cannot mix before and after views.

- Repeated short-lived script executions no longer leave one `.json.lock`
  beside every instance discovery reference: references share one
  `instances/register.lock`, so a directory keeps a single lock instead of one
  per instance. Orphan locks left by earlier versions are not removed
  automatically, so stop those processes before deleting them.

- A launch killed before its sandbox reference was bound no longer blocks later
  commands: the next `start`, `serve`, `stop`, or removal action recovers the
  workload by its launch ID, adopting a host child that is still running or
  retiring an intent whose workload never started, while a still-pending Docker
  creation helper keeps its missing container from being read as dead, so a
  container that is still being created is never stopped.

- Runtime inspection no longer reports an unverifiable workload as stopped:
  `too list` shows `unknown` when a process or container identity cannot be
  confirmed, a definitively dead workload is corrected once without clearing
  its recovery references, and live or unknown workloads are left untouched.
  Docker container states are read in one bounded batch with a two-second
  timeout, and `too list` inspects agent layouts concurrently.

- Concurrent chore mutations no longer interleave: the API patch holds the
  agent's edit guard across its whole read/modify/write, so two patches to
  different fields of one chore are both retained; the CLI edit reads the
  original before opening the editor and rechecks the winning definition under
  the edit guard only when saving, and the CLI delete holds that guard across
  its winner check and removal.

- Chore inspection and the edit/delete winner check read inline declarations and
  home Markdown files in one guarded snapshot, so a read no longer mixes
  before/after states or drops a malformed definition's diagnostic.

- Roaming configuration projection rechecks its root sources after releasing the
  home guard and retries changed inputs for up to three projection attempts in
  total, so a delayed invocation no longer leaves `config.toml` or `catalog.json`
  behind a newer edit, and repeated changes fail explicitly instead of silently
  accepting stale output.

## [0.4.0a7] - 2026-10-14

### Changed

- **Breaking:** the default Hub port changes from `7000` to `7001`, and resident
  agent ports are allocated from `7002`–`7999` instead of `7001`–`7999`, so an
  agent recorded on `7001` gets a new port on its next default start. Explicit
  `--port`, `TOOLANG_HUB_PORT`/`TOOLANG_AGENT_PORT`, and `teaming.hub.port`
  values still take precedence, and an explicit Hub port of `7000` keeps using
  `7000`. Stop an agent on `7001` and restart it without an explicit port
  before restarting the Hub on its new default; running processes and saved
  configuration are not migrated automatically.

- **Breaking:** `state.sync` reports one acceptance receipt per source: each
  `files` entry has `scope`, `key`, `observed_digest`, and `accepted_digest`,
  excluded sources add flat `code`, `message`, and optional `line`, and the
  `differences` and `diagnostics` fields are gone, so a fatal failure returns
  `{error, message, revision, files: []}`. The tool summary appends the
  excluded source count, and successful publication no longer means every
  file was accepted, so clients that read the removed fields must use the
  source records.

- **Breaking:** Agent State publication excludes invalid resources
  independently instead of rejecting the whole candidate: an invalid cap,
  chore, or Program unit is skipped with a diagnostic on its source record
  while valid siblings still publish, root/home precedence applies to
  accepted definitions only, and no previous definition is reused, so repair
  a rejected resource to restore it. Home `chores/*.md` definitions are now
  part of Agent State, while root-level `chores/*.md` definitions are
  ignored: move them into the agent home's `chores/` directory to keep them
  listed and scheduled.

- `too AGENT chore list` and the chores API report a malformed home
  definition with its `source_error` instead of failing the listing, and
  `too AGENT chore delete` removes the referenced file and prints its name,
  so a malformed definition can be deleted for repair.

- **Breaking:** ID allocation state moves from each agent's `.runtime/ids.json`
  file into a `seqs` table in `runs.db`, serialized by SQLite instead of a file
  lock, and legacy counters are not imported: a writable open rejects an
  adjacent legacy `ids.json` and any store below schema 57, so stop all
  writers, preserve the old runtime, and start with fresh runtime data while
  supported older stores stay readable read-only. `allocate_id()` and
  `IdIssuer` now take the initialized `runs.db` path as `db_path`, and
  `AllocatorSnapshot` and `AgentLayout.id_state` are removed, so integrations
  that pass a state file or read the snapshot must move to the database path.

- Chat's queue summary now shows only a dim `N queued` count, right-aligned
  with two cells of right padding, instead of a centered count that restyled on
  focus and a trailing `tab to focus`, `space to expand`, or
  `space to collapse` hint; the focused selected entry shows compact
  `(m-enter)steer (e)dit (d)elete` hints instead of
  `m-enter steer · e edit · d delete`, and narrow terminals reduce right
  padding before truncating the count.

- **Breaking:** agent talk progress moves from local per-runtime
  `.runtime/channels/messaging/v2-<backend-identity>.json` checkpoint files to
  the teaming backend, stored per agent and conversation with no TTL, so a
  restart or relocation resumes from the backend cursor and agents create no
  channel files or directories. Local files are ignored without import or
  cleanup, and the team member key is renamed from `P:team` to `P:team:members`
  without migration, so development backend data must be reinitialized.

- **Breaking:** talk handler batches no longer carry previous messages, handling
  results, or reply receipts; they contain only the current messages and existing
  conversation metadata. Custom `talk` runnables that read the removed
  `previous` field must switch to explicit history lookup with `convo/peek`.

- **Breaking:** canonical observers and HTTP run streams receive model
  `StepBegin.given` with only `model`, `setup`, and optional `state`
  revisions. The complete normalized call is no longer included, reconnection
  snapshots carry the same metadata, and durable execution records keep the
  full call, so clients that read `given.call` from observation events must
  reconstruct it with `too AGENT inspect STEP call --json` and upgrade clients
  and runtimes together. The event decoder still accepts earlier events that
  contain a complete call.

- **Breaking:** the root Hub's generated files move from `.runtime/` to a `hub/`
  directory beside it: `.runtime/hub.json`, `.runtime/hub.lock`, and
  `.runtime/hub.log` become `hub/hub.json`, `hub/hub.lock`, and `hub/hub.log`.
  Discovery reads only the new record and ignores a legacy one, and the moved
  lock means an old and a new Hub no longer coordinate, so stop a running Hub
  before upgrading.

- **Breaking:** Chat and Talk input history uses new paths and no longer
  imports earlier files: Chat reads `<agent-runtime>/chat-input.jsonl`
  instead of `<agent-runtime>/chat-history.jsonl`, staying isolated per agent
  and runtime instance, and human Talk reads
  `<root>/human/talk-input/<conversation-id>.jsonl` instead of
  `<root>/.runtime/talk-history/<conversation-id>.jsonl`. Old files are left
  untouched and are no longer read, so copy them to the new paths to keep
  earlier recall. Talk also stops saving and restoring unsent composer drafts,
  so unsent text is lost when its process exits.

- Remote run and chat streams log a redacted diagnostic line when a stream is
  interrupted, resumed, or a terminal detail fails: phase, run ID, error type,
  and HTTP status, never response bodies, endpoints, or payloads.

- Server-sent event streams now use the framework's idle ping (`: ping`) rather
  than manually yielding `: keep-alive` comments, so the runtime and Hub APIs
  emit a frame only for real events or checkpoints.

### Fixed

- Talk now leaves one blank line before its startup panel and two before the
  first message, matching Chat's startup spacing.

- Conversation metadata results from `convo/peek`, `convo/create`, and
  `convo/rename` return `participants` as a JSON array instead of a tuple, so
  their completed tool steps round-trip through the run store and a repeated
  step-end delivery no longer fails with `conflicting step_end event`.

- Cap authoring no longer requires valid content: the cap `edit` and `delete`
  commands and the caps API delete act on the raw authored file, so an excluded
  cap can be repaired or removed, and `too AGENT chore edit` opens a malformed
  home definition the same way.

- The State watcher re-checks while a tracked source cannot be read, so a
  temporarily unreadable definition is published once it becomes readable
  again instead of waiting for another change.

- Message-handler failure notices no longer trigger another handler,
  including on other agents in the same conversation, so a failing batch
  cannot cascade failure replies between agents; the notice stays visible
  and attached to the triggering message. The notice carries a
  `handler_error` marker that older Hub validators and Talk message decoders
  reject, so upgrade the Hub, receiving agents, and Talk clients together.

- Message handler failures are no longer silent: the agent posts a reply
  `Message handling failed: <detail>` attached to the triggering message,
  quoting the failed handler run's error detail.

- Message handling no longer fails with `run requires a model` and skips the
  batch when the agent has no configured default model: the handler run now
  resolves its own effective models, so an authored `models` selection still
  applies, the first available model is the fallback, and model-free
  flows that explicitly select no models remain unbound.

- Tool call summaries and tool errors keep one content cell at very narrow
  widths, dropping the leading marker and its indentation when fewer than
  three content cells remain instead of showing only the marker.

- Tool call summaries and tool errors in execution progress now end two
  cells before the available progress width's right edge, matching execution
  footers and other progress rows, while narrow parallel-lane rows keep
  their full width.

- Remote run and chat streams ignore a comment-only keep-alive that follows an
  identified event instead of failing with `remote run stream returned invalid
  event JSON` or advancing the committed cursor.

## [0.4.0a6] - 2026-10-12

### Added

- Talk's group status line shows online and total members, such as `#dev(2/3)`:
  human members always count as online and agents count while they hold a live
  Hub lease, presence refreshes independently of messages from Hub team
  deadlines and presence notifications with a half-second fallback and a
  one-second lookup timeout, agent presence starts unknown and returns to
  unknown after lookup failures as `#dev(?/3)`, empty groups show `(0/0)`,
  human-only groups show `(m/m)`, and message following and sends continue.

- `too talk all` opens the system group conversation, and the Hub resolve
  endpoint accepts `kind="system"` with `target="all"`, using the stored
  system GC mapping so the shortcut survives a rename or a name collision.

### Changed

- **Breaking:** sidecar lock files are renamed to an unprefixed name beside the
  protected object: `.config.toml.lock`, `.agent.too.lock`, `.caps.lock`,
  `.chores.lock`, and `.runnables.lock` become `config.toml.lock`,
  `agent.too.lock`, `caps.lock`, `chores.lock`, and `runnables.lock`, the
  runtime `status.lock` and `state.lock` become `status.json.lock` and
  `state.json.lock`, and thread compaction uses `<thread-id>.compact.lock`;
  `runs.db.threads.lock` is gone because thread fork and rewind now run inside
  the store's write transaction. Mixed versions do not coordinate where names
  change, so stop every writer sharing the storage, upgrade together, then
  restart; remove the old sidecars only while the whole scope is offline.

- Chat and Talk input history move to per-runtime and per-conversation files:
  Chat reads `<agent-runtime>/chat-history.jsonl` instead of
  `chat-input-history.jsonl`, and human Talk reads
  `<root>/.runtime/talk-history/<conversation-id>.jsonl` and
  `<conversation-id>.draft.txt` instead of
  `talk-v2/<identity>/<conversation-hash>/`. On first use an absent destination
  imports the legacy file and leaves the source untouched, so recall history
  and drafts carry over once, and humans sharing a root and conversation ID now
  share one history file.

- Incoming message batches now create a fresh `talk` thread per handled batch
  instead of reusing a shared execution thread; each batch keeps the transport's
  per-conversation checkpoint context, but the shared thread's earlier model
  history is no longer inherited.

- Talk target parsing reserves `all` for the system group conversation, so
  `too talk all` no longer opens a direct conversation with an agent named
  `all`: use that agent's canonical `dm_...` ID. A target beginning with `dm_`
  or `gc_` must now be a valid conversation ID and never falls back to agent
  lookup.

- `too hub status` now appends the Hub process's Toolang source version after
  its endpoint, for example `Hub running: http://127.0.0.1:7000, v0.4.0a5`,
  read from the Hub's `/info` metadata so it reports the server's version
  even when the invoking CLI differs; a stopped Hub still prints only
  `Hub stopped`, and missing, rejected, malformed, or timed-out metadata
  prints `unknown` without changing the readiness state or exit code.

### Fixed

- Runtime registration and conditional stop now share `status.json.lock`,
  preventing an old stop from overwriting a concurrently registered runtime
  report.

## [0.4.0a5] - 2026-10-12

### Changed

- **Breaking:** independent spawned work now uses `work` instead of `worker`:
  flow and agic `spawn` issue `work_` thread IDs, persist `origin="work"`, and
  publish that origin in thread events; `ThreadPrefix.WORKER` becomes
  `ThreadPrefix.WORK`, and the profile metric key `threads.worker` becomes
  `threads.work`. There are no old-prefix aliases, compatibility conversion, or
  data migration, and historical record IDs are not rewritten, so integrations
  that read the old prefix, origin, enum member, or metric key must switch to
  the new names.

- **Breaking:** built-in toolsets are renamed: `me` becomes `state`, `msg`
  becomes `convo`, and `_toolang` becomes `runtime`. Refs, queries, and
  configuration use `state/get` and `convo/write`, model calls use
  `state__get` and `convo__write`, and human labels use `ToolRef.display_name`
  as `state.get`. The `runtime` plugin and toolset name is reserved for
  Toolang, so external plugins cannot claim it, and the public
  `MsgToolContext` is renamed `ConvoToolContext`. No old name is aliased:
  update authored selections, configuration, and tool calls, then restart
  hosts and prepare a fresh Setup. Saved exchanges remain readable.

- **Breaking:** the conversation tools are renamed and reshaped: `msg/targets`
  becomes `convo/team` and `convo/list`, `msg/send` becomes `convo/write`,
  `msg/resolve` is removed in favor of `convo/list` and the new `convo/peek`,
  and `create`, `rename`, `join`, and `leave` drop their `_conversation`
  suffix. `send` renames `body` to `message` and `in_reply_to` to `reply_to`,
  and an agent target is a bare name (`alice`, not `agent:alice`). Directories
  return `{id, name, members}` and `{id, name, online}`, messages expose only
  `{id, sender, message, reply_to}`, and `rename` returns `previous_name`, so
  consumers must map the renamed fields and drop the removed backend receipt
  fields. Human and pair targets require an explicit conversation ID from
  `convo/list` or `convo/create`.

- A dot or slash model call is rejected with a message naming the exact
  advertised counterpart, such as `Unknown tool name 'state.get'; call
  'state__get' instead.`; old, runtime-only, and unavailable names are never
  matched, aliased, or retried.

- Tool progress shows a separate `[toolset]` prefix beside each tool's
  lifecycle summary, for example `[shell] Running: ls`: every built-in tool
  describes its own running, succeeded, and failed states, cancellation reuses
  that action text, and a plugin tool without its own summary falls back to
  `Executing <leaf>: <first argument>` using schema order, then input order,
  with masking and the 80-character preview limit.

- A runnable module filename may not be `_`, the root's unnamed-entry alias:
  `runnables/_.too` is rejected with `invalid-runnable-filename` instead of
  exporting a runnable that shadows the unnamed entry.

- **Breaking:** home program modules move from `flows/<name>.too` to
  `runnables/<name>.too`. Modules are now identified as `runnables::<name>`,
  as in the private reference `runnables::talk::agic:helper`, while public
  references keep `name`, `agic:name`, and `flow:name`. There is no
  compatibility discovery or automatic migration: move each `flows/*.too` file
  into `runnables/`; leftover `flows/` files are ignored. `flows/`, `agics/`,
  other home-level `.too` files, nested files, and Toolang-root modules are
  ignored.

- Each module exports exactly one unnamed or filename-matching agic or flow, so
  `runnables/talk.too` can share an `agic talk` handler that messaging selects,
  and root flow statements, agic routes, and messaging can call either exported
  kind. A named entry must be renamed with its file, and two candidates,
  including an unnamed entry beside a filename-matching declaration, are
  ambiguous even across kinds. A public name already exported by `agent.too` or
  another module is rejected by a diagnostic naming both sources, and
  nonportable or case-folded filenames and invalid exports report the
  `runnable-extension` layer with the `invalid-runnable-filename` and
  `invalid-runnable-export` codes.

- Home authoring tools accept `runnables/*.too` and reject `flows/*.too`,
  writing through `.runnables.lock` instead of `.flows.lock`.

### Fixed

- Teaming backend calls now propagate a cancellation that arrives while a
  Redis/Valkey command is in flight, so a cancelled or timed-out Hub
  operation stops instead of completing and silently dropping the
  cancellation.

- Message handling records retain successful send receipts from the handler and
  its owned child Runs, including async children and replies delivered before
  cancellation, so replies sent by a handler's delegated child runs stay in the
  context of later batches instead of being dropped.

## [0.4.0a4] - 2026-10-11

### Added

- Execution drivers decide what a running agent serves: resident agents enable
  `chat`, `talk`, and `chore`, visiting agents enable chat and talk, and roaming
  agents and standalone scripts enable none. `too serve`, `too AGENT start`, and
  `too AGENT chat` accept `--enable chat,talk,chore` or `--enable none`, so
  `serve --enable none` is valid; only `too AGENT chat` requires chat, and
  against a running agent an omitted selection preserves its drivers while an
  explicit selection that mismatches them or omits chat fails with restart
  guidance without changing the agent, and profile, status, and startup logs
  report the enabled drivers. Interactive submission, retry/rerun, and steer use
  routes under `/api/v1/chat/runs`, with interactive contexts created at
  `POST /api/v1/threads`, both requiring the chat driver and answering
  `409 driver_disabled` otherwise, while noninteractive scripts keep
  `/api/v1/runs` and create contexts at `POST /api/v1/runs/threads`.

- The Hub serves a root-scoped presence roster at `GET /activity/roster`
  (`agent:` IDs mapped to `online` or `offline` without reading activity
  stores) and accepts `GET /activity?agent=agent:alice`, returning one roster
  agent's current pages without the aggregate cache and an empty list for an
  absent or foreign-root agent.

- Foreground interactive commands publish OSC 0 terminal titles: Chat
  `<agent>: <thread title>`, Talk `@alice`, `@alice,bob`, or `#dev`, Top
  `top <agent>` or `top team`, Serve `serve <agent>` or `serve hub`, Script
  Run `run <file>` (basename only), and Retry/Rerun `retry <run id>` /
  `rerun <run id>`. Each accepts `--title-prefix TEXT` and is configurable per
  type through `[terminal.title.prefix]` in `config.toml` or
  `TOOLANG_TITLE_PREFIX_<TYPE>`, with CLI over environment over agent-home
  config over root config precedence; unsafe controls are removed and the
  complete title is clipped to 32 display columns.

- In tmux, foreground commands publish pane-local options instead of session
  and window marks: `@toolang_kind`, `@toolang_root`, `@toolang_agent`,
  `@toolang_thread`, `@toolang_run`, `@toolang_convo`, and `@toolang_context`,
  read with `tmux show-options -p -t "$TMUX_PANE"` without `-A`.
  `TOOLANG_TMUX=0` disables marks while preserving OSC titles.

### Changed

- **Breaking:** cap template HTTP lists now contain only `default`, and
  `/templates/default` remains readable. The former
  `GET /api/v1/services/templates/stdio` endpoint returns `404`; use
  `GET /api/v1/services/templates/default` and edit `transport` and `target`
  for stdio services.

- Root help regroups its command panels into six ordered groups — Agent, Cap,
  Runtime, Work, Inspection, and Script Commands — retiring the Teaming
  Commands panel: `serve`, `start`, `stop`, and `hub` form the new Runtime
  Commands panel, `workspace` moves from Work Commands to Agent Commands,
  `talk` and `chore` join `chat` under Work Commands, and `top` moves from
  Teaming Commands to Inspection Commands. `too more` renames its Runtime
  Commands panel to Plugin Commands and orders Language Commands as `parse`,
  `highlight`, `fmt`, and root, target, and command help now share one clearer
  description per command.

- The chore HTTP API changed its payload shape: list and detail responses now
  carry `path` plus an optional `line` in place of the overloaded `path`, and
  nest execution state under `runtime` — `status`, `active_run_id`, `last_run`,
  `scheduler_error` (renamed from `error`), checkpoint `updated_at`, and
  `synced`, which is false while the checkpoint still describes an older or
  never-observed definition. `pending_at` and `next_scheduled_at` replace the
  ambiguous `next_run_at`, and the guessed `remote_ref` and `remote_status`
  fields are removed. Clients that read chore payloads must move to the
  `runtime` fields and drop the removed projections.

- Chore authoring edits only current home definitions: root and inline
  `chore name:` definitions stay visible to `too AGENT chore list` but must be
  edited at their source; `too AGENT chore new`, `edit`, and `delete` require a
  resident agent, `edit` and `delete` reject definitions that are not home
  `chores/<name>.md` files or that an inline declaration shadows, and `new` may
  shadow a root file but fails when the name is already declared inline, naming
  the source to edit, with the matching `/api/v1/chores` requests returning
  `409`.

- Tasks and jobs are unified into chores: current definitions are flat
  `chores/<name>.md` files or inline `chore name:` declarations with no draft,
  ready, or archive stages, and a definition is one-off unless it sets an
  explicit RRULE `schedule`. A chore ID is the first 80 bits of the SHA-256 of
  its exact, case-sensitive name encoded as 16 lowercase Base32 characters and
  its thread is `chore_<id>`, so renaming creates a new identity while editing
  the body, title, schedule, or location preserves it. The chore driver keeps at
  most one active run per chore, coalesces missed scheduled occurrences into one
  catch-up run and repeated manual requests into one pending manual occurrence
  while manual and scheduled work stay distinct and execute serially, and stores
  pending work, the active run, and schedule checkpoints in the `chores` table
  of `runs.db`. Removing a definition stops future dispatch but retains its
  checkpoint and history; restoring an unchanged completed, failed, or canceled
  one-off does not rerun it, and `chore run` is the explicit manual retry.
  Thread prefixes are now `chat_`, `talk_` (one durable thread per agent instead
  of one per message batch), `chore_`, `worker_`, and `script_`.

- Independent invocations of the same `.too` script source use separate runtime
  and sandbox instance directories, so each run keeps its own instance state.

- `too top` observes locally running runtime instances (resident, visiting,
  roaming, and script) from the root's `.runtime/instances` registrations, so it
  needs neither talk nor the Hub to cover them and combines them with remote Hub
  activity when a Hub is available.

- Chat commands and run directives now use distinct markers, breaking existing
  input syntax. Colon commands inspect or change session settings (`:model`,
  `:runnable`, `:agic`, `:flow`, `:allow`, `:limit`, `:workdir`, and `:?` for
  session help); slash commands (`/help`, `/?`, `/keys`, `/models`, `/tools`,
  `/caps`, `/agics`, `/flows`, `/output`, `/show`, `/exit`, `/quit`) remain
  standalone interactions. A run submission may begin with consecutive
  `/model`, `/allow`, `/limit`, and `/workdir` override lines followed by at
  most one root runnable call (`/NAME`, `/agic:NAME`, or `/flow:NAME`), whose
  signature decides arguments and primary input. Migrate by moving session
  settings from `/model`, `/runnable`, `/agic`, `/flow`, `/allow`, and `/limit`
  to their colon forms, renaming `/cd` to `:workdir` (bare `:workdir` queries
  the session directory), and invoking runnables as `/NAME` or a qualified
  `/agic:NAME`/`/flow:NAME` instead of `:runnable NAME`, `:agic NAME`, or
  `:flow NAME`; reserved built-in names need qualification, as in `/agic:help`.
  An override prefix requires at least one supplied primary or named input, so
  overrides formerly accepted with empty Call Input now reject, and root `---`
  capture becomes `-` without a closing fence. Script and Chore runs share
  this syntax and reject Chat-only slash commands and colon headers.

- Team `too top` refreshes from Hub state notifications instead of the
  aggregate activity stream, following execution and member events and reading
  each agent's current activity independently, so one slow or failing agent no
  longer delays presence or healthy agents; pending refreshes coalesce,
  reconnects reload current snapshots, and the Status bar reports
  `Reconnecting` while a source reconnects.

- `too talk` wakes its message and metadata read loop on relevant
  conversation-member changes from the Hub's team feed, leaving message
  cursors and rename polling unchanged.

- Chat and Talk now always run in the invoking terminal; Toolang no longer
  creates, selects, or renames tmux sessions or windows, removing `too AGENT
  chat` placement and the `talks` session. Chat's title is now
  `<agent>: <thread title>`, with the agent component limited to 12 display
  columns, instead of the previous 60-column thread title and `[new chat]`
  placeholder.

### Removed

- **Breaking:** the cap `template` subcommand is removed from `too` and `caps`,
  and `too new` and cap `new` commands no longer accept `--template`/`-t`.
  Creation always uses the one bundled default template per kind, and the extra
  `service.stdio` template is removed; to author a stdio service, edit
  `transport` and `target` in the generated default instead of selecting a
  template.

- **Breaking:** the hidden `too channel` command and its `channel list`
  subcommand are removed with the `toolang.channel` entry-point family, the
  bundled Telegram channel, the `create_channel` factory, and the channel
  protocols, value types, and helpers. Plugin configuration now consumes only
  supported families and ignores unknown ones, including former top-level
  `[channels.*]` and `[plugin.channel.*]` tables, instead of rejecting them.
  Remove imports of the former channel contracts or factory and stop
  registering channel entry points;
  no replacement channel plugin API is provided, and Hub/Talk messaging keeps
  its own transport.

- **Breaking:** `too AGENT chore clone` is removed; create a definition with
  `new` or copy a home `chores/<name>.md` file under a different name, which
  starts a new execution identity and independent progress.

- **Breaking:** the `too task` command and the `task name:` declaration are
  removed; `too AGENT chore` now lists, creates, edits, runs, cancels, and
  deletes chore definitions, and a `task name:` declaration fails with
  `Use chore for one-off or scheduled work`. No old directories, authored IDs,
  or `jobs.db` checkpoints are migrated or imported: existing `tasks/` files and
  databases are left untouched and unused, and a definition added to the new
  model starts from a checkpoint under its name-derived ID. The `me/create` tool
  no longer accepts `tasks/*.md`.

- **Breaking:** the task and job HTTP API is removed. `/api/v1/tasks`,
  `/api/v1/tasks/{task_id}`, the task draft/ready/archive/reopen/cancel routes,
  and `/api/v1/jobs` no longer exist; `/api/v1/chores` covers current-definition
  list/detail, create (now with a required `name`), update, delete, manual run,
  and cancel, and its payloads expose chore fields instead of task and job
  fields. Thread, run, rewind, and fork requests carry `client_id` and
  `request_id` instead of the removed `client` client-type field.

- The `@toolang_pad` and `@toolang_talk` tmux marks are retired. Integrations
  that read the old session and window marks must migrate to the pane-local
  `@toolang_*` options, and automatic placement now requires a user tmux
  binding.

### Fixed

- Recovering a scheduled chore whose claimed run was never accepted keeps the
  current schedule: after the schedule is changed or removed, the chore keeps
  the new next occurrence, or becomes `done` when no schedule or other pending
  work remains, instead of rerunning the replaced schedule's occurrence.

- Invalid inline `chore name:` schedules fail while parsing the `.too` module
  with an error naming the schedule, matching invalid Markdown definitions,
  and a malformed `chores/*.md` edit keeps the last valid definitions active
  instead of ending the watch, so a later corrected edit still applies.

- A chore that has no `chore` runnable uses the agent's configured default
  runnable before the unnamed entry fallback, so it no longer fails when the
  agent declares a default runnable but no unnamed entry.

- Chore inspection over HTTP reads current inline `chore name:` definitions
  straight from the agent program without preparing or publishing executable
  State, so listing and reading chores over `/api/v1/chores` works even when the
  program references missing or unresolvable flows and State publication fails.

- A chore whose schedule is already exhausted starts terminal: the first
  checkpoint records `done` with no next occurrence instead of leaving a pending
  chore that can never run.

- Reconciling a chore definition whose body and schedule are unchanged keeps the
  saved checkpoint intact, so its `updated_at` timestamp no longer advances on
  every scan.

- A manual request on a one-off chore keeps its `manual` trigger identity across
  restarts, so an unprocessed manual occurrence stays pending as `manual`
  instead of losing its origin.

- Chore inspection distinguishes current definitions from saved checkpoints:
  `too AGENT chore list` marks a definition `unsynced` when its checkpoint does
  not describe the current body and schedule instead of showing an older status,
  reports the earliest pending manual or source request alongside the next
  scheduled occurrence, and shows an inline `chore name:` definition's location
  as `<file>:<line>`.

- Listing an agent's capabilities (`too caps`, `caps list`, and the
  kind-specific lists) resolves current declarations without preparing or
  publishing executable agent State or validating runnable handlers, so an
  unresolvable flow no longer breaks the listing.

- `too AGENT chore list` now reads inline `chore name:` declarations
  directly without resolving runnable handlers or preparing State, so it
  lists definitions even when the agent program references missing or
  unresolvable flows instead of failing. (#767)

- Team `too top` keeps `Threads` and `Runs` header counts from an agent
  whose activity coverage is incomplete, and its Details panel shows `-`
  for counts it cannot confirm instead of `0/0`.

- Team `too top` keeps `Threads` and `Runs` header counts from agents
  whose activity is available when another agent's activity is missing,
  incomplete, or stale, showing `-` only when no agent's activity is
  available.

- Inline skills declared in `.too` modules are now honored instead of
  silently discarded: `skill NAME:` with a `description` and a literal body
  becomes a module-local capability whose description is exposed as its
  `skill/NAME` trigger and whose body loads on demand through
  `_toolang__pick`, and cached home layers that omitted inline skills rebuild
  automatically. A now-active declaration that shadows a same-name home or
  root skill wins inside its module, so remove or rename it to keep the
  previous effective skill.

- An agent whose run store has a schema this version does not support no
  longer breaks activity views: `too AGENT top`, team `too top`, and Hub
  activity pages label it `History unavailable` with its authoritative presence
  and unknown metrics while healthy agents keep updating, and Hub Details
  returns `503 Result unavailable`.

- `too chat` and `too talk` reapply their blinking block input cursor after
  each redraw, so it keeps blinking while composing instead of turning
  steady on terminals that stop blinking when the cursor is shown.

- Markdown code blocks in `too chat`, `too talk` agent messages, and
  execution progress now reach the right edge of the available content
  width with their background and two-cell padding intact, including
  inside list items and block quotes, instead of ending two cells short.

## [0.4.0a3] - 2026-10-11

### Added

- `too talk` opens with a startup panel captioned `Talk <client-version>`,
  printed once before the first messages: `hub` shows the connected Hub's
  version from the new `GET /info` endpoint — `unknown` when that inspection
  is unsupported, failed, or malformed after a two-second deadline, and never
  the local version — `convo` the conversation's complete ID, including a
  pending DM, and `user` the viewer's login, shown as
  `<login> · view only` to observers. The panel shares Chat's responsive
  layout and content width, scrolls away with the messages as an entry-time
  snapshot, and is not reprinted by reconnects, redraws, tmux window reuse, or
  Ctrl+L, while the Talk directory and one-shot sends show no panel and
  request no Hub version.

- `too chat` and `too talk` accept `TOOLANG_INPUTBOX_MAX_WIDTH`, a positive
  terminal-cell width limit for the input area — Chat's Input, Queue, and
  status bars and Talk's input and footer — independent of message output.
  When unset it falls back to `TOOLANG_PROGRESS_MAX_WIDTH` (120 columns by
  default), and both limits narrow to the terminal; an empty, non-numeric, or
  nonpositive value fails before interactive startup with an error naming the
  variable. Message output and submitted controls keep the progress width,
  while scripted Chat, one-shot Talk sends, and the Talk directory ignore the
  setting.

- Commands that prepare State now report waiting on a held State lock,
  showing `Waiting for root State...`, `Waiting for home State...`, or
  `Waiting for agent <name> State...` and reporting when the lock becomes
  available instead of appearing stalled.

- Commands that load agent setup or attach to a running agent now report
  staged progress while they block instead of appearing stalled, covering
  setup, models, tools, model-catalog discovery, capability materialization,
  and runtime readiness and management-lock waits, with
  statuses such as `Loading setup...`, `Loading models...`, `Discovering model
  catalogs...`, and `Waiting for agent API...`.

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

- `too talk` with no target prints one table of accessible conversations, most
  recently updated first, and exits, without requiring a TTY and without opening
  a session or tmux window: `CONVERSATION` shows the canonical ID, `NAME` the
  editable name, `MEMBERS` up to two member names followed by `,… (N)` when more
  are present, `UPDATED` the latest message timestamp, and `MESSAGE` the latest
  sender and preview. The former `Team` list and usage lines are removed, an
  unnamed conversation shows `-`, an empty listing prints `0 conversations`, a
  count line follows the table, and member and sender names omit their identity
  prefixes while long names, member lists, and messages are cropped or
  ellipsized so every row stays on one line.

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
  Period and Recent windows, Table adds an `S` presence column (`+` online,
  `-` offline, `?` unknown), Details renders on-demand result text as Markdown,
  and F7/F8 cycle the Recent and Period windows.

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

- The shared `too talk` tmux session is renamed from `talk` to `talks`.

- `too chat` and `too talk` report tmux placement without the chat-specific
  wording: `located pane %6 in <session>:<window>` for an existing chat,
  `created pane ...` for a new one, or `reused pane ...` for a retry, with
  failures reported as `failed to create pane: <reason>`.

- `too info AGENT` and `too AGENT info` read a running agent's models, tools,
  caps, chores, tasks, and workspaces from its runtime API instead of loading
  the local setup, and reject `--catalog` while the agent is running; stop the
  agent to change the catalog.

- **Breaking:** workspace selection options are now accepted only where a
  command executes with caller-selected directories (`serve`, `chat`, and
  script runs): `-w`/`--workspace` and `-d`/`--workdir` are removed from
  `tools`, `models`, `providers`, `workspace list`, and `start`, and
  `--no-auto-workspace` is removed everywhere. Configure `[workspaces]` or run
  `too AGENT workspace add DIR` before `start`, whose former `-d` subdirectory
  override has no configuration equivalent, and replace `--no-auto-workspace`
  with explicit `-w` options, since a script run without `-w` still adds its
  source directory even when `-d` selects a workdir.

- `too AGENT workspace list` now reads authored workspace configuration
  (source-local `toolang.toml` for scripts, layout `config.toml`
  otherwise) and lists configured names, resolved paths, and local
  availability without preparing State, querying a running server, or
  including the implicit `lab` workspace, temporary grants, or a workdir.

- Chat's startup banner now describes the connected runtime and its
  workspaces instead of the executor and agent home. Its caption is
  `Chat <client-version>` from the local process, and its rows are `runtime`
  (only `<version>`, with a single `v` prefix on known versions; `too -V`
  reports the client version and `too AGENT info` the API endpoint),
  `sandbox` (`<driver> · <environment>`; Docker shows its image
  selector instead of a container ID), and `workspaces` (the runtime's
  available logical names from `GET /api/v1/workspaces` in response order,
  `none` when the list is empty, `unavailable` when inspection fails, and
  never filesystem paths). The `home` row is removed, and the status bar still
  shows the current workspace.

- `too top` keeps a fixed six-line Header at every width: a first line with
  the team's online count or the agent's live session uptime in place of
  accumulated execution time, the local clock, two aligned statistics rows, and
  a `Period`/`Recent` settings line, so counts, filters, and windows never
  reflow it. The former Stats and Activity windows are renamed `Period` (F8,
  statistics range) and `Recent` (F7, activity visibility), the Table keeps the
  `AGENT` column in both scopes, and the Status bar's `F5` names the next view.

- `too top` Header `Runs` now counts every Activity-eligible run before
  filters, including completed runs, showing `0` when empty and `-` when
  unavailable, while active, failed, and `idle` summaries stay in Table
  ACTIVITY.

- **Breaking (teaming API):** the Python `HubClient` and `AgentClient`
  `create_conversation()` and `rename_conversation()` methods now return a
  validated `Conversation`, as `conversation()` already did, instead of an
  untyped dictionary; read attributes such as `.id` and `.participants`, or
  call `dataclasses.asdict()` when a dictionary is needed. HTTP JSON fields and
  `msg` tool outputs keep their record shape, and an inaccessible conversation
  now fails with HTTP `403` code `conversation_access_denied`.

- `too top` shows only function-key hints in its Status bar — F1 Help, F4
  Filter, F5 naming the next view, F6 Sort, F7 Recent, F8 Period, and F10
  Quit — so `F5` cycles the Agent, Thread, Run, and Tree levels, `F10` exits
  like `q` and Ctrl-C, and letter and Enter shortcuts remain available through
  Help; the Status bar no longer shows `Incomplete`, whose coverage and
  freshness detail stays in Details.

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
  The private `.runtime/hub.json` no longer records a token, and a record that
  still carries one is rejected instead of loaded; Text reconnects across a Hub
  restart, needing a reopen only when the Hub's endpoint, backend, or human
  changes.

- **Breaking (teaming transport):** agents now reach messaging, presence, and
  event export through the Hub HTTP API instead of connecting to Redis/Valkey
  directly, so only Hub talks to the backend. Start the Hub with `too hub
  start` before using agent `msg` tools or exporting agent events; stopping it
  leaves agents running, and they resume without a
  restart, while an uncertain send is never retried automatically. Isolated
  guests without access to the local Hub report unavailability. (#721)

- Interactive Text draws a dim bright-black `┄` rule on its own row below
  each agent message body, with no added blank row before it, starting at the
  body inset and reaching the message area's right edge; it is present even at
  narrow widths and with wrapped names. Left-aligned messages, including human
  bubbles, now fill the available configured content width, while own
  right-aligned messages keep their gutter. Sender name and marker colors and
  non-dim styling are unchanged.

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

- Execution progress Markdown blocks and the script run context header now
  end two cells before the available progress width's right edge, matching
  execution footers and two-ended rows and keeping one right margin across
  progress output.

- `too chat` and `too talk` show a blinking block cursor in their input area
  so it is easier to see while composing, and restore the terminal's default
  cursor shape on exit.

- `too chat` and `too talk` align their live output: execution progress
  footers and two-ended rows end two cells before the available progress
  width's right edge, Chat's banner is followed by two blank rows before
  the input box and the first control bar, and the empty-input placeholders
  read `Describe your task` in Chat and `Type a message` in Talk.

- Ctrl+L in Chat and Talk clears the display and places the input area at
  the top without a leading blank row, preserving prior output in native
  scrollback; Talk restores the gap above its input when a new message or
  notice is displayed.

- `too chat` and `too talk` pass the invoking process's
  `TOOLANG_PROGRESS_MAX_WIDTH` and `TOOLANG_INPUTBOX_MAX_WIDTH` into new tmux
  panes — including unset values, preserving the fallback — so a tmux server's
  stale environment no longer overrides them.

- `too chat` and `too talk` keep native terminal scrollback stable through
  terminal resizes: the live input no longer drifts across repeated resizes,
  and Chat's Input, Queue, and status bars now follow the input area width and
  no longer fill the terminal.

- Interactive Text now ends each agent message block at its last visible
  Markdown row: trailing empty rows, including a fenced code block's bottom
  padding, are trimmed so the `┄` rule follows the body with no gap, while
  interior spacing and code backgrounds are preserved.

- `too info` prepares only the selected target's layout and materializes a
  roaming `.too` source or visiting remote agent only when the target is not
  running, so a running roaming or visiting agent is inspected without
  projecting or fetching its source.

- Cap `new` and `edit` commands read the authored catalog without preparing
  State first, so they no longer fetch unrelated configured caps before the
  editor opens, and changes are published only after being saved; a failed
  publication now reports that the change was saved but agent State could not
  be published.

- `too tools` resolves tool policy through a tool-only Setup view that loads
  no model catalogs or adapters, so an invalid or unavailable model catalog
  no longer breaks tool listing.

- A malformed stored conversation record now rejects membership changes without
  partial writes, and losing membership in one conversation during a polling
  round no longer blocks the others.

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

[Unreleased]: https://github.com/openhat-ai/toolang/compare/v0.4.0a11...HEAD
[0.4.0a11]: https://github.com/openhat-ai/toolang/compare/v0.4.0a10...v0.4.0a11
[0.4.0a10]: https://github.com/openhat-ai/toolang/compare/v0.4.0a9...v0.4.0a10
[0.4.0a9]: https://github.com/openhat-ai/toolang/compare/v0.4.0a8...v0.4.0a9
[0.4.0a8]: https://github.com/openhat-ai/toolang/compare/v0.4.0a7...v0.4.0a8
[0.4.0a7]: https://github.com/openhat-ai/toolang/compare/v0.4.0a6...v0.4.0a7
[0.4.0a6]: https://github.com/openhat-ai/toolang/compare/v0.4.0a5...v0.4.0a6
[0.4.0a5]: https://github.com/openhat-ai/toolang/compare/v0.4.0a4...v0.4.0a5
[0.4.0a4]: https://github.com/openhat-ai/toolang/compare/v0.4.0a3...v0.4.0a4
[0.4.0a3]: https://github.com/openhat-ai/toolang/compare/v0.4.0a2...v0.4.0a3
[0.4.0a2]: https://github.com/openhat-ai/toolang/compare/v0.4.0a1...v0.4.0a2
[0.4.0a1]: https://github.com/openhat-ai/toolang/compare/v0.3.6...v0.4.0a1
[0.3.6]: https://github.com/openhat-ai/toolang/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/openhat-ai/toolang/compare/v0.3.4...v0.3.5

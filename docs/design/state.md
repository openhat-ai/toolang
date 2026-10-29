# State

Agent State is the immutable runtime input derived from an agent's Programs,
configuration, capabilities, and chore definitions. A module is a term for one
named, sourced Program in State; it is not a separate runtime type. Agent State holds the
complete module-name-to-Program index rather than a separate primary Program.
It combines one root layer with one home layer. Each accepted Run keeps one
Agent State revision. New named children select the latest valid publication
independently; inline bodies retain their containing plan.

Accepted home `chores/*.md` files and chore declarations inside `agent.too`
are part of Agent State. The chore driver owns activation, checkpoints, and
execution progress. Root chores are ignored; the home directory is created
only on its first explicit file write. Installed providers, tools, environments,
and other [Setup](setup.md) data remain separate from Agent State.

State preparation reads authored source but never edits it. The place
and agent home must already exist. A missing `agent.too` produces the empty
default `agent` module term with an empty Program without creating the source
file.

The [canonical documents](#canonical-documents-and-revisions) define parsed
values, exact source/fetched file maps, pinned upstream versions, and revision
creation times.

## Vocabulary

State has two persistent layers and one composition:

- the `root` layer contains configuration and capabilities from agent root;
- the `home` layer contains configuration from agent home, agent and runnable modules,
  home and module-local capabilities, and Markdown chore terms; and
- the `agent` composition identifies one exact root/home revision pair.

The in-memory composition exposes aggregate indexes. `modules` resolves stable
module names to Programs, while matching source, digest, and module-local cap
indexes retain their runtime context. `caps` indexes effective capabilities by
`kind:name`; `skills`, `psyches`, `services`, and `prompts` are its name-keyed
classifications. `agics` and `flows` classify the public `runnables` index. A
module-local runnable index resolves private and exported `AgicDecl` and
`FlowDecl` values. These indexes are immutable views of prepared State terms
and are not separate persisted definitions.

Capability scope is `root`, `home`, or `here`, with precedence
`root < home < here`. Capability form is exactly `authored`, `inline`,
`configured`, or `referenced`:

- `authored` capabilities come from capability files;
- `configured` capabilities come from refs in `config.toml`;
- `inline` capabilities are declared inside one program module; and
- `referenced` capabilities are attached by a module `with` declaration.

Root and home layers contain authored and configured capabilities. Inline and
referenced capabilities have `here` scope and belong to one module.

## Persistent Layout

```text
${PLACE}/.state/root/
  edit.lock
  v24/
    current
    build.lock
    revs/<root-revision>/
      layer.json
      source/
        config.toml
        prompts/<name>.md
        skills/<name>/SKILL.md
      materialized/<acquisition-key>/<original-relative-path>

${PLACE}/agents/<agent>/.state/home/
  edit.lock
  v24/
    current
    build.lock
    revs/<home-revision>/
      layer.json
      source/
        agent.too
        config.toml
        runnables/<name>.too
        chores/<name>.md
        prompts/<name>.md
        skills/<name>/SKILL.md
      materialized/<acquisition-key>/<original-relative-path>

${PLACE}/agents/<agent>/.state/agent/v24/
  current
  publish.lock
  revs/<state-revision>/
    layers.json
```

The `v<format>` directory follows `AGENT_STATE_FORMAT`, currently 24. Software
releases with the same State format share these stores. Different formats have
independent pointers, revisions, build/publication locks, and watcher baselines.
A watcher ignores another format's derived files and checks only its own current
pointers. Authored source remains shared.

Each source scope's unversioned edit lock coordinates participating source
writers and is created only on a mutation attempt. State preparation does not
acquire edit locks; it verifies source snapshots and retries when sources change.
Build and publish locks coordinate derived State writes within each format.
Unversioned caches and other formats' stores are ignored without migration,
fallback, or deletion. A missing current-format store is rebuilt from authored
inputs; exact historical loads search only the running format's store. State
isolation does not change runtime, API, or database compatibility rules.

For roaming, substitute `DIR/.toolang` for PLACE and
`DIR/.toolang/agents/NAME` for agent home in the State paths above. State sources
and watchers use those shared roots; runtime identity/storage remains below
`DIR/.toolang/R8/P19`. Each State scope keeps its own `current`; no State pointer
is added to `.runtime`. Roaming agent `current` publishes authored State;
invocation overrides and temporary grants are captured in separate exact Run
revisions. Changing another instance's bindings does not change accepted Runs.

## Canonical Documents and Revisions

`layer.json` (agent state format 24) contains `scope`, `created_at`, normalized State
`config`, `source` and `materialized` file maps, and `modules`, `caps`, and
`chores` arrays. Each item has `ref`, `source`, and `value`; caps also have
`form`. The `source` field names its original file in the source map. Only an
empty default agent module has `source: null`, when `agent.too` is absent or
excluded.

Program values hold the parsed AST. Cap values hold `meta` and `body`. Chore
values hold `title`, `body`, and `schedule`, after home Markdown/agent-inline
precedence. All accepted Cap candidates remain in the layer, including candidates
shadowed in another scope or module. Selection and `shadowed` inspection data
are computed in context.

Each file map records `{size, sha256}` for exact raw bytes. Accepted inputs are
copied unchanged under `source/`, including config comments and line endings;
known relative configuration paths are resolved separately in `config`. Rejected
inputs add `error: {code, message, line?}` and have no stored copy; unreadable
inputs have null size and digest. Config cap declarations are represented in
`caps`, rather than repeated in normalized config.

Fetched items include one complete `resolution` object:

```json
{"declared_ref":"gh:owner/review","resolved_ref":"github://owner/skills/review@<full-commit-sha>","materialized":"<acquisition-key>/review/SKILL.md"}
```

`resolution.materialized` selects an entry in the materialized map. Its acquisition
key is SHA-256 of the UTF-8 canonical `resolved_ref`. Equal acquisitions share
files across consumers. Conflicting file sets or bytes exclude the conflicting
Program during acceptance; conflicts with configured acquisitions fail preparation.
Source owners supply canonical refs, entry paths, and original relative paths.
State does not interpret schemes or choose a default source. Visiting Mod
provenance is passed from its source cache into its module item with the exact
acquired Program bytes.
If that cached Program is edited locally, the new module keeps the captured
local source and omits the fetched resolution. Restoring the acquired bytes
restores their provenance; the visiting source-cache lifecycle is unchanged.

`layers.json` (the same agent state format 24) records `created_at`, selected root/home
revisions, agent name, frozen cap overrides, and optional workspace additions.
Both documents use canonical UTF-8 JSON: sorted object keys, compact separators,
direct Unicode, finite numbers, and no trailing newline. Unordered item arrays
are sorted; semantic order, such as workspace order, is retained. Exact bytes
define revisions:

```text
layer revision = sha256(layer.json bytes)
State revision = sha256(layers.json bytes)
```

Revisions are lowercase 64-character SHA-256 strings. Each new revision includes
a fixed-width UTC creation time with microseconds, such as
`2026-10-15T08:30:00.000000Z`. Before assigning time, preparation compares the
candidate without `created_at` to the current document. A no-op reuses that
revision and time, including on forced refresh. Returning to earlier content
after an intervening change creates a new revision.

List home revisions by creation time from an agent home:

```sh
jq -r 'select(.created_at != null) | [.created_at, input_filename] | @tsv' \
  .state/home/v24/revs/*/layer.json | sort
```

Public source receipts keep `scope`, `key`, `observed_digest`, `accepted_digest`,
and flat diagnostics. These runtime projections are reconstructed from the map;
there is no duplicate persisted receipt list.

Invalid cap, chore, and Program units are excluded independently while valid
siblings publish. Invalid inline resources exclude their whole Program. No
previous definition is reused. An excluded agent Program contributes only an
empty structural module; other valid modules remain available. Shared config,
root/home access, persistence, and internal composition failures still reject
the whole candidate. Root/home precedence applies to accepted definitions.

Chore inspection keeps accepted inline definitions ahead of rejected home files.
A rejected file with an invalid chore name remains addressable by its source
path; its API `id` and `runtime.thread_id` are null because it has no execution
identity.

The raw `state` file tools retain `{key, digest}` write receipts and disk-digest
checks. `state.sync()` returns `{revision, files}` with the flat source records
above, including Markdown chores. Successful publication may contain excluded
sources. A fatal failure returns `{error, message, revision, files: []}`, with the
last published revision or null. Sync performs one serialized check without an
additional reporting scan. It does not replace active code or refresh Setup.
See [the state interface](tools.md#current-agent).

Normal loading checks persisted values and references without hashing files or
parsing captured syntax. Cap text and summaries use JSON values; explicit raw
details and skill asset reads may open captured files. Loading never reads
current authored source, chooses a default source, resolves refs, or contacts a
provider.

Explicit offline validation checks canonical document bytes, revision hashes,
exact source/materialized file sets, sizes/digests, paths, item ownership,
resolutions, and stored value schemas. It recursively validates composition
layers without resolving Source Refs or reinterpreting raw syntax. Hashes protect
captured data against a known revision; they do not authenticate a publisher.
Rejected bytes are absent, so only their observation records can be verified.
Old agent state formats are unsupported. Derived current caches can be
rebuilt from authored inputs; historical data is not migrated or converted.

## Programs And Module Terms

The main module `agent` comes from `agent.too`. Each direct
`runnables/<name>.too` contributes an independent runnable module named `<name>`.
The builtin module is `_`; its packaged Talk source is captured when bound by
the runtime, rather than added to every prepared home. This captured source
does not create a State layer or change its revision. Native compaction needs
no parsed Program. [Runnable names](program.md#runnable-names) defines
FQNs, kinds, lookup, and interface labels. Prepared Program roles remain `agent`
and `runnable`; the exported declaration determines execution kind.

Runnable filename stems must match `^[A-Za-z_][A-Za-z0-9_-]{0,63}$`, must not be a
Windows reserved device name or the reserved module names `agent` and `_`, and
must be unique under Unicode `casefold()`.
A runnable module exports exactly one unnamed or filename-matching agic or flow.
Renaming an unnamed-entry file therefore renames its public runnable
without editing its source.

Inline Cap values stay in `layer.json`; no generated Markdown files are written.
Referenced Caps use shared acquisitions under `materialized/`, with no module
or Cap-kind directory prefix.

Programs preserve omitted agic and flow names as `None`. State binds an unnamed
top-level declaration under the lined lookup key `<entry:LINE>`, applies
filename-based public exports, and rejects duplicate local or public names across
both kinds. It indexes the original declarations without mutating their AST names.
Inline agic bodies stay unnamed and out of the index; they are addressed only as
`module::<adhoc:LINE>`. The indexes are derived from Programs and are not duplicated
in `layer.json`. Script help uses the same binding rules.

## Prepare, Publish, and Load

Managed AgentServer startup prepares State locally before creating a workload;
foreground host `serve` prepares before entering the server in the same process.
The server then validates and reuses compatible prepared layers and owns the
subsequent watcher. Setup stays in the server process. Remote `.too` agents are
fetched and parsed before their dependencies are prepared; independent remote
caps retain bounded parallel preparation.

Cached preparation is silent. Required work appears on stderr as reusable,
dim `<ref> fetching`, `parsing`, `resolving`, or `updating` rows. Rows disappear
as work finishes, then one dim summary replaces the region. No spinner or
running counter is shown. Plain output uses operation transitions and a summary
without terminal controls; fully cached preparation produces no summary.

Cap inspection, publication after saved edits, offline `info`, and local script
execution use this same presentation. Preparation completes before command
records, Setup inspection, or run progress are displayed. Cap inspection retains
its existing boundary: it resolves capabilities without preparing executable State.
Remote-cap additions also show ref rows during validation; validation alone
does not produce a preparation summary. After an agent-scoped change is saved,
its State publication owns that summary.

State passes unchanged source text to `Program.from_source`; all syntax belongs
to `toolang.lang` and its tree-sitter grammar. Legacy `agent NAME` and `agent:`
headers are rejected. Missing optional source is represented by empty text.

Normal preparation compares raw source identities and asks the Source owner
whether each recorded selection remains reusable under current source policy.
The watcher performs that eligibility check even when local files are unchanged.
Reusable versions do not require upstream polling. A forced State refresh
rechecks Cap acquisitions: GitHub selectors resolve to full commit SHAs before
files are fetched at that SHA; HTTP(S) URLs are fetched again and compared by
bytes. A changed Git commit changes provenance even when content is equal.
Visiting Mod acquisition keeps its one-hour source-cache lifecycle; State records
the supplied Program and resolution. Historical/offline loads use their captured
refs without consulting current selection policy.

When rebuilding a layer, a writer:

1. acquires the layer build lock and checks the source manifest again;
2. captures authored files and parses config and modules;
3. resolves and materializes configured and referenced capabilities;
4. verifies the process-local source observation did not change during
   preparation;
5. writes a complete temporary revision directory;
6. atomically installs the immutable revision; and
7. atomically publishes the layer `current` pointer.

After both layers are prepared, preparation persists `layers.json` and validates
the runtime composition before atomically publishing `agent/current`. Re-publishing the same revision is a
no-op. Root, home, and agent revisions remain on disk so execution records can
be resolved later. Readers assume these immutable revision directories are
complete; integrity checks happen only through explicit validation.

## Watching and Access

`StateWatcher` owns the process-local current State. It exposes operations to
read the current State, load an exact revision, request another candidate
check, and inspect diagnostics for the latest rejected candidate.

At startup it first attempts to load `agent/current` as the last-known-good
State. A valid changed candidate is fully persisted before it replaces that
State. An invalid candidate records structured diagnostics and keeps the prior
revision available; startup fails only when neither a valid candidate nor a
loadable new-format published State exists.

The watcher permits one filesystem monitor and one serialized check/publication
path. It retains process-local path and stat observations separately from the
portable manifests. Periodic checks compare observations without reading file
contents; changed files alone are rehashed. Filesystem events and explicit
refreshes invalidate affected digests, so a byte change is detected even when
size and modification time are preserved. `refresh()` submits one additional
check and waits for that specific check to finish; concurrent calls do not run
checks in parallel or merely join an older check. An internal `current`
publication whose revision is already current does not produce another
candidate check.

The complete check and publication transaction also holds the agent-scoped
`publish.lock`, so multiple processes cannot interleave checks for the same agent
or let an older check publish after a newer one. Different agents use different locks
and may check concurrently. Root and home `build.lock` guards remain narrower:
they are acquired only when that shared or agent-local layer actually needs to
be rebuilt. The lock order is agent publish, then root build, then home
build. This process-owned boundary is also the State access boundary for
current-agent `state` tools.

## Execution Records

Every Run entry stores its accepted Agent State revision, including named and
inline children. Run and Step records carry a `ControlRef` named `state` pointing
to that entry or an explicit `exec` transfer.

The executor retains the immutable State supplied at acceptance for active
bindings. Watcher publication affects future named acceptance and resource
resolution for later Model Calls. Installed model catalogs remain Setup-owned;
accepted code, types, directives, and workspace authority stay bound.
[State capture](execution.md#state-capture) owns live-State selection and those
execution boundaries. Ordinary rerun uses current State; recorded references
preserve the accepted source revision for inspection.

Model/provider continuation is a different value and is named `cont` in model
calls, model results, model step records, and runtime agic state. Execution-store
compatibility follows [Durable Truth](execution.md#durable-truth).

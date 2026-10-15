# Agent State

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
and other `AgentSetup` data remain separate from Agent State.

State preparation reads authored source but never edits it. The Toolang root
and agent home must already exist. A missing `agent.too` produces the empty
default `agent` module term with an empty Program without creating the source
file.

## Vocabulary

State has two persistent layers and one composition:

- the `root` layer contains root config and root capabilities;
- the `home` layer contains agent config, the agent and runnable modules, home
  capabilities, module-local capabilities, and Markdown chore terms; and
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
${TOOLANG_ROOT}/.state/root/
  current
  edit.lock
  build.lock
  revs/<root-revision>/
    layer.json
    files/
      config.toml
      caps/
        authored/<kind>/<name>/...
        configured/<kind>/<name>/...

${TOOLANG_ROOT}/agents/<agent>/.state/home/
  current
  edit.lock
  build.lock
  revs/<home-revision>/
    layer.json
    files/
      agent.too
      config.toml
      runnables/<name>.too
      chores/<name>.md
      caps/
        authored/<kind>/<name>/...
        configured/<kind>/<name>/...
        inline/<module>/<kind>/<name>/...
        referenced/<module>/<kind>/<name>/...

${TOOLANG_ROOT}/agents/<agent>/.state/agent/
  current
  publish.lock
  revs/<state-revision>/
    layers.json
```

Each source scope's edit lock coordinates participating source writers and is
created only on a mutation attempt. State preparation does not acquire edit
locks; it verifies source snapshots and retries when sources change. Build and
publish locks coordinate derived State writes. Old
`.state/current` and `.state/versions` caches are ignored and are not migrated.

## Canonical Documents and Revisions

`layer.json` is a complete root or home layer identity document. It contains:

- schema and scope;
- a portable source manifest of sorted logical paths, captured byte sizes and
  SHA-256 digests, plus each raw authored file's SHA-256 digest;
- parsed config;
- configured and referenced resolutions;
- State capabilities;
- accepted Markdown chore terms (name, title, body, schedule, and source path);
- home program modules and their module-local capabilities; and
- a sorted manifest of every file below `files/`, including path, size, and
  SHA-256.

`layers.json` records the composition schema, selected root and home revisions,
agent name, frozen cap overrides, and workspace additions when present:

```json
{"allow_overrides":{},"home_revision":"<sha256>","name":"alice","root_revision":"<sha256>","schema":2}
```

Both documents use canonical UTF-8 JSON: keys are sorted, separators contain
no whitespace, Unicode is emitted directly, non-finite numbers are rejected,
and no trailing newline is written. Their exact bytes define revisions:

```text
layer revision = sha256(layer.json bytes)
State revision = sha256(layers.json bytes)
```

Revision values are lowercase 64-character SHA-256 hex strings. State does not
persist an absolute source path, filesystem identity, Toolang version, or
observation timestamp as identity metadata. Captured config resolves known relative
paths before use. The source manifest also retains raw-byte digests, so comments,
line endings, and Setup-only config changes affect identity even when prepared
State terms do not change. Raw config content is not copied into State storage.

Every newly prepared AgentState exposes sorted source records containing
`scope`, `key`, `observed_digest`, and `accepted_digest`. Digests identify exact
raw authored bytes. Valid sources have equal digests; invalid readable sources
have a null accepted digest and flat `code`, `message`, and optional `line`.
Unreadable files/directories have null digests and a read diagnostic. Valid
shadowed inputs and skill assets are included; deleted files are absent. These
records are persisted and included in `to_snapshot()`.

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

New layers use schema 21 and source manifest schema 4. Accepted Markdown chore
terms are stored alongside their captured bytes and restored without parsing
authored syntax. Explicit integrity validation checks those terms against the
captured Markdown and acceptance records. Schema 20 histories restore Markdown
chores from their captured bytes; older schemas have no Markdown chore index.
Preparation rebuilds older current layers without rewriting history. Explicit
historical loads retain known file identities; schema-3 config and schema-2 source
trees lack reliable raw
identities and are not exposed as tracked files.

Normal loading trusts a revision to be complete and reads its persisted
documents directly. It does not hash document or materialized file content.
Explicit validation separately requires canonical document bytes, matching
revision digests, an exact recursive `files/` manifest, and valid capability,
module, and resolution references. No normal runtime path implicitly requests
that validation. Loading a revision never reads current authored source, falls
back to `current`, or contacts a remote provider. Current-schema loading restores
accepted terms without parsing source; legacy compatibility paths may consult
captured bytes.

## Programs And Module Terms

`agent.too` is the special `agent` module term. Each direct
`runnables/<name>.too` file contributes an independent module term named
`runnables::<name>`. Prepared Program kinds are `agent` and `runnable`;
the exported declaration determines whether it executes as an agic or flow.

Runnable filename stems must match `^[A-Za-z_][A-Za-z0-9_-]{0,63}$`, must not be a
Windows reserved device name or the unnamed-entry alias `_`, and must be unique
under Unicode `casefold()`.
A runnable module exports exactly one unnamed or filename-matching agic or flow.
Renaming an unnamed-entry file therefore renames its public runnable
without editing its source.

Module-local capability files include the module name in their path:

```text
files/caps/<inline|referenced>/<module>/<kind>/<name>/...
```

Programs preserve omitted agic and flow names as `None`. State binds an unnamed
top-level declaration under the lined lookup key `<entry:LINE>`, applies
filename-based public exports, and rejects duplicate local or public names across
both kinds. It indexes the original declarations without mutating their AST names.
Inline agic bodies stay unnamed and out of the index; they are addressed only as
`agic:<adhoc:LINE>`. The indexes are derived from Programs and are not duplicated
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

Normal preparation hashes the selected source bytes and compares the portable
manifest with the published layer. An unchanged current layer is loaded without
parsing Programs, materializing capabilities, or polling remote refs. Layer
schema 8 rebuilds older caches with structured module and parameter documentation
from grammar 0.3.2, in addition to strict source parsing and unnamed declaration
binding. Exact historical loads retain their recorded Programs and run references.
These rules also apply when the root and home are mounted at different absolute
paths. An explicit refresh resolves remote refs again.

When rebuilding a layer, a writer:

1. acquires the layer build lock and checks the source manifest again;
2. captures authored files and parses config and modules;
3. resolves and materializes configured and referenced capabilities;
4. verifies the process-local source observation did not change during
   preparation;
5. writes a complete temporary revision directory;
6. atomically installs the immutable revision; and
7. atomically publishes the layer `current` pointer.

After both layers are prepared, preparation persists `layers.json` before
atomically publishing `agent/current`. Re-publishing the same revision is a
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

A legacy source metadata tree remains decodable only when loading an exact
historical layer. It never matches current authored source; a legacy current
layer is rebuilt with the portable manifest schema.

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
to that entry or an explicit execute transfer. Retry retains its existing
entry reference.

The executor retains immutable State for active bindings and loads historical
revisions from durable snapshots. Watcher publication affects future named
acceptance and model catalogs, without changing accepted code or caps.

Model/provider continuation is a different value and is named `cont` in model
calls, model results, model step records, and runtime agic state. The runs
database accepts schema version 52 and rejects unsupported versions at open.

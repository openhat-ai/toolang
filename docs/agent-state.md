# Agent State

Agent State is the immutable runtime input derived from an agent's authored
program, program modules, configuration, and capabilities. It combines one
root layer with one home layer. A top-level run binds one Agent State revision,
and descendants keep that revision for the lifetime of the run tree.

Tasks and chores stored as independent Markdown files are work data, not Agent
State. Task and chore declarations inside `agent.too` are part of State because
they are part of the program. Installed providers, tools, environments, and
other `AgentSetup` data remain separate from Agent State.

State preparation reads authored source but never edits it. The Toolang root
and agent home must already exist. A missing `agent.too` produces the empty
default program in State without creating the source file.

## Vocabulary

State has two persistent layers and one composition:

- the `root` layer contains root config and root capabilities;
- the `home` layer contains agent config, the agent and flow modules, home
  capabilities, and module-local capabilities; and
- the `agent` composition identifies one exact root/home revision pair.

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
  prepare.lock
  revs/<root-revision>/
    layer.json
    files/
      config.toml
      caps/
        authored/<kind>/<name>/...
        configured/<kind>/<name>/...

${TOOLANG_ROOT}/agents/<agent>/.state/home/
  current
  prepare.lock
  revs/<home-revision>/
    layer.json
    files/
      agent.too
      config.toml
      flows/<flow-name>.too
      caps/
        authored/<kind>/<name>/...
        configured/<kind>/<name>/...
        inline/<module>/<kind>/<name>/...
        referenced/<module>/<kind>/<name>/...

${TOOLANG_ROOT}/agents/<agent>/.state/agent/
  current
  check.lock
  revs/<state-revision>/
    layers.json
```

Locks and temporary paths are writer implementation details. Old
`.state/current` and `.state/versions` caches are ignored and are not migrated.

## Canonical Documents and Revisions

`layer.json` is a complete root or home layer identity document. It contains:

- schema and scope;
- the source metadata tree used for change detection;
- parsed config;
- configured and referenced resolutions;
- State capabilities;
- home program modules and their module-local capabilities; and
- a sorted manifest of every file below `files/`, including path, size, and
  SHA-256.

`layers.json` contains exactly one schema number and the selected root and home
revisions:

```json
{"home_revision":"<sha256>","root_revision":"<sha256>","schema":1}
```

Both documents use canonical UTF-8 JSON: keys are sorted, separators contain
no whitespace, Unicode is emitted directly, non-finite numbers are rejected,
and no trailing newline is written. Their exact bytes define revisions:

```text
layer revision = sha256(layer.json bytes)
State revision = sha256(layers.json bytes)
```

Revision values are lowercase 64-character SHA-256 hex strings. State does not
persist a Toolang version or observation timestamp as identity metadata.

Normal loading trusts a revision to be complete and reads its persisted
documents directly. It does not hash document or materialized file content.
Explicit validation separately requires canonical document bytes, matching
revision digests, an exact recursive `files/` manifest, and valid capability,
module, and resolution references. No normal runtime path implicitly requests
that validation. Loading a revision never reads current authored source, falls
back to `current`, reparses a program source file, or contacts a remote
provider.

## Program Modules

`agent.too` is the special `agent` module. Each direct
`flows/<name>.too` file is an independent module named `flow_<name>`. The
module name is an opaque, single filesystem segment.

Flow filename stems must match `^[A-Za-z_][A-Za-z0-9_-]{0,63}$`, must not be a
Windows reserved device name, and must be unique under Unicode `casefold()`.
A flow module exports the unnamed flow or the flow whose name matches the file
stem. Renaming an unnamed-flow file therefore renames its public runnable
without editing its source.

Module-local capability files include the module name in their path:

```text
files/caps/<inline|referenced>/<module>/<kind>/<name>/...
```

The public runnable catalog is derived from validated modules and is not
duplicated in `layer.json`.

## Prepare, Publish, and Load

Normal preparation compares source metadata with the published layer. An
unchanged layer is loaded directly, so unchanged remote refs are not polled.
An explicit refresh resolves remote refs again.

When rebuilding a layer, a writer:

1. acquires the layer writer lock and checks source metadata again;
2. captures authored files and parses config and modules;
3. resolves and materializes configured and referenced capabilities;
4. verifies source metadata did not change during preparation;
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
path. Filesystem events and periodic metadata checks submit work to that path.
`refresh()` submits one additional check and waits for that specific check to
finish; concurrent calls do not run checks in parallel or merely join an older
check. An internal `current` publication whose revision is already current does
not produce another candidate check.

The complete check and publication transaction also holds an agent-scoped file
lock, so multiple processes cannot interleave checks for the same agent or let
an older check publish after a newer one. Different agents use different locks
and may check concurrently. Root and home layer writer locks remain narrower:
they are acquired only when that shared or agent-local layer actually needs to
be rebuilt. The lock order is agent check, then root layer writer, then home
layer writer. This process-owned boundary is also the State access boundary for
future internal `_me` tools.

## Execution Records

Execution preparation records store the Agent State revision in a field named
`state`. Root runs and all descendants continue using the bound revision.
Switching an active run tree to a newer State requires a future explicit
runtime operation and is not part of normal watcher publication.

Model/provider continuation is a different value and is named `cont` in model
calls, model results, model step records, and runtime agic state. The runs
database migrates version 28 model-step continuation keys from `state` to
`cont` without modifying preparation-control Agent State fields.

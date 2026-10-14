# Unified Runnable Modules

## Status

Approved for implementation on 2026-10-12. The human selected `runnables/`,
retained `agent.too`, and explicitly excluded compatibility and migration work.

## Goal and Scope

Each `HOME/runnables/<name>.too` is an independent Program exporting one agic or
flow as `<name>`. A handler such as `talk.too` can be shared as a source file,
with private helpers and module-local capabilities. `HOME` means one agent's
home. Existing `config.toml`, capabilities, and chores remain unchanged.

Scope includes discovery, export validation, root composition, prepared State,
home authoring tools, public listings, documentation, and offline tests. It
excludes new syntax, recursive modules, imports, package installation, automatic
migration, and old flow-module compatibility.

## Decisions

1. Keep `agent.too` as root module `agent`, with its existing public declarations
   and default-entry rules. Discover extensions only in direct
   `runnables/*.too` files with lowercase suffixes. Other HOME-level `.too`
   files, `flows/`, `agics/`, nested modules, and Toolang-root modules are ignored.
   Standalone script invocation remains unchanged.
2. Select exactly one top-level agic or flow whose name is absent or exactly
   matches the filename stem. Zero or multiple candidates reject the module;
   inline anonymous steps are not candidates. Publish it under the filename,
   with its declaration determining kind. Other declarations remain private.
   Renaming a named entry's file also requires renaming its declaration.
3. Use `runnables::<name>` as module identity. Public references retain
   `name`, `agic:name`, and `flow:name`; kind-qualified references enforce kind.
   Private references retain module qualification, for example
   `runnables::talk::agic:helper`. Preserve authored AST names and existing
   lined identities for unnamed declarations.
4. Keep the current portable filename rules: ASCII letter or underscore first,
   then letters, digits, underscores, or hyphens, at most 64 characters, excluding
   reserved device names and `_`, which remains the unnamed-entry alias. Reject
   case-folded filename collisions. Reject public
   collisions with `agent.too` across either kind and identify both sources.
   No lookup priority or silent shadowing is introduced.
5. Root flow statements and agic routes can call either kind of public export.
   Extensions can call only their own entry and helpers. Types, instructions,
   and `here` caps remain owned by their Program; existing home/root resource
   precedence still applies. Cross-module imports remain out of scope.
6. Source capture, observation, and watcher path classification use the same
   discovery boundary. Valid changes publish a new immutable State; invalid
   candidates retain the last valid State and recover after repair. Existing
   run binding and explicit State-change controls remain unchanged.
7. Prepared Program roles become `agent` and `runnable`; the runnable's actual
   kind remains declaration-owned. Bump the prepared-layer schema and rebuild
   current caches through normal preparation. Do not translate or accept old
   flow-module records or identities. Do not rewrite authored files or history.
8. Existing `me` operations accept `runnables/*.too` and reject `flows/*.too`.
   Preserve digest checks, path safeguards, and the roaming `agent.too`
   exception. Use `.runnables.lock` for extension writes and retain the existing
   root-program and other resource locks.
9. A published `runnables/talk.too` follows existing [messaging](../messaging.md)
   selection and input/reply rules. An inline root `talk` conflicts with it.
   Sharing copies source; recipient environments supply required models,
   credentials, services, and external files.

## Examples

`runnables/research.too` exports `flow:research`:

```too
flow():
  run helper

agic helper():
  Produce a short report.
```

`runnables/talk.too` exports `agic:talk`:

```too
agic(_: Json):
  Handle this message batch: {{_}}
  Reply through msg/send when needed; return a handling summary.
```

## Implementation Touchpoints

- `src/toolang/state/{source,prepare,state,cache,types,errors}.py`: discovery,
  generic export binding, diagnostics, and prepared Program records.
- `src/toolang/lang/{ast,validate}.py`: external declarations of either kind;
  keep language validation independent of State and runtime services.
- `src/toolang/execution/runnables.py`: root calls to either exported kind.
- `src/toolang/execution/tools/me/`: allowed paths, locking, tool descriptions.
- `src/toolang/work/messaging.py`: receipts from the handler's owned Run tree.
- Existing watcher, messaging, CLI, and API consumers share the public index.
- Focused module, layout, State, messaging, execution, and home-authoring docs;
  generate the user-facing change record with `too aid.too update_changelog`.

## Acceptance Tests

1. Both kinds export named and unnamed entries, resolve to the owning module,
   keep helpers private, and reload from prepared State without authored files.
2. Missing/ambiguous exports, nonportable filenames, case-folded filenames, and
   public conflicts reject State with actionable diagnostics.
3. Root calls and routes execute exported agics and flows with their own
   helpers, input contracts, and capabilities; extension isolation is preserved.
4. Messaging selects file-based agic/flow `talk` handlers and retains its batch,
   reply, context, and restart behavior with fake providers, including receipts
   from owned child Runs and sends completed before cancellation.
5. Watcher changes, deletion, rename, rejection, and repair retain existing
   publication behavior for the new source location.
6. Ignored locations contribute no Programs or source events. A root-level
   `talk.too` does not conflict with discovered `runnables/talk.too`.
7. Home file CRUD, locking, and public local/remote/API listings agree with the
   new paths and exported kinds. Old home-authoring paths are rejected.
8. Existing root defaults, agent discovery, and standalone scripts remain
   unchanged. Default verification passes: `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest -n auto`.

## Risks and Open Questions

The layout and module identity changes are breaking: old flow directories and
module records are unsupported. No compatibility or migration mechanism is
provided. Every `.too` file in `runnables/` participates in State validation.
Shared files still depend on the receiving environment and cannot dispatch to
arbitrary sibling modules. There are no open design questions.

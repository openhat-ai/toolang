# Initialize Scripts and Expose Explicit Script Execution

## Goal and Approval

Create a small Script with `too init DIR` and execute it with
`too run FILE [RUNNABLE]`. Use `serve` for foreground agent hosting. Both `too`
and `toolang` expose the same behavior.

The human approved implementation, path-first shorthand, `main` as the shared
entry name, the existing help position for `serve`, and a final Script Commands
panel. During review, the human confirmed that AST declarations preserve omitted
names and State owns all module-aware name binding and indexing. Comment syntax
is handled in a separate task.

PR #529 renamed the internal server command to `_serve`. This change retains
that behavior and moves the former public `run` command to `serve`. Integration
with `db753ade` (#527) uses its data-only runnable descriptions, route snapshots,
and model protocol separation.

Success means one-file initialization, equivalent explicit/shorthand execution,
consistent default-entry selection, and shared runnable documentation in CLI help
and authorized model routes.

## Command Contract

| Command | Behavior |
| --- | --- |
| `too init DIR` | Create `DIR/main.too`; use `.` for the current directory. |
| `too init` | Show help without creating files. |
| `too run FILE` | Execute authored `main`, or show file help if absent. |
| `too run FILE RUNNABLE` | Execute the selected authored runnable once. |
| `too FILE [RUNNABLE]` | Retain path-first Script invocation. |
| `too run [--help]` | Show static Script entry-point help. |
| `too run FILE --help` | Show Script help and available runnables. |
| `too run FILE RUNNABLE --help` | Show that runnable's help. |
| `too serve TARGET` / `too TARGET serve` | Perform the former public `run` behavior. |
| `too _serve AGENT` | Retain the internal AgentServer entry point. |

### Initialization

- Require an explicit directory, not a filename. Omitting it shows help and
  creates nothing; `too init .` selects the current directory. Resolve it at the
  CLI boundary and create missing directories. Permit nonempty directories;
  preserve neighboring files.
- Load the bundled template before creating directories. Write `main.too` as
  UTF-8 with a trailing newline using exclusive creation. Existing files,
  directories, and symlinks at the destination must fail without being changed.
  Report invalid and unwritable destinations clearly.
- Use the existing catalog template mechanism with `script.default.too`:

```too
## Write a short greeting.
agic():
  Say hello to someone trying Toolang for the first time.
  Keep the greeting to one sentence.
```

- Print the created path and a command usable from the caller's current directory,
  quoting paths correctly and preserving the invoked executable name.
- Create no agent registration, configuration, credentials, runtime cache, or
  executable permission. Initialization and help require no model or network.
  Running the greeting requires a configured model; `--model` remains available.
- Filename options, template selection, overwrite flags, credential setup, and
  offline demo flows are outside scope.

### State Binding and Default Entries

- AST `AgicDecl.name` and `FlowDecl.name` preserve omitted names as `None`.
  Parsing, formatting, and serialized AST round trips retain this distinction.
- State binds unnamed declarations locally as `main` and creates the runnable
  indexes without mutating the AST. The agent/Script module exposes its local
  names publicly. Explicit `agic main` and `flow main` select identically.
- State also owns filename-derived exports: the unnamed flow in
  `flows/research.too` is publicly `research`, locally indexed as `main`, and
  remains unnamed in the AST. Renaming the file changes only its module/public
  identity. Named flow exports still require the filename stem to match.
- Agics and flows share one namespace. State rejects duplicate local or public
  bindings, including unnamed/explicit `main` conflicts across either kind.
  Named helpers may coexist. Script help uses the same State binding rules
  without preparing runtime State and rejects the same local conflicts.
- Do not infer a Script default from declaration order, filename, or a sole
  differently named runnable. Explicit authored names remain unchanged.
- Preserve the runtime `agic:default` fallback. An authored agic, flow, or exported
  flow named `default` takes precedence over the synthetic public entry.
  Script exposes authored declarations only and hides generated names beginning
  with `<`; it never selects the synthetic fallback.
- Explicit selections win. Otherwise, Chat tries `chat`, then authored `main`,
  then `default`; jobs try their kind (`task` or `chore`), then `main`, then
  `default`. Preserve the selected kind and public name, including flow exports.
- Chat's status bar retains the existing qualified form, such as `agic:chat`,
  `agic:main`, or `flow:main`, including active-run and session-default behavior
  during child runs and handoffs.
- Generic `:runnable default` continues resetting to the surface-selected entry,
  including a named Script entry. It does not force `main`. Model, configuration,
  reset, and template vocabulary keeps its existing `default` spelling.
- State passes unchanged authored text to `Program.from_source`. It must not strip
  legacy `agent NAME` headers or recognize alternative syntax. Missing optional
  source is empty text. Both `agent NAME` and `agent:` fail in the language parser.
- Advance the State layer schema so current preparation reparses older caches
  with strict syntax and unnamed AST declarations. Subsequent unchanged preparation
  reuses the current cache without parsing. Exact historical loads preserve
  recorded Programs, names, exports, and run identities; do not rewrite history.
  Retain `FlowDecl.name_explicit` to decode historical unnamed flow exports whose
  stored AST already used `main`.

### Script Arguments and Help

- `run` accepts only a local `.too` file. Report invalid extensions, URLs, and
  directories with specific file errors without suggesting `serve`. Missing
  files retain Script file-error behavior. Do not discover an implicit `main.too`.
- Reuse the existing Script execution path, options, input binding, sandbox
  execution, output, progress, and exit codes. Show the actual invocation spelling
  in help and errors. Keep path-first invocation and existing shebangs.
- Resolve explicit `run` before agent routing. Preserve every token after FILE,
  including command-like runnable names, option values, colon overrides, and `--`.
  Support the native `--` boundary before FILE, including dash-prefixed filenames.
- Reject global `--root` / `-r` overrides as in existing Script invocation. Explicit
  `run` has no target-first alias. `too AGENT run` must not start an agent and must
  point callers to `serve`; `too FILE run` must also identify explicit Script
  invocation when a runnable name collides with the former lifecycle command.
- Preserve path-first precedence for supported target-first commands. Explicit
  `too run FILE RUNNABLE` can invoke names such as `run`, `serve`, and `init`.
- Without a selector, execute authored `main` with its declared signature. Missing
  required inputs show runnable help and exit 2. With no main and no invocation
  arguments, show file help; input/options without an entry produce a usage error.
- A bare token in selector position remains a name, so misspellings fail. Default
  primary text uses `--`, `-`, or redirected stdin. For example:

```text
too run main.too --model MODEL topic=demo -- "text"
too run main.too review -- "text"
```

- Help never executes, prepares State, or reads stdin. File help identifies `main`
  when present and makes RUNNABLE optional only then. Static help explains FILE,
  RUNNABLE, forwarded arguments, and where to request Script-specific help.
  Use `[ARGUMENTS]` without an ellipsis, `Path to a .too file`, `Runnable name`,
  and `Runnable-specific arguments`. Display `[default: main]` using native
  argument metadata; file help shows RUNNABLE in Arguments, required if main is
  absent. Explain the `too FILE [RUNNABLE] [ARGUMENTS]` shorthand using the actual
  executable name. Command summaries are `Serve an agent in the foreground`,
  `Initialize Toolang in a directory`, and `Execute a runnable from a .too file`.
  Root help starts with `Toolang is a language and runtime for agents and humans.`
  followed by the actual source version in parentheses; dim the version and
  parentheses together.
- Replace `run` with `serve` in Agent Commands, after `info` and before `start`.
  Append Script Commands (`init`, then `run`) after Inspection Commands. Keep
  `_serve` and `channel` callable but absent from both root and `too hidden` lists.
  Move `compact` from Control Commands to `too hidden`, preserving its invocation.
- Preserve foreground target forms, lifecycle flags, defaults, preparation,
  logging, sandbox handling, Ctrl+C cleanup, and background `start`/`stop` behavior.
  Retain #529's `_serve` launch construction, guest validation, and legacy process
  recognition; public `serve --help` must not validate the internal entry point.

### Shared Runnable Documentation

- Consume `Runnable.doc` as CLI description and model trigger description for
  named/unnamed agics, flows, and exports. Runnable query `description` also uses
  this field. Prompts, `instruct` references, and flow statements remain separate.
- Consume `Parameter.doc` in Script argument help, including primary `_` input.
  Add `documentation` to each existing primary-input/parameter object in
  `runnable_signature`, shared by route snapshots and input-validation feedback;
  use an empty string when absent.
- Preserve names, types, optionality, output types, and reachable structs. Keep
  route limits of 64 targets and 32,768 bytes; reject oversized snapshots using
  the existing diagnostics. Limit each documentation string to 512 code points.
  CLI help retains complete text.
- Documentation does not authorize routes. Keep existing `hands` child-run and
  `handoffs` transfer authority, generic runtime tools, and captured-State refresh.
- Comment markers, attachment rules, parameter-comment grammar, and comment
  parsing/formatting changes belong exclusively to the separate comment task.

## Implementation Boundaries

Touch the existing CLI registry/routing and Script factory, add the init callback
and bundled template, preserve authored names in language lowering, and bind them
through State's public/module/query indexes. Update execution fallback selection,
runnable identity use, documentation projections, cache rebuilding, and directly
affected tests and user documentation.

Do not add an execution protocol, provider/plugin behavior, alternative parser,
new lifecycle flags, persisted runnable index, or historical data migration.
The existing source cache schema transition is the only storage change.

## Acceptance Checks

1. Initialization without a directory only shows help. Explicit initialization
   covers current, nonempty, nested, spaced, and Unicode paths;
   concurrent calls have one winner; existing files/symlinks and invalid or
   unwritable destinations fail safely. Verify the packaged template.
2. Explicit and shorthand Script invocations execute the template with an offline
   model fixture and preserve input, named parameters, options, `--`, stdin,
   colon overrides, local/remote results, and exit status. Cover command-like
   runnable names, unsupported targets, root overrides, and exact filenames.
3. Verify actual help for both executable names at narrow and normal widths:
   root, init, run, file, runnable, serve, target-specific, and hidden help.
   Check descriptions, required markers, panel order, usage, and no execution.
4. Cover unnamed/named `main` in both kinds, State conflict diagnostics, AST
   serialization, module/public indexes, renamed flow exports, and no inferred
   default for other names. Authored `default` must remain callable without a
   synthetic collision. Script exposes no synthetic runnable.
5. Verify Chat/task/chore fallback order and selected public kind/name, including
   local/remote execution and Chat status. Cover generic default reset, actual
   authorized child calls/handoffs, unnamed-agic reload, and inline agic execution.
6. Verify old-cache rebuilding, strict rejection of legacy headers, current-cache
   reuse, and exact historical unnamed-agic/flow Programs and exports.
7. Compare Script help, route snapshots, queries, and input contracts for the same
   named/unnamed declarations. Cover documentation limits and unchanged authority.
8. Retain hosting, sandbox, server-argv, guest-validation, process-discovery, and
   cleanup checks. Run `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run ty check`, and the full offline `uv run pytest` suite.

## Risks

The public `run`/`serve` transition is intentionally breaking; update maintained
hosting examples and direct old invocations to `serve`. Unnamed agics move from
the old public `default` identity to `main`; files with colliding entries must name
one explicitly. Preserve historical identities and avoid global replacements of
`default`. Query filters based on the former instruction-reference description
will change; parameter documentation contributes to the existing route byte limit.

No open product decisions remain within this scope.

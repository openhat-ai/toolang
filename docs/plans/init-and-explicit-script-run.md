# Initialize Scripts and Unify Runnable Entry Points and Documentation

Status: Approved for implementation on 2026-09-13. The human confirmed the
shared `main` entry in both contexts and requested implementation of this
contract. Script commands use the final visible group. Comment syntax changes
remain owned by a separate task. The human also required State to reject
legacy headers and delegate unchanged source to the language package.

## Goal and Success Criteria

Let users create a small, editable script with `too init [DIRECTORY]`, then
execute it with `too run main.too`. Use `serve` for foreground agent
hosting and `_serve` for the internal server entry point. `toolang` and `too`
remain equivalent executables.

Success means initialization writes only one source file, both unnamed runnable
kinds work as Script defaults, named Script behavior is retained, and all agent
launch paths use the renamed internal entry point without changing lifecycle
behavior. One authored documentation source describes each runnable and its
parameters to both CLI users and models selecting authorized routes.

## Current Behavior

- `main.py` routes a leading local `.too` path to dynamic Script commands;
  `script.dispatch` lists public runnables when no runnable is supplied.
- Public `run` starts a foreground agent on the host or in a selected sandbox.
  It accepts resident names, local `.too` files, and remote references or URLs,
  with the target before or after the command.
- Hidden `_serve` directly starts an AgentServer. PR #529 already updated
  command registration, host/Docker startup, guest validation, and help.
  Process discovery also recognizes legacy `serve` processes.
- `new` creates a resident agent from a bundled template. Its default template
  contains only comments, so it does not provide a Script runnable to try.
- An unnamed agic lowers to `default`, which Script filters out. An unnamed
  flow lowers to `main`, which Script exposes. Both can currently coexist.
- State supplies an implicit `agic:default` when no authored default agic exists.
  Non-Script surfaces use it as a fallback; this runtime role is distinct from
  the proposed authored `main` entry.
- CLI help can render `Parameter.doc`, but source lowering never fills it.
  Route catalogs already include `Runnable.doc`; their input/parameter entries
  omit documentation. Runnable queries currently use an agic's `instruct`
  reference as `description` and provide no flow description.

These observations are verified against implementation at `ba894e5b`, after
[PR #529](https://github.com/openhat-ai/toolang/pull/529) merged. The human
confirmed `main` as the shared name in both Script and Server contexts.

## Proposed Command Contract

| Command | Behavior |
| --- | --- |
| `too init` | Create `./main.too` from the bundled Script template. |
| `too init demo` | Create `demo/main.too`, creating missing directories. |
| `too run main.too` | Execute the authored default, or show help if absent. |
| `too run main.too main` | Explicitly execute the authored default. |
| `too run main.too review` | Execute a named runnable once. |
| `too main.too` / `too main.too review` | Equivalent path-first shorthand. |
| `too run main.too --help` | Show help without executing or reading stdin. |
| `too serve TARGET` / `too TARGET serve` | Perform the previous public `run` behavior. |
| `too _serve AGENT` | Keep the internal server behavior shipped in #529. |

### Initialization

- The optional positional argument is a directory, defaulting to the current
  directory. Resolve it at the CLI boundary. The output name is always
  `main.too`; no file-path overload, filename option, template selection, or
  overwrite flag is included in this scope.
- Permit existing nonempty directories. Fail with a clear error if the target
  directory is a file, the output already exists (including a symlink), or the
  directory cannot be written. Use exclusive creation so concurrent calls
  cannot overwrite a file. Existing neighboring files remain untouched.
- Load the bundled template before creating directories. Write UTF-8 with a
  trailing newline. Do not create an agent registration, runtime cache,
  configuration file, credentials, or executable permission.
- Print the created file path and a runnable command usable from the caller's
  current directory, quoting paths with spaces correctly. Initialization and
  help require no network, credentials, or runtime startup.
- Add `script.default.too` to the existing catalog template mechanism. Use the
  following content, with no name interpolation or remote dependencies:

```too
## Write a short greeting.
agic():
  Say hello to someone trying Toolang for the first time.
  Keep the greeting to one sentence.
```

The next command is `too run <created-path>`. The explicit empty signature `()`
requires no primary input. Execution uses the existing model selection and
credentials; users can pass `--model`. Document that a configured model is
required for the greeting to execute.

### Unnamed Runnables

- Lower both unnamed `agic` and unnamed `flow` to the canonical name `main`.
  Explicit `agic main` and `flow main` have the same default-entry meaning.
  Agics and flows continue sharing one runnable namespace: two `main` entries,
  including one of each kind, are a validation error. Named helpers may coexist.
- Do not infer defaults from the filename, declaration order, or the presence
  of only one differently named runnable. An unnamed agic changes from
  `default` to `main`; an unnamed flow keeps its existing `main` identity.
  Explicit authored names are not renamed. An authored `default` is a regular
  selectable runnable, not an alias for the `main` entry.
- Expose authored `main` in Script help and named dispatch. Support existing
  kind-qualified selectors such as `agic:main` and `flow:main`, enforcing
  the actual kind. Continue hiding generated inline runnables whose names begin
  with `<`. Do not expose the implicit runtime agic as a Script entry.
- Keep the existing runtime `agic:default` fallback identity and its injection
  rules; it does not collide with authored `main`. Script chooses only authored
  declarations and never falls back to this synthetic agic. Queries, publication,
  and local and remote execution must resolve the same authored `main`.
- Keep `FlowDecl.name_explicit` and filename-based flow-module exports: an
  unnamed flow in `flows/research.too` still exports `flow:research`, and its
  module-local name remains `main`. Formatting preserves omitted names.
- State passes unchanged authored text to `Program.from_source`; it neither
  strips legacy `agent NAME` headers nor supplies an alternative syntax. Missing
  optional source is represented by empty text. Both `agent NAME` and `agent:`
  headers are rejected with the language parser's diagnostics.
- Increment the State layer schema so current preparation rebuilds cached
  Programs with the new unnamed-agic identity and strict parsing. This also
  rejects legacy headers that were previously accepted into a cache. Subsequent
  unchanged preparation reuses the current layer without parsing. Preserve exact
  historical Programs and run identities; no historical files are rewritten.
- Other surfaces keep explicit selections first and their surface-specific
  fallback next, then try authored `main` before the existing runtime `default`
  fallback. For example, Chat uses `chat`, then `main`, then `default`; jobs use
  their kind, then `main`, then `default`. Resolve the actual kind of `main`,
  including when projecting the selected agic/flow in local and remote help.
  This preserves access to unnamed agics after their canonical name changes.
  `main` names the general entry; each surface owns its fallback order.
- The Chat status bar under the input shows the selected runnable using the
  existing kind-qualified format: `agic:chat` for a chat entry, and `agic:main`
  or `flow:main` when the authored generic entry is selected. Preserve active
  runnable and session-default display behavior during child runs and handoffs.
- Generic `:runnable default` continues resetting to the selected surface
  runnable, including a named Script command; it does not forcibly switch to
  `main`. Model/configuration/reset vocabulary and the bundled template name
  `script.default.too` keep their existing `default` spelling.

### Shared Runnable and Parameter Documentation

Comment markers, parameter-tag syntax, attachment/validation rules, parser and
formatter changes, and comment-specific cache compatibility belong to the
separate comment task. This plan does not select a replacement module-doc marker
or specify a parameter-comment grammar.

The Toolang-side integration below retains the earlier requirement to display
help and calling descriptions. It consumes metadata supplied by the separate
comment task, without duplicating its changes. The meaning is shared across
named/unnamed agics and flows and exported flow modules:
`Runnable.doc` describes what a runnable does and when to call it;
`Parameter.doc` describes an input. Consumers do not reparse comment text.

- Use `Runnable.doc` as both the trigger description and CLI description;
  executable prompts, `instruct` references, and flow statements remain separate.
- Script runnable lists and descriptions use `Runnable.doc`; Arguments help
  uses `Parameter.doc`, including `_`. File help identifies `main`, and explicit
  `main --help` presents the same signature and documentation as its shorthand.
- Runnable query `description` becomes `Runnable.doc` for both kinds, so route
  filtering and model-visible trigger descriptions share the authored meaning.
- Keep the existing authorized route catalog and generic runtime tools. Its
  runnable `documentation` contains `Runnable.doc`; add `documentation` to the
  existing primary-input object and each named-parameter object, using the
  corresponding `Parameter.doc` or an empty string. Use the same parameter
  projection in `runnable_input_contract` so input-error guidance agrees.
- Preserve names, types, optionality, output types, and reachable structs.
  Retain the catalog's 64-entry and 32,768-byte limits; cap each documentation
  string at 512 code points. CLI help keeps complete text. Documentation remains
  descriptive data and cannot authorize a route or override runtime rules.
  `hands` still authorizes returning child runs; `handoffs` still authorizes
  transferring the remainder of the run. State refresh updates both descriptions
  and parameter metadata through the existing captured-State behavior.

### Explicit Script Execution

- `run` takes a local `.too` file after the command, followed by existing Script
  arguments. Do not add resident-agent, URL, directory, or implicit `main.too`
  discovery to this command. Invalid target forms report a usage error; the
  message identifies `serve` as the foreground agent command. A missing `.too`
  file retains the Script file-error behavior.
- `too run` and `too run --help` display static Script entry-point usage.
  `too run FILE --help` shows dynamic script help and identifies the default,
  if any. `too run FILE RUNNABLE --help` shows runnable help. Help always exits
  without executing, preparing State, or consuming stdin.
- With no runnable selector, execute the authored `main`, retaining its
  signature checks. Required input that is absent still shows runnable help and
  exits with status 2. With no authored default and no invocation arguments,
  display script help; input or execution arguments without an entry report a
  usage error instead of selecting a helper.
- Keep bare positional tokens in the runnable-selection position as explicit
  selectors, so misspelled names remain errors. Default-entry primary text uses
  `too run FILE -- "text"`, or `-` / redirected stdin. Named `NAME=VALUE` input
  and shared Script options may precede that boundary. For example,
  `too run FILE --model MODEL topic=demo -- "text"` targets the default;
  `too run FILE review -- "text"` targets `review`. Do not guess whether an
  unknown bare token is a runnable name or prompt text.
- Resolve explicit `run` before agent target routing. After the source path,
  preserve every token for the Script parser, including runnable names matching
  top-level commands, option values, colon overrides, and `--` separators.
  Do not extract global options from this tail.
- Use the existing Script parser and execution path; retain its options, input
  binding, local and remote sandbox execution, output, progress, and exit codes.
  Show `too run FILE` in explicit-invocation usage and errors.
- Keep path-first execution and existing shebangs. Preserve its existing
  precedence for supported target-first commands; use explicit `run` to invoke
  a runnable whose name collides with a command. `too FILE run` is no longer a
  foreground lifecycle command and must direct callers to `too FILE serve` or
  `too run FILE RUNNABLE`, without starting a server.
- Explicit `run` has no target-first alias and rejects global `--root` / `-r`
  overrides just as Script invocation does today. `too AGENT run` must not
  start an agent and should identify `serve` as the replacement.

### Agent Hosting and Help

- Move public foreground `run` to `serve`, retaining its target forms, flags,
  defaults, preparation, logging, sandbox handling, and Ctrl+C behavior.
- Preserve the internal `_serve` command shipped in #529. Newly built server
  commands and Docker probes use `_serve` only; public `serve --help` must not
  validate the internal entry point.
- Preserve process recognition for `_serve` and existing
  legacy `serve AGENT` process under the same root and agent identity checks,
  so status and stop can handle processes started before an upgrade. This
  recognition does not retain an alias for launching the old internal command.
- Keep `start` and `stop` public behavior. Ensure every caller of the shared
  server argv builder, including Script and Chat sandbox startup, receives the
  `_serve` command.
- Replace `run` with `serve` at exactly its current position in `Agent Commands`:
  after `info` and before `start`. Keep all existing panels in their current
  order. Append a visible `Script Commands` panel containing `init`, then `run`,
  after `Inspection Commands`, as the final panel. This uses the requested
  separate-group option and keeps the quick-start commands discoverable.
- Keep `_serve` in hidden-command help. `init` and `run` are
  visible only in their Script panel and are not duplicated in `too hidden`.
  Target-specific help offers `serve`, not Script `init` or `run`.

## Scope and Implementation Touchpoints

Implementation is limited to the command contract above and its direct callers:

- `src/toolang/cli/toolang/main.py` and `routing.py`: lazy registration, command
  grammar, explicit Script dispatch, and help panels. Keep registry validation
  and existing command factories; do not introduce a second parser.
- `src/toolang/cli/toolang/commands/init.py` (new) and
  `src/toolang/catalog/templates/{__init__.py,script.default.too}`: initialization
  and the packaged template, reusing the existing template loader.
- `src/toolang/cli/toolang/commands/script.py`: shared invocation and accurate
  explicit usage, authored-default dispatch, and help. Keep source parsing in
  `toolang.lang`.
- `src/toolang/lang/lower.py` and directly affected validation/format tests:
  unnamed agic/flow naming and shared namespace rules only. Comment parsing and
  formatting changes belong to the separate task.
- `src/toolang/state/source.py` and `cache.py`: pass source unchanged to the
  language parser and advance the layer schema. Verify preparation, State
  injection, queries, flow exports, and historical loading.
- `src/toolang/execution/calls.py`, `src/toolang/execution/runnables.py`,
  `src/toolang/work/scheduler.py`, and
  their direct surface callers: prefer authored `main` before the existing
  runtime fallback and project its actual kind. Add shared parameter documentation
  to route catalogs and input contracts. Keep reset-token semantics.
- `src/toolang/state/runnable_collections.py`: use declaration documentation
  for both kinds' query descriptions. Verify existing CLI help consumes the
  parsed parameter docs via `src/toolang/cli/common/runnable_parameters.py`.
- `src/toolang/cli/toolang/commands/runtime.py` and
  `src/toolang/cli/common/routing.py`: foreground and internal CLI bindings.
- Existing internal argv, Docker validation, and process-recognition tests
  from #529 remain regression coverage; no second internal rename is required.
- Corresponding CLI, template, server, process, sandbox, and entry-point tests;
  `README.md`, `docs/program.md`, `docs/call-input.md`, `docs/agent-state.md`, and
  directly affected maintained command examples. Parameter-comment syntax
  documentation belongs to the separate task. Historical plans stay historical.
  No changes to input types, execution protocols, persistence formats, providers, plugin
  contracts, or public lifecycle flags are included.

## Acceptance Tests

1. `init` in the current, existing nonempty, and missing nested directory creates
   exactly the specified template. Spaces and Unicode in directory names work.
   Existing files and symlinks cannot be overwritten; invalid or unwritable
   destinations fail clearly. No agent or runtime artifacts are created.
2. The packaged template loads normally, parses with `Program.from_source`, and
   requires no input. An offline scripted model fixture executes it through
   `too run FILE` and path-first shorthand; `main` appears in help.
3. Explicit and shorthand execution deliver equivalent Script input, options,
   results, and exit status. Cover options before and after the runnable,
   `--model`, bare and valued `--dev`, `--out`, colon overrides, stdin, and `--`.
4. Explicit `run` can execute runnables named `run`, `serve`, or `init` without
   invoking lifecycle commands. Missing files, missing runnables, unsupported
   targets, and global root overrides have the specified diagnostics.
5. Static and dynamic help work offline, preserve lazy execution imports, and
   show the correct invocation spelling. Registry names and target grammar
   agree. Root, target-specific, and hidden help have the specified visibility.
6. `serve` retains former `run` coverage for resident, roaming, and visiting
   targets in both supported positions; foreground exit and cleanup behavior,
   lifecycle flags, and background `start` / `stop` remain covered.
7. All host and Docker server launch paths and guest validation use `_serve`.
   Process matching accepts new and legacy internal commands only with the
   matching root and agent. Mismatched identities remain rejected.
8. Both installed executable names expose the same contract. Validate bundled
   template inclusion and directly affected user examples. Run the default
   offline checks: `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run ty check`, and `uv run pytest`.
9. Unnamed agics and flows lower to `main`; explicitly named `main` entries have
   identical selection behavior. Duplicate `main` entries fail across kinds.
   A sole differently named helper is never inferred as the default. Source
   formatting and serialized AST round trips preserve unnamed flow metadata.
10. Exercise default execution with no arguments, named input, `--` primary
    input, stdin, missing required input, shared options, and explicit selectors.
    Unknown names remain errors. File help never executes or reads stdin.
    Named helper execution and generic `:runnable default` reset remain covered.
11. Authored `main` of either kind publishes and executes alongside the runtime
    fallback. Other surfaces prefer their specific entry, then `main`, then the
    existing fallback, preserving explicit choices and actual kind. Script
    exposes no synthetic entry. Cover local/remote runs, flow-module exports,
    and the actual selected kind/name in the Chat status bar.
12. Unchanged source backed by an old unnamed-agic home layer is reparsed on
    preparation and publishes `main`. Legacy headers fail through the language
    parser on fresh preparation and when rebuilding an older cache. Cached
    explicit `agic default` remains reusable after the schema upgrade. Unnamed
    flows keep their existing `main` identity and filename-derived exports.
    Historical loading retains recorded names and
    run references.
13. For the same authored runnable, compare Script help with `hands` and
    `handoffs` catalogs: trigger text, parameter descriptions, types, requiredness,
    and canonical/exported identity agree. Verify query descriptions, input-error
    contracts, changed-State refresh, text limits, and unchanged route authority.
    Use offline fixtures for both child-run and transfer execution paths.

## Risks and Tradeoffs

- Repurposing `run` and `serve` is intentionally breaking. Update foreground
  examples from `run` to `serve` in the same implementation; do not overload
  `run` by target type or retain the old internal behavior under public `serve`.
- A missed internal caller could recursively launch the public foreground
  command or validate an incompatible guest package. Test command construction
  and guest checks together; mixed-version new-host/old-guest launches fail
  validation rather than falling back to `serve`.
- New command names reserve additional path-first tokens. The explicit Script
  entry point provides access to colliding runnable names. Existing agents
  named after commands remain addressable through explicit `agent:` selectors.
- A fixed, model-neutral template keeps initialization small but requires an
  already configured provider or local model for execution. Credential setup,
  offline demo flows, and a template gallery are outside this scope.
- Unnamed agic naming changes authored references and introduces `main` into
  other surfaces' fallback order. Document the rename and duplicate-`main` rule.
  Existing files with both unnamed kinds must name one explicitly. Cached source
  must use current lowering for new runs; historical revisions retain recorded
  meaning. A global replacement of `default` would corrupt unrelated reset,
  model, template, and configuration semantics and is outside this scope.
- Correcting query `description` changes filters that relied on instruction
  references.
  Adding parameter docs can reduce the number of complete route entries that
  fit the existing byte budget; omission reporting must remain deterministic.

## Open Questions and Approval

The human confirmed shorthand retention, unified `main` semantics, and
`serve` retaining the former `run` help position. A final visible Script panel
was selected from the two permitted placements. Comment changes are handled in
a separate task. The working scope retains the earlier requested Toolang-side
documentation consumption and excludes comment syntax and parsing changes.
The approved defaults are fixed `main.too`, one zero-input unnamed agic,
no overwrite or template-selection flags, and the explicit input boundary for
default-entry text. Comment syntax is outside this definition.

# Consolidate current documentation

Status: proposed; requires human approval before implementation.

## Goal and success criteria

Make `docs/` a concise, implementation-accurate reference for users and
developers. Give each topic one authoritative document, remove obsolete design
material from current reading paths, and use current terms consistently.
Readers should find an answer through the index without comparing overlapping
specifications or interpreting historical terminology.

The proposed inventory has **24 substantive documents**, down from 33 including
the knowledge base in [PR #681](https://github.com/openhat-ai/toolang/pull/681).
This is an ownership map, not a file-count optimization: preserve unique,
necessary explanations and examples while removing duplication.

## Scope and baseline

- Consolidate Markdown under `docs/`, excluding existing `docs/plans/**`.
  This plan is the only file created during definition.
- Baseline: `origin/main` at `0be39fe1` (0.3.6), verified on 2026-10-04. PR #681 supplies
  a reviewed-in-session knowledge map and factual corrections; it is still open
  and is evidence, not proof that its changes are on main. Reconcile its status
  at implementation start and retain or incorporate its verified corrections
  without maintaining two competing overviews.
- Keep documentation in English. Change no source code, tests, CLI behavior,
  README, changelog, generated `reference/`, external documentation site, or
  files under `archive/`, `dist/`, and `scratch/`.
- Existing plans remain historical records. Do not rewrite, relocate, or sweep
  their terminology. Do not create a second archive or limitations document.
- Use a dedicated worktree and a documentation-only PR. This plan does not
  authorize implementing product features described by old documentation.

## Reading structure

Keep existing canonical filenames where practical. Organize `index.md` by
reader task rather than creating a parallel directory hierarchy:

- **Use Toolang:** CLI and scripts → Chat → tasks/chores → models, tools, caps.
- **Write Toolang:** program/signatures → flow syntax → input → authoring style
  and source commands. Queries are shared reference material.
- **Develop or integrate:** knowledge base → layout and prepared State →
  execution/records and scheduling → HTTP API, plugins and presentation →
  development workflow.

The index contains navigation and short descriptions. The knowledge base owns
the concise architecture and glossary, with links to detailed owners. Each
guide starts with its audience, purpose and prerequisites; examples precede
internal details. Put existing plans in a clearly labeled historical section.

## Document disposition

“Merge” means extract verified unique content into the named owner, update
active links, then remove the old document. It never means concatenate files.
The table covers every current document outside `plans/`, including PR #681.

| Current document | Decision and authoritative content |
| --- | --- |
| `index.md` | Keep; provide the three reading paths and a compact document map. |
| `knowledge-base.md` | Keep from #681; own architecture, glossary, feature boundaries, source/test navigation and verification baseline. |
| `concepts.md` | Merge unique definitions into `knowledge-base.md`; move detailed rules to their topic owners. |
| `program.md` | Keep; declarations, modules, runnable signatures, structs and instruction composition. Link to flow, cap and input details. |
| `flow-syntax.md` | Keep; supported statements, bindings and collection semantics. Remove migration essays; state current bridge requirements accurately. |
| `call-input.md` | Keep; canonical flat input, Content evaluation, prompt expansion, coercion and absent/empty/null distinctions. |
| `input-syntax.md` | Merge shared input semantics into `call-input.md`; move Chat-only settings and interaction to `chat.md`. |
| `toolang-authoring-conventions.md` | Keep; writing style and documentation comments only. Link to syntax and input contracts instead of repeating them. |
| `script-projects.md` | Keep; script setup, configuration discovery, placement, workspace and attachment resolution. |
| `source-commands.md` | Keep; parse/check, format, highlight and interpreting diagnostics. |
| `source-diagnostics-examples.md` | Merge a few representative diagnostics into `source-commands.md`; link to tests for the exhaustive error matrix. |
| `caps.md` | Keep; cap kinds, forms, scopes, origins, precedence and authoring. Move endpoint descriptions to `api.md`. |
| `queries.md` | Keep; shared query semantics and representative selectors. Other guides link here. |
| `models.md` | Keep; catalog selection, readiness, routes, model configuration and accounting semantics. Plugin contracts belong in `plugins.md`. |
| `tools.md` | Keep; built-in tool behavior, workspace authority, service/history/me operations. Shared plugin interfaces belong in `plugins.md`. |
| `plugins.md` | Keep; integration families, contracts, registration/factories and configuration. Avoid duplicating built-in usage. |
| `tasks.md` | Keep; authoring and managing tasks/chores, stages and stable identity. Link to scheduler behavior and HTTP operations. |
| `work.md` | Keep; scheduler ownership, checkpoints, claims, RRULE processing and recovery. Do not repeat authored file formats. |
| `webui-jobs.md` | Merge API consumer workflow and derived phase rules into `api.md`; authored concepts link to `tasks.md`. Remove speculative UI design. |
| `layout.md` | Keep; placement, authored/generated paths, storage ownership and sandbox control files. Revision algorithms belong in `agent-state.md`. |
| `agent-state.md` | Keep; preparation, immutable publications, module binding, revisions, refresh and last-valid-State behavior. |
| `execution.md` | Keep; acceptance, binding, run/step lifecycle, control timing, history, retry/rerun and executor invariants. |
| `executor.md` | Merge verified invariants into `execution.md`; replace copied Python signatures with source links. |
| `run-step-records.md` | Keep; durable references, record/projection distinctions, output provenance and current persistence compatibility. |
| `ids.md` | Merge public ID/reference rules into `run-step-records.md`, job identity into `tasks.md`; link to allocator code for private encoding details. |
| `chat.md` | Keep; user interaction, session versus run settings, threads, local/remote behavior and recovery. Link to API and shared presentation rules. |
| `execution-presentation.md` | Keep; current shared output behavior and projection boundaries. Reduce exhaustive screen permutations to representative examples and test links. |
| `chat-tui-execution-presentation-draft.md` | Remove after transferring any unique, implemented behavior to `chat.md` or `execution-presentation.md`. Do not transfer proposals. |
| `execution-transcript.md` | Remove the obsolete compatibility page and retired presentation vocabulary; use `execution-presentation.md`. |
| `api.md` | Keep HTTP-only contracts and integration workflows; extract CLI content to new `cli.md`, with detailed script/Chat usage owned by their guides. |
| `refactor-target.md` | Remove; verified current ownership belongs in the knowledge base and development guide. Git history retains the proposal. |
| `package-audit.md` | Merge repository-specific review rules into new `development.md`; remove generic audit prose. |
| `package-testing.md` | Merge test layers, focused/default verification and opt-in live checks into `development.md`. |

New `cli.md` owns command discovery, selector/routing rules and representative
administrative workflows; `too --help`, `too more` and command help remain the
source for exhaustive flags. New `development.md` owns contribution navigation,
package boundary guidance, review and verification, linking to `AGENTS.md` and
tests rather than maintaining competing policy.

## Terminology and content rules

Use code definitions and behavior tests to resolve terms. Do not run blind
search-and-replace: several confusing names still describe valid distinctions.

| Area | Required treatment |
| --- | --- |
| `Percept`, `PerceptPart`, `MessagePart` | Remove obsolete type names from current explanations. Describe current `Part` variants and actual message-role restrictions. |
| `AgentRuntime`, `ExecutionRuntime` | Replace stale architectural entities with the actual owner: `AgentCore`, server acquisition, `AgentServerRef`, `RunClient`, or executor as appropriate. |
| Local and value shape | Distinguish executor-internal `none/item/list` from durable `Local(value, dim)` and caller-facing derived `type`. Array-valued items are not automatically flow collections. |
| Input vocabulary | A parameter is declared; an argument is supplied. `_` is primary input. Keep the flat `CallInput` contract and distinguish omission, empty input and null. |
| Agent resources | Distinguish placement from sandbox, cap scope from form/origin, Setup from State, and selection/defaults from quantity limits. |
| Models | Provider is a record; catalog discovers/describes; adapter executes a protocol. Preserve legitimate concrete type names such as `ModelProvider` when describing their actual role. |
| Work and execution | Distinguish authored stage, scheduler status, run status and derived UI phase. Jobs define work; Runs execute it. Retry retains a Run; rerun creates one and is not a separate Control kind. |
| Revisions and continuation | Separate published State revisions, record `state` references, model `cont`, and captured versus later named-call bindings. |
| Legacy behavior | Remove obsolete `rank` syntax, old presentation markers and stale compatibility aliases. Keep necessary current compatibility rules; link historical migrations to the changelog or Git history. |

Each rule, schema statement and detailed example has one owner. Other pages may
give a one-sentence orientation and a link, not another normative description.
Keep schema compatibility in `run-step-records.md`; derive exact fields from
code instead of copying large type definitions. The knowledge base may record
a versioned snapshot but must link to the current owner.

State implementation boundaries next to the relevant feature. In particular,
do not advertise `ask`/`seek` bridges, channel polling or hook routes merely
because syntax, plugins or historical descriptions exist. Validate these
boundaries again against the implementation baseline.

## Ordered implementation

- [ ] Capture latest main, reconcile #681, and create one inventory of headings,
  links, terms and source/test owners. Reuse the knowledge map; inspect only
  changes since its baseline and unresolved claims.
- [ ] Rewrite the index and knowledge base around the approved reader paths and
  glossary. Establish topic ownership before moving detailed material.
- [ ] Consolidate language/input/source guides and CLI/Chat/script usage;
  validate examples against the parser, templates and command routing.
- [ ] Consolidate runtime, records, scheduling, resources and integration
  material; verify behavior against the corresponding packages and tests.
- [ ] Extract unique implemented material from drafts, merge developer workflow
  guidance, remove superseded pages and repair active links and anchors.
- [ ] Perform a reader-path and terminology review, then run the documentation
  acceptance checks below. Inspect each removed section for lost necessary
  information before committing.
- [ ] Fetch/rebase onto latest main, recheck affected evidence and links, then
  submit the documentation-only PR with the disposition and verification
  summary. Leave approval and merge decisions to the maintainer.

## Acceptance checks

1. Every inventory entry has its declared destination; the index reaches all
   substantive documents and offers usable user and developer starting paths.
   No current guide presents a draft or target architecture as implemented.
2. Each retained document has one purpose and one owner for its detailed rules.
   No duplicate glossary, API schema, CLI flag catalog or lifecycle definition
   competes with that owner. Merges reduce repetition rather than just length.
3. Search current docs for the terminology above and review every match in
   context. Deprecated runtime type names and commands do not remain as current
   concepts; valid internal names are not accidentally renamed.
4. Validate relative links and anchors across retained docs, and inspect inbound
   links from the repository, including frozen plans. The baseline has no
   Markdown backlinks from plans to the proposed removed files. Historical
   plain-text mentions may remain. If a new immutable backlink is found, keep
   only a minimal destination pointer with any required anchors rather than
   modifying an excluded file;
   document why that pointer is necessary. Do not add speculative redirects.
5. Check CLI examples through offline help and routing; parse/check `.too`
   examples with current language tools. Compare HTTP paths, request/response
   fields and durable record descriptions with actual routes and schema types.
   Replace captured error/output dumps with representative checked examples.
6. Record source/test evidence for nontrivial behavior, including fresh named
   binding versus pinned inline code, typed input, control timing, workspace
   authority and scheduler recovery. Use existing focused offline tests when
   execution is needed; no live-provider or Docker calls are required.
7. Run `git diff --check`; confirm the implementation diff changes only approved
   documentation. Full lint/type/test checks are not required for documentation
   changes under the repository rules. Never infer verification from a plan.

## Risks and decisions

- **Concurrent code/doc changes:** record the reviewed revision and update only
  affected owners after rebasing. Treat #681 and earlier plans as evidence to
  reconcile, not a second authority.
- **Lost details or broken links:** move verified unique content before deleting
  a file, track old headings to destinations, and check inbound links. Preserve
  existing changelog records and excluded plans unchanged.
- **New oversized documents:** keep procedural usage separate from runtime
  invariants, use focused sections, and link to source/tests for implementation
  detail. Do not reproduce removed documents as giant appendices.
- **Over-cleaned vocabulary:** retain genuine distinctions and actual code
  identifiers; remove obsolete meanings, not ordinary English words globally.

No unresolved design questions. Human approval of this scope and disposition is
required before implementation; this proposal changes no existing guide.

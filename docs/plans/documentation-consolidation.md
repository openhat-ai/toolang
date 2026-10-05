# Consolidate current documentation

Flow syntax and value rules in this historical plan are superseded by
[Flow Array Semantics](flow-array-semantics.md).

Status: documentation ownership and reader split confirmed on 2026-10-04;
implementation remains a separate step.

## Goal and success criteria

Make `docs/` a concise, implementation-accurate reference for **Toolang's own
maintainers and contributors**. Help them understand ownership, preserve
behavioral contracts, locate implementation/tests, and change the runtime
safely. Give each topic one authoritative document, remove obsolete design
material from current reading paths, and use current terms consistently.

Extend the reader split in [the documentation index](../index.md) to all three
repositories: `tree-sitter-toolang` owns syntax/CST contracts, `toolang` owns
language/runtime semantics, and `toolang-docs` owns user learning and authoring
guidance. API and plugin implementers are secondary readers of repository docs.
Keep short examples that explain or verify a contract, not parallel tutorials.

The target inventory has **23 substantive current guides**, down from 33 including
the knowledge base in [PR #681](https://github.com/openhat-ai/toolang/pull/681).
Historical evaluation evidence and a required compatibility pointer are counted
separately.
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
- Do not recreate installation, model-provider setup, first-script, Chat usage,
  or task-management tutorials here. Link to the website for product usage;
  keep observable behavior and implementation constraints in repository docs.
  Grammar-repository and website changes require separate scoped follow-ups.
  Record their source sections, destination files and completion evidence in
  the implementation PR; do not delete unique guidance before its destination
  is available. Independent local consolidation can proceed in the meantime.
- Existing plans remain historical records. Do not rewrite, relocate, or sweep
  their terminology. Do not create a second archive or limitations document.
  Preserve `docs/evaluations/web-search-2026-09-29/README.md` and its three result
  files as dated measurement evidence, not current product guarantees.
- Use a dedicated worktree and a documentation-only PR. This plan does not
  authorize implementing product features described by old documentation.

## Ownership across repositories

| Repository | Primary reader | Authoritative responsibility | Main outline |
| --- | --- | --- | --- |
| `tree-sitter-toolang` | Parser, editor and language-tool developers | `grammar.js` defines parsing; `GRAMMAR.md` explains syntax and public CST nodes/fields. Corpus tests, generated artifacts and queries verify that contract. | Installation/integration; grammar; CST/queries; parser development and compatibility. |
| `toolang` | Runtime maintainers and contributors | Language defaults, lowering, binding, validation, evaluation, formatting and runtime/integration contracts, verified against implementation/tests. | Index; architecture; development; language semantics; runtime/state/scheduling; plugins/CLI/API. |
| `toolang-docs` | Authors and users | User learning, recommended authoring style and published reference organized around usage. Syntax and behavioral claims inherit the upstream owners. | Getting started; guides; authoring conventions; language reference; CLI/API/configuration reference. |

The website remains the complete user-facing reading entry point. Its grammar
reference identifies the supported grammar version and links to the normative
syntax/CST reference; avoid a separately maintained full EBNF copy. Runtime docs
explain semantics with necessary syntax examples rather than restating the
parser specification. Short explanatory summaries may repeat facts with an
owner link; they must not establish competing rules.

The complete **Authoring Conventions** guide belongs in
`toolang-docs/docs/pages/docs/toolang-conventions.mdx`. Split the current Toolang
guide by responsibility:

- Naming, prose, when to omit types and useful comment writing: website guide.
- Type defaults, documentation attachment and parameter validation: `program.md`
  and the relevant flow/input semantic owners.
- Mechanical formatting, preservation and idempotence: `source-commands.md`.
- Comment markers, valid positions and CST fields: grammar reference.

Bundled Toolang templates/examples follow the website conventions; the developer
guide links there rather than creating a second style guide. Parser fixtures
cover all valid forms, including discouraged ones. Style advice does not change
syntax validity, runtime defaults or formatter behavior by implication.

For example, Tree-sitter parses `## @param NAME DESCRIPTION`; Toolang attaches
it, validates the parameter and exposes the description to consumers; the
website explains when to use it and how to write useful descriptions.

### Handoffs and version alignment

| Change | Required handoff |
| --- | --- |
| Syntax or public CST | Update grammar/reference, corpus, generated artifacts and affected queries; publish a compatible parser; integrate/test its consumers in Toolang; update website syntax reference, highlighter and examples. |
| Defaults or runtime semantics | Update Toolang implementation/tests and semantic owner; sync website behavior/examples. No parser change is required when syntax/CST are unchanged. |
| Authoring advice | Update the website guide; review affected bundled examples. Do not change parser acceptance or formatter behavior implicitly. |
| Formatter behavior | Update Toolang formatter contract/tests; sync website formatting advice when user guidance changes. |

These are maintenance handoffs, not product changes authorized by this plan.
Website reference generation and examples should share a recorded compatibility
baseline: Toolang release/commit plus a verified compatible grammar version.
Follow-ups must validate references, examples and highlighting against that
baseline; repository version numbers need not match. Retain supported legacy
syntax in parser compatibility notes without recommending it for new source.

Evidence inspected on 2026-10-04, to reuse rather than rescan:

- `tree-sitter-toolang` at `7477b10`: `GRAMMAR.md` includes runtime defaults and
  Model Call Assembly. Keep normative syntax/CST there; move runtime detail to
  Toolang's semantic owners and replace duplication with links.
- `toolang-docs` at `c35411b`: the existing conventions page already covers
  naming, prose, type omission and current documentation comments. Check only
  remaining differences before removing the local guide. The grammar page
  cites 0.3.3; `scripts/toolang-source.txt` separately pins Toolang at `02419769`.
  A website follow-up should record and verify their combined compatibility
  baseline and sync `docs/syntaxes/toolang.tmLanguage.ts` when needed.

## Reading structure

Keep existing canonical filenames where practical. Organize `index.md` around
maintainer questions rather than creating a parallel directory hierarchy:

- **Understand the codebase:** architecture → package ownership and source/test
  map → development workflow. Link end-user learning to `toolang-docs`.
- **Change language behavior:** program/signatures → flow and input semantics →
  source tooling → owning parser/runtime tests. Link upstream grammar and
  website authoring conventions at their respective boundaries.
- **Change runtime or integrations:** layout/Setup/State → execution, records and
  scheduling → CLI/Chat, HTTP, plugins and presentation boundaries.

Rename `knowledge-base.md` from #681 to **`architecture.md`**. The entire docs
collection is the knowledge base; this file has the narrower purpose of teaching
the system model. Keep these outlines distinct:

- **Index:** audience and repository boundaries; architecture/development entry
  points; subsystem document map; clearly labeled historical plans/evaluations.
- **Architecture:** system overview; core concepts and relationships; package
  responsibilities; main end-to-end flows; key invariants; concise source/test
  entry points and the reviewed baseline.

Keep document navigation in the index and detailed contracts in their topic
owners. Each document states its responsibility, contracts and relevant
implementation/test entry points. Use minimal examples to make invariants
concrete; avoid class/function catalogs covered by generated reference.

## Document disposition

“Merge” means extract verified unique content into the named owner, update
active links, then remove the old document. It never means concatenate files.
The table covers all 33 current top-level guides, including PR #681. The dated
evaluation README and data retain their separate evidence role described above.

| Current document | Decision and authoritative content |
| --- | --- |
| `index.md` | Keep; provide maintainer reading paths, the three-repository boundary and a compact document map. |
| `knowledge-base.md` | Rename to `architecture.md`; own the system model, glossary, package responsibilities, main flows, invariants and evidence entry points. Remove overlapping document navigation. |
| `concepts.md` | Merge unique definitions into `architecture.md`; move detailed rules to their topic owners. |
| `program.md` | Keep as program semantics; own declarations, modules, signatures/defaults, documentation binding, structs and instruction composition. Link normative syntax/CST upstream and detailed flow/cap/input rules to their owners. |
| `flow-syntax.md` | Keep the filename; present flow evaluation, bindings, collection shapes and statement contracts. Link grammar productions upstream; remove migration essays and state current bridge requirements accurately. |
| `call-input.md` | Keep; flat input, Content evaluation, prompt expansion, coercion, absence/null distinctions and shared colon RunOverride syntax. Policy resolution belongs in `execution.md`. |
| `input-syntax.md` | Merge input and shared colon syntax into `call-input.md`, policy precedence/ceilings into `execution.md`, and slash/session interaction into `chat.md`. |
| `toolang-authoring-conventions.md` | Split semantic rules into program/flow/input owners and formatter guarantees into `source-commands.md`; verify unique style guidance exists on the website, then replace the full guide with a minimal destination pointer for the existing frozen-plan backlink. Complete any missing website handoff first. |
| `script-projects.md` | Keep; script discovery, configuration precedence, placement, workspace and attachment resolution contracts. Remove first-script walkthroughs. |
| `source-commands.md` | Keep; parse/check, format and highlight contracts, diagnostic semantics, exit behavior and source/tooling test links. |
| `source-diagnostics-examples.md` | Merge a few representative diagnostics into `source-commands.md`; link to tests for the exhaustive error matrix. |
| `caps.md` | Keep; cap kinds, forms, scopes, origins, authored formats and precedence. Move endpoint descriptions to `api.md`; user workflows belong on the website. |
| `queries.md` | Keep; shared query semantics and representative selectors. Other guides link here. |
| `models.md` | Keep; catalog selection, readiness, routes, configuration interpretation, recovery and accounting semantics. Provider setup tutorials belong on the website; plugin contracts in `plugins.md`. |
| `tools.md` | Keep; built-in tool behavior, workspace authority, service/history/me operations. Shared plugin interfaces belong in `plugins.md`. |
| `plugins.md` | Keep; integration families, contracts, registration/factories and configuration. Avoid duplicating built-in usage. |
| `tasks.md` | Keep; authored task/chore formats, validation, stage transitions, stable identity and projections. User task-management walkthroughs belong on the website. |
| `work.md` | Keep; scheduler ownership, checkpoints, claims, RRULE processing and recovery. Do not repeat authored file formats. |
| `webui-jobs.md` | Merge API consumer workflow and derived phase rules into `api.md`; authored concepts link to `tasks.md`. Remove speculative UI design. |
| `layout.md` | Keep; placement, authored/generated paths, storage ownership and sandbox control files. Revision algorithms belong in `agent-state.md`. |
| `agent-state.md` | Keep; preparation, immutable publications, module binding, revisions, refresh and last-valid-State behavior. |
| `execution.md` | Keep; run-policy precedence, defaults/limits/ceilings, acceptance, binding, lifecycle, control timing, history, retry/rerun and executor invariants. |
| `executor.md` | Merge verified invariants into `execution.md`; replace copied Python signatures with source links. |
| `run-step-records.md` | Keep; durable references, record/projection distinctions, output provenance and current persistence compatibility. |
| `ids.md` | Merge public ID/reference rules into `run-step-records.md`, job identity into `tasks.md`; link to allocator code for private encoding details. |
| `chat.md` | Keep; Chat orchestration, slash/session state, queue/thread ownership, local/remote lifecycle and recovery contracts. Link to shared input, policy, API and presentation owners. |
| `execution-presentation.md` | Keep; current shared output behavior and projection boundaries. Reduce exhaustive screen permutations to representative examples and test links. |
| `chat-tui-execution-presentation-draft.md` | Remove after transferring any unique, implemented behavior to `chat.md` or `execution-presentation.md`. Do not transfer proposals. |
| `execution-transcript.md` | Remove the obsolete compatibility page and retired presentation vocabulary; use `execution-presentation.md`. |
| `api.md` | Keep HTTP contracts and integration constraints; move CLI orchestration to new `cli.md` and shared behavior to its owner. Remove end-user tutorials. |
| `refactor-target.md` | Remove; verified current ownership belongs in `architecture.md` and the development guide. Git history retains the proposal. |
| `package-audit.md` | Merge repository-specific review rules into new `development.md`; remove generic audit prose. |
| `package-testing.md` | Merge test layers, focused/default verification and opt-in live checks into `development.md`. |

New `cli.md` documents CLI implementation: entry points, command registration,
selector/routing rules, environment/default resolution, local/hosted acquisition
and presentation boundaries, with source/test links. It is not a command
cookbook; CLI help and the website own product usage. New `development.md` owns
contributor setup, package boundary guidance, review and verification, linking
to `AGENTS.md`, website authoring conventions and tests rather than maintaining
competing policy.

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
give a short explanation and a link, not another normative description.
Keep schema compatibility in `run-step-records.md`; derive exact fields from
code instead of copying large type definitions. The architecture guide may record
a versioned snapshot but must link to the current owner.

State implementation boundaries next to the relevant feature. In particular,
do not advertise `ask`/`seek` bridges, channel polling or hook routes merely
because syntax, plugins or historical descriptions exist. Validate these
boundaries again against the implementation baseline.

## Ordered implementation

- [ ] Capture latest main, reconcile #681, and create one inventory of headings,
  links, terms and source/test owners. Reuse the knowledge map; inspect only
  changes since its baseline and unresolved claims. Check sibling-repository
  deltas only for the grammar/conventions handoffs above.
- [ ] Rewrite the index and rename/refocus the knowledge base as `architecture.md`
  around the agreed outlines. Establish topic ownership before moving details.
- [ ] Consolidate language/input/source contracts and CLI/Chat/script
  orchestration. Preserve shared run-policy syntax separately from Chat-only
  session interaction. Split authoring guidance from semantics and formatting;
  verify website coverage or complete the separate handoff before replacing the
  local conventions guide with its pointer. Validate contract examples against
  their owners.
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
   substantive documents and states the three-repository audience/owner split.
   A new contributor can use it to locate (a) parser/input coercion and its
   tests, (b) Setup/State publication and accepted-run binding, and (c) CLI/API
   run acceptance and persistence. Each route leads to its document owner and
   implementation/test anchors without consulting a historical plan.
   No current guide presents a draft or target architecture as implemented.
   The index supplies reading paths; `architecture.md` teaches the system model
   without reproducing the document directory.
2. Each retained document has one purpose and one owner for its detailed rules.
   No duplicate glossary, API schema, CLI flag catalog or lifecycle definition
   competes with that owner. Shared colon override syntax is in `call-input.md`;
   policy precedence, defaults, ceilings and limit scope are in `execution.md`;
   Chat-only slash/session transitions are in `chat.md`. Merges reduce
   repetition rather than just length. Product tutorials are not recreated.
   Syntax/CST rules link upstream; semantic defaults/validation remain owned by
   Toolang; complete authoring conventions are owned by the website. Formatter
   guarantees remain distinct from optional style recommendations. All unique
   guidance has a verified destination before its source document is removed.
3. Search current docs for the terminology above and review every match in
   context. Deprecated runtime type names and commands do not remain as current
   concepts; valid internal names are not accidentally renamed.
4. Validate relative links and anchors across retained docs, and inspect inbound
   links from the repository, including frozen plans. The existing
   `docs/plans/source-developer-commands.md` links to the conventions guide;
   retain only a minimal pointer there to website style guidance and local
   semantic/formatter owners, with no duplicated rules. Recheck other removals
   and the architecture rename for new inbound links. Historical plain-text
   mentions may remain. Preserve any other immutable backlinks with minimal
   destination pointers and required anchors rather than modifying excluded
   files. Document each reason; do not add speculative redirects.
5. Classify examples before checking: complete `.too` programs must pass;
   partial examples require a stated minimal context; intentionally invalid
   samples must produce the expected failure/diagnostic. Mark schematic grammar
   notation as notation rather than runnable code. Check CLI contract examples
   through offline help/routing; compare HTTP and durable record descriptions
   with actual routes and schema types. Use representative checked outputs.
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
- **Cross-repository drift:** record upstream versions and source/destination
  owners. Missing website content blocks only the corresponding deletion; do
  independent local work while its separately scoped handoff is completed.
  Never treat an open follow-up as evidence that content has migrated.
- **New oversized documents:** keep end-user tutorials on `toolang-docs`, use
  focused contract/invariant sections, and link to source/tests for implementation
  detail. Do not reproduce removed documents as giant appendices.
- **Over-cleaned vocabulary:** retain genuine distinctions and actual code
  identifiers; remove obsolete meanings, not ordinary English words globally.

No unresolved ownership decisions. The agreed direction is recorded here;
this update changes only the plan and does not begin the documentation rewrite
or authorize product changes.

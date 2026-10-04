# Contributing to Toolang

This guide connects contributor workflows to the packages and contracts they
exercise. [AGENTS.md](AGENTS.md) owns repository policy and required checks;
[architecture](docs/architecture.md) explains package responsibilities.

## Working environment

Use Python 3.11+ and `uv` on macOS or Linux. In a dedicated Git worktree:

```sh
uv sync
uv run too --help
uv run pytest tests/unit/lang/test_program.py
```

`too` is an alias for `toolang`. Resolve environment, CLI defaults and filesystem
placement at call sites; pass concrete values into core modules. Keep package
facades narrow and behavior with its owning concept. Parsing and authored
schemas must not acquire runtime services, watchers or stores.

## Find the owner before editing

| Change | Start here | Verification anchor |
| --- | --- | --- |
| Syntax/CST | Upstream grammar repository; Toolang's [lang](src/toolang/lang/) consumes it | Upstream corpus and [language tests](tests/unit/lang/) |
| Binding/coercion | [lower.py](src/toolang/lang/lower.py), [input.py](src/toolang/lang/input.py) | [language tests](tests/unit/lang/), [flow scenarios](tests/integration/execution/test_flow_scenarios.py) |
| Setup/State visibility | [setup](src/toolang/setup/), [state](src/toolang/state/) | [latest binding](tests/integration/execution/test_latest_state_binding.py) |
| Acceptance and persistence | [executor](src/toolang/execution/executor/executor.py), [store](src/toolang/execution/store.py) | [control relations](tests/integration/execution/test_control_relations.py), [schema compatibility](tests/unit/execution/test_store_schema.py) |
| Scheduling | [work](src/toolang/work/) | [scheduler](tests/unit/work/test_scheduler.py), [checkpoints](tests/unit/work/test_store.py) |
| CLI/API integration | [CLI routing](src/toolang/cli/toolang/routing.py), [API routers](src/toolang/api/routers/) | [CLI integration](tests/integration/cli/), [remote runs](tests/integration/api/test_remote_runs.py) |

Use [package-boundary tests](tests/architecture/test_package_boundaries.py)
for enforced import rules. Some additional package restrictions remain explicitly
pending review; do not describe them as enforced. Plugin factory and entry-point
contracts are in [plugins](docs/plugins.md), not in package facades.

## Review and test by behavior

For a package change, map each affected public behavior or invariant to its
caller, existing test, missing case and verification level. Inspect serialization,
normalization, ordering, cancellation, persistence and external boundaries before
optimizing module size. A public entry point without an internal caller may still
be used by plugins; check registrations and documented consumers before removal.
Extract shared helpers only when ownership and multiple consumers are concrete.

Use deterministic unit tests for package logic, shared contract cases for plugin
implementations, integration tests for real persistence and collaborators, and
system tests for critical CLI workflows. Keep fixtures and resources isolated
across workers. Use synchronization primitives for concurrency instead of sleeps;
mock network/process boundaries rather than internal implementation details.
Coverage is evidence for finding gaps, not a substitute for assertions about
outputs, side effects and state transitions.

Before code/test commits, run the checks required by AGENTS.md:

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest -n auto
```

The full suite uses one worker per physical CPU core. Focused tests run serially;
use `--durations=20` to investigate slow tests. Live-provider and Docker cases are
opt-in through `--live-model` and `--live-docker`; see
[test configuration](tests/conftest.py). The default suite stays offline.
Documentation-only changes require implementation/example/link checks and
`git diff --check`, not the full suite.

## Documentation ownership and handoffs

| Rule | Canonical owner |
| --- | --- |
| Legal source forms and public CST | [tree-sitter-toolang grammar](https://github.com/openhat-ai/tree-sitter-toolang/blob/main/GRAMMAR.md), verified by `grammar.js` and corpus tests |
| Meaning, defaults, validation and runtime behavior | Toolang source/tests and the focused owners in [the index](docs/index.md) |
| Recommended source style | Website [Authoring Conventions](https://toolang.ai/docs/toolang-conventions) |
| Mechanical formatting | [Source commands](docs/source-commands.md#format), verified by formatter tests |

Bundled templates/examples follow the website conventions. Parser fixtures also
cover valid but discouraged source. Style recommendations do not silently change
parser acceptance, default types or formatter rewrites.

A syntax/CST change updates upstream grammar, generated artifacts, corpus and
queries together, then verifies Toolang's consumers and updates website reference,
`docs/syntaxes/toolang.tmLanguage.ts` and affected examples. A semantics-only
change updates Toolang and website explanations without requiring a parser
release. A style-only change starts on the website; a formatter change starts in
Toolang and updates website advice when its user-visible behavior changes.

Record a compatible Toolang release/commit and grammar version for website
reference generation and examples. Version numbers need not match. Check the
website's `scripts/toolang-source.txt` alongside its grammar reference instead of
assuming both describe the same release. Migration PRs record source sections,
destination files and completed verification before removing unique guidance.

## Documentation quality checklist

Apply these checks to every current guide touched by a documentation review.
Record the reviewed revision, findings and verification in the pull request;
keep historical plans and dated evaluations distinct from current contracts.

| Check | Passing condition |
| --- | --- |
| Reader and ownership | The title and opening identify the topic and intended reader. Detailed rules have one owner across the three repositories; other entry points link to it. |
| Accuracy | Claims match current implementation and tests, including defaults, precedence, lifecycle, errors and compatibility. Implemented behavior is distinguished from registered, reserved or planned functionality. |
| Completeness | The reader can understand the main concepts and normal path, with prerequisites and relevant limits or failure behavior stated explicitly. |
| Clarity and terminology | Concepts are defined before use, names match code and related guides, and the order supports a concrete reading task. Remove obsolete terms, ambiguous claims and repeated explanations. |
| Examples | Commands, source and configuration match current interfaces. State required context; distinguish complete examples from fragments, placeholders and intentionally invalid cases. Validate offline where possible. |
| Navigation and maintenance | Index entries, links and anchors resolve to the correct owners. Source/test evidence supports important claims; avoid duplicating volatile inventories or keeping empty pointer documents. |

A guide may pass without an edit. Fix substantive findings, then validate affected
examples, links and `git diff --check`; expand source inspection when evidence is
missing or contradictory.

## Maintain evidence incrementally

Record the source revision reviewed, topic owner and focused code/test anchors.
Compare revisions first, then recheck affected owners and consumers; expand the
search only for unresolved claims. Keep each detailed rule in one guide and link
from other readers' entry points. Validate complete examples, give fragments
minimal context, and label intentionally invalid examples or schematic notation.
Keep dated evaluations and historical plans separate from current contracts.

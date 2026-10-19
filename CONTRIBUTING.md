# Contributing to Toolang

Read [AGENTS.md](AGENTS.md) for work types, scope, approvals, Git workflow, and
required verification. Use a dedicated Git worktree with Python 3.11+ and `uv`
on macOS or Linux.

```sh
uv sync --frozen
uv run too --help
```

`too` is an alias for `toolang`.

## Repository documentation

| Location | Responsibility |
| --- | --- |
| Root [AGENTS.md](AGENTS.md) | Repository policy and definition of done |
| [docs/authoring.md](docs/authoring.md) | Recommended Toolang source style |
| Root [TESTING.md](TESTING.md) | Test design, coverage review, isolation, and execution |
| Root [AUDITING.md](AUDITING.md) | Package audit method and evidence requirements |
| [docs/index.md](docs/index.md) | Documentation TOC |
| [docs/design/](docs/design/) | Current architecture and implementation design, organized by owning concept |
| [docs/plans/](docs/plans/) | Discussion, feature-definition, and implementation records that may become outdated |
| [reference/](reference/README.md) | Generated implementation reference |

Keep repository contribution, testing, and audit standards at the root. Toolang
source-style conventions belong in `docs/authoring.md`. A product's implemented
behavior, data formats, ownership, and protocol belong in its focused guide under
`docs/design/`, even when those contracts impose rules on consumers.
Discussion and implementation records belong under `docs/plans/`. They may
become outdated as the product evolves and are not current specifications.
Current `docs/design/*.md` guides must never reference individual documents under
`docs/plans/`; link the current design owner instead. Directory navigation may
link to `docs/plans/` as historical context.
Remove superseded drafts and empty pointer documents after preserving any
still-current content in its owner. Use Git history to recover retired context.

User tutorials and the published language reference live on the
[documentation website](https://toolang.ai/docs). The installed
`tree-sitter-toolang` package owns grammar and CST definitions; Toolang owns
semantic lowering, validation, formatting, and execution. Check the pinned
grammar version in [pyproject.toml](pyproject.toml) when changing language
behavior. Do not infer compatibility from matching version numbers.

## Find the implementation owner

Start with the [architecture map](docs/design/architecture.md#package-ownership), then
read the relevant source, direct callers, design guide, and tests. The
opt-in [package-boundary tests](tests/architecture/test_package_boundaries.py)
record reviewed dependency rules; some boundaries remain unreviewed. Preserve plugin factories and entry points;
a public plugin interface may have callers outside this repository.

Keep tests proportional to the behavior being changed. The default suite must
remain offline and deterministic; [TESTING.md](TESTING.md) owns the detailed
method. Documentation-only changes require source/example/link verification
and `git diff --check`. Code or test changes require all four checks in
[AGENTS.md](AGENTS.md#verification) before each commit.

## Review design documentation

Check every touched guide against these conditions:

| Check | Passing condition |
| --- | --- |
| Ownership | The opening identifies the topic; each detailed contract has one owner and other guides link to it. |
| Accuracy | Defaults, lifecycle, precedence, failure behavior, and compatibility match the current source and tests. Distinguish implemented behavior from proposals. |
| Completeness | Explain the normal path, required context, boundaries, and material limits. Link to exact schemas instead of copying a partial schema as a complete one. |
| Clarity | Names match the implementation; remove obsolete vocabulary, repeated rules, and superseded migration instructions. |
| Examples | Validate complete source/configuration examples offline. Label fragments, placeholders, external prerequisites, and deliberately invalid inputs. |
| Navigation | Update the index, relative paths, anchors, and non-document consumers when moving a guide. Keep plans outside a current-contract audit unless explicitly in scope. |

Keep `docs/index.md` as the single TOC and design files flat in `docs/design/`.
Organize design chapters by responsibility. Explain ownership, data
flow, capture/update boundaries, failure handling, and consequential tradeoffs.
Omit self-evident usage and implementation inventories that source/help already
provides. Keep schemas linked when needed to explain a contract. Do not add
implementation-evidence sections, test inventories, or verification appendices
to design guides; record verification in the PR.

Record the implementation revision, substantive findings, moved/deleted owners,
and checks in the PR description. A guide can pass without an edit. Preserve
accurate changelog records; internal documentation organization does not require
a user-facing changelog entry.

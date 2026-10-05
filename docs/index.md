# Developer Documentation

These docs serve Toolang maintainers and contributors, with API and plugin
implementers as secondary readers. Start with [architecture](architecture.md)
for the system model and [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution
and verification. User installation, guides and examples belong on
[toolang.ai](https://toolang.ai).

## Repository responsibilities

| Repository | Owns |
| --- | --- |
| [tree-sitter-toolang](https://github.com/openhat-ai/tree-sitter-toolang) | Source syntax, public CST nodes/fields and parser queries; see its [grammar reference](https://github.com/openhat-ai/tree-sitter-toolang/blob/main/GRAMMAR.md). |
| Toolang | Semantic AST and lowering, language semantics, architecture, runtime and integration contracts, verified against code and tests. |
| `toolang-docs` | User guides, published reference and [authoring conventions](https://toolang.ai/docs/toolang-conventions). |

## Reading paths

- **Change language behavior:** [semantic AST and lowering](ast.md) →
  [shared program semantics](program.md) →
  [Agic execution](agic.md) / [Flow evaluation](flow.md) / [call input](call-input.md) →
  [source tooling](source-commands.md) and their linked tests.
- **Change binding or state:** [layout](layout.md) →
  [State publication](state.md) → [run acceptance](execution.md).
- **Change resources or plugins:** [caps](caps.md) / [models and Setup](models.md) /
  [tools](tools.md) → [query selection](queries.md) → [plugin contracts](plugins.md).
- **Change background work:** [Markdown jobs](tasks.md) /
  [program declarations](program.md#job-declarations) → [scheduling and recovery](work.md).
- **Change history or compaction:** [threads](execution.md#threads) →
  [recall and compaction](execution.md#history-recall-and-compaction) →
  [durable records](records.md).
- **Change a caller or integration:** [CLI](cli.md) / [HTTP](api.md) →
  [execution](execution.md) → [events](events.md) / [durable records](records.md).

## Document map

| Area | Focused owners |
| --- | --- |
| Orientation | [Architecture](architecture.md), [contributing](../CONTRIBUTING.md) |
| Language | [Semantic AST and lowering](ast.md), [Program declarations and shared semantics](program.md), [Agic model/tool loop](agic.md), [Flow evaluation](flow.md), [call input](call-input.md), [source commands](source-commands.md) |
| Configuration and State | [Scripts](scripts.md), [layout/storage](layout.md), [prepared State](state.md) |
| Resources | [Composable agent primitives (caps)](caps.md), [queries](queries.md), [models](models.md), [tools](tools.md), [plugins](plugins.md) |
| Work | [Markdown tasks/chores](tasks.md), [program declarations](program.md#job-declarations), [scheduling/recovery](work.md) |
| Execution | [Lifecycle and policy](execution.md), [records/references](records.md), [events/tracing](events.md), [presentation](execution-presentation.md) |
| Callers | [CLI](cli.md), [Chat](chat.md), [HTTP API](api.md) |

## History and generated reference

[Plans](plans/) record feature definitions and past decisions; they are not
proof of implemented behavior. Use [CHANGELOG.md](../CHANGELOG.md) for user-facing
changes and migration guidance.

[Generated code reference](../reference/) complements these contract guides.
Follow each guide's source/test links for exact types and current behavior.

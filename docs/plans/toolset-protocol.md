# Toolset protocol

## Goal and scope

Publish a small, explicit plugin contract. Keep model messages, execution records,
events, tool names, and authorization behavior unchanged. This definition reflects
the approved protocol discussion and supersedes the optional preparation and
description hooks in the tool-progress plan.

## Contract

- `Toolset.tools()` exposes `Tool` instances. `Tool` supplies default `summary`
  and `touchpoints` methods returning `None`.
- `invoke(arguments, context)` returns `ToolResult(output={}, error=None)`.
  Only `output` is extensible JSON data. An error may retain partial output.
  Call identity, timing, and cancellation belong to the executor.
- `summary(arguments, result=None)` returns plain call wording. No result means
  running; a result's error distinguishes failure from success. Executor prefixes
  running wording with `Canceled:` on cancellation, or uses its generic fallback.
  Progress owns markers, styles, duration, and layout.
- `touchpoints(arguments, context)` returns workspace names mapped to tuples of
  normalized workspace-relative paths. `None` means unsupported; an empty mapping
  means no workspace paths for this call. It performs no requested operation.
- Remove `ToolPreparation`, `ToolPath`, and duck-typed hook discovery. The only
  execution entry is `invoke`. Path helpers reuse invocation-local resolutions
  so preflight and execution agree, including symlink handling.
- Ordinary `ToolContext` contains `home`, `room`, and a captured workspace map.
  Runtime, history, service, and agent-management dependencies are explicitly
  provided only to their respective tools. Never retain a Run's dependencies on
  shared tool instances.
- Keep the used function-tool adapter, with the same hook signatures. Remove
  the unused Typer adapter. Keep `web/search` as the public search tool name.

## Touchpoints and acceptance

Update base protocols/types/helpers, built-in toolsets, executor tool dispatch,
plugin documentation/examples, and their tests.

Verify default hooks, typed success/failure/partial results, deterministic wording
without I/O, cancellation fallback, path-free calls, overlapping workspaces,
symlink target binding, State/workspace replacement on subsequent calls, isolated
privileged dependencies, and unchanged honor suppression/model replies. Existing
execution, replay, progress, and provider-offline tests must continue to pass.

## Risks and decisions

This is a Python plugin API change; migrate all repository implementations and
examples together without keeping old hook aliases. A context is invocation-local,
not reusable mutable Run state. Shell touchpoints cover its explicit cwd, not all
paths within arbitrary commands. No new sandbox policy or retry behavior is added.
There are no outstanding design decisions in this scope.

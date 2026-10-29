# Provider-owned search toolsets

Status: Ordinary toolsets and provider-owned APIs confirmed by the user on
2026-10-29. This document defines the extension direction, not implementation
approval for provider packages.

## Goal and scope

Provide search integrations as ordinary Toolang toolset plugins. A user chooses
which provider packages to install, such as the proposed `tavily-toolang`,
`serpapi-toolang`, or `xai-toolang`. Each package owns its public tool API,
configuration, authentication, transport, and result content.

Use the existing plugin contracts and loading path. Do not introduce a search
adapter/backend family, catalog, router, rotation state, readiness protocol,
shared search request/response types, or cross-provider fallback.

Provider package implementations and their exact APIs require their own scoped
definitions. This document does not claim these packages already exist or
authorize implementing all providers.

## Registration and identity

Each distribution registers normal `toolang.toolset` entry points:

```toml
[project.entry-points."toolang.toolset"]
tavily = "tavily_toolang:create_toolset"
```

The factory accepts its own configuration mapping and returns a normal
`Toolset`. Its tools provide their own `ToolDefinition` schemas and ordinary
`ToolResult` envelopes. Follow the existing
[plugin contracts](../design/plugins.md) and
[tool contracts](../design/tools.md).

Provider-specific names allow packages to coexist, for example
`tavily/search`, `serpapi/search`, and `xai/search`. Names and leaf APIs belong
to their packages; this is not a mandatory common search schema. Installation
uses normal plugin discovery, subject to existing allow policy and run resource
declarations. No additional search-plugin activation list is needed.

Keep the existing duplicate-name checks. Installing another package must not
silently replace a toolset. The bundled `web/search` remains its current
DDGS-based tool; it does not discover or delegate to these provider toolsets.
Do not change its public arguments, DDGS behavior, or configuration here.

## Provider-owned APIs and credentials

Plugins may expose different arguments, defaults, response fields, errors, and
additional tools. Do not require every provider to accept `query`, `top_k`,
`domains`, or `backends`, or normalize provider results into a new shared type.
Toolang enforces only its existing generic tool contracts.

Configuration remains in each package's own table, for example
`[plugin.toolset.tavily]` or `[plugin.toolset.serpapi]`. The package defines
the table's fields. Factories and invocation follow existing plugin and
[selected environment](../design/setup.md#environments) boundaries.

The first Tavily integration should support documented keyless access with no
provider configuration or key after installation. Key-only providers such as
SerpAPI or xAI may require credentials; installing a package cannot supply an
account automatically. The selected agent environment must actually contain
those credentials. Each plugin defines credential checks and actionable errors
through the ordinary tool mechanism; core does not inspect provider key names,
probe services, or add search-specific availability state.

Keep initialization and tool inspection usable without provider network calls.
Absent credentials must not prevent unrelated installed toolsets from loading.
Actual search calls establish whether supplied credentials and the remote
service work. Never expose credentials in outputs or diagnostics.

## Multiple providers and future composition

Installed provider toolsets remain independent. Their tools contact their own
providers and do not rotate across, automatically invoke, or fall back to other
installed provider tools. Prompts or explicitly authored workflows may choose
and sequence them using their published schemas.

If combined search becomes useful, define a separate ordinary toolset with its
own API, provider selection, aggregation/fallback policy, and acceptance tests.
That composite tool owns adaptation to heterogeneous provider APIs. Do not
build it now or impose its prospective requirements on provider plugins.

## Acceptance and implementation touchpoints

- Provider package `pyproject.toml`: existing `toolang.toolset` registration,
  compatible Toolang dependency, and independently installable distribution.
- Provider package source: ordinary factory, toolset, tool schemas, execution,
  and package-owned configuration. No new Toolang core contract is required.
- Conformance checks: wheel installation exposes the provider's tool identities;
  two provider packages coexist without replacement; normal allow rules apply;
  each tool retains its own input/output schema and contacts only its provider.
- Provider-specific definitions must cover their own inputs, auth modes, errors,
  deadlines/cancellation, and offline request fixtures. Tavily acceptance includes
  empty provider configuration and no key; keyed integrations cover missing
  credentials without breaking unrelated tool loading.
- Preserve the existing DDGS tests and behavior. Keep default tests offline;
  use separately opted-in live smoke checks when implementing each integration.
  Follow repository verification and changelog requirements for those changes.
- This definition changes only this plan. No implementation, user environment,
  agent prompt, or current-design document is modified.

## Risks and remaining decisions

Keyless services remain subject to network and rate limits; keyed providers may
charge for calls. These limits belong in each plugin's documentation. Exact
provider APIs, package hosting, and release details are decisions for the
corresponding provider definition, not a new global search framework.

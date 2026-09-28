# Configure the web search backend

Status: Approved by the user on 2026-09-28 for implementation.

## Goal and success criteria

Web searches use DDGS's Google backend by default, while allowing the backend to
be selected in the web toolset's plugin configuration. The selected backend is
passed to DDGS for every search.

## Scope and decisions

- Add a `backend` setting to the `web` toolset configuration, with `google` as
  the default. Existing plugin config loading already delivers this mapping to
  the web toolset.
- Pass the configured backend to `DDGS.text()`; do not choose a backend based
  on `domains`, which remains post-search result filtering.
- Keep the backend out of the model-facing `search` tool arguments. Selection
  belongs to plugin configuration, alongside the existing `top_k` and
  `timeout` settings.
- Preserve existing result, timeout, and error handling behavior. Do not change
  other toolsets or add unrelated documentation.

## Implementation touchpoints

- `src/toolang/plugin/toolsets/web.py`: read the configured backend, supply its
  default, and pass it into the DDGS text search.
- `tests/unit/plugin/`: add focused deterministic tests for the default and an
  explicitly configured backend, mocking the DDGS call rather than using live
  search.

## Acceptance tests

- With no `backend` in the web plugin configuration, the DDGS text call receives
  `backend="google"`.
- With a configured backend, the DDGS text call receives that value.
- Existing web search result and domain-filter behavior is unchanged.
- Focused tests and repository default verification pass.

## Risks

- The configured value must match the backend identifiers accepted by the
  installed DDGS version. The implementation should pass through the configured
  value rather than maintain a potentially stale duplicate backend list.

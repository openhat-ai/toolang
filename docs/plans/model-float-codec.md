# Float prices and typed catalog decoding

Approved in the PR #554 discussion: use float and msgspec, including cost
accounting and budgets; retain the existing record wire formats.

## Contract

- Reuse the catalog dataclasses as the decoding schema, without parallel DTOs.
  Decode the private resolved snapshot directly with msgspec and preserve
  immutable mappings, ownership checks, and trusted provider overrides.
  Model.provider uses a fixed ModelProvider dataclass whose _toolang field is
  ProviderToolang; no dynamic reconstruction.
- Source caches contain declarations only. Disk ready/route values never become
  published runtime facts. Cache encoding changes invalidate older caches.
- Catalog numbers and accounting interfaces use finite floats instead of
  Decimal. Preserve intermediate per-token prices until the call is settled.
- Settle each complete call to six fractional USD digits, rounding half up.
  Interpret the shortest decimal rate text before multiplication and summation;
  use transient rational arithmetic only at settlement to preserve half-micro ties.
  Accumulate settled amounts and compare budgets in integer micro-USD units.
  Monetary values range from zero through 999,999,999.999999 USD; reject
  non-finite, negative, or out-of-range amounts. Normalize budgets identically.
  Reject boolean budgets before numeric coercion; accept legacy decimal strings.
- Preserve record field names and decimal-text wire values, including reading
  existing records. Do not migrate records or introduce new persistence types.
- Keep generic query support for unrelated Decimal values unchanged.

## Implementation and acceptance

Touch catalog parsing, shared/cache codecs, model views, accounting, limits,
provider usage normalization, policy/API/record conversion, and cost displays.
Add msgspec to dependencies. Cover typed snapshot round trips, nested trusted
provider declarations, source route exclusion, malformed numeric values, six-digit
settlement, repeated small costs, exact budget boundaries, upper bounds, and
record replay. Run the default offline checks and existing live smoke tests.
Benchmark cold/warm setup and full catalog retrieval for api.json and catalog.json.

## Risks

Call totals below half a micro-USD round to zero. Never round per-token rates
before multiplication. Typed decoding is stricter than the former coercing
codec; source parsing and cache-miss recovery remain separate. msgspec ships
native wheels for supported CPython versions on Linux, macOS, and Windows;
repository CI currently verifies Linux and macOS. No open design questions.

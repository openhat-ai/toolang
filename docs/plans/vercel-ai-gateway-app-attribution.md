# Vercel AI Gateway App Attribution

## Goal and success criteria

Send Toolang app attribution on requests routed through Vercel AI Gateway so the app can be identified in Gateway analytics and, where applicable, app listings.

Success means Gateway requests carry `http-referer: https://toolang.ai` and `x-title: Toolang`; non-Gateway routes do not gain these headers.

## Scope and decisions

- Identify the Vercel AI Gateway route by its `@ai-sdk/gateway` catalog package declaration, not by a URL match or generic OpenAI-compatible protocol.
- Add the two Vercel-documented attribution headers using the existing Toolang attribution URL and app name. Do not add a Vercel-specific app ID, secret, or new user configuration.
- Keep existing OpenRouter attribution unchanged. Do not add support for other routers or change provider catalog contents.
- Let existing route header merging preserve normal per-model and mode overrides.

## Design touchpoints

- `src/toolang/setup/routes.py`: resolve Gateway attribution headers alongside provider conventions.
- `tests/unit/setup/test_routes.py`: verify Gateway headers, isolation from other providers, and existing override precedence.

## Acceptance tests

1. A provider using `@ai-sdk/gateway` resolves with `http-referer` and `x-title` attribution headers.
2. A non-Gateway provider, including an OpenRouter provider, receives no Vercel-specific headers and retains its current behavior.
3. Explicit model headers continue to override convention headers case-insensitively.
4. The default verification suite passes.

## Risks

- Attribution applies only where the catalog identifies the provider with the official Gateway package; a manually configured compatible endpoint under another package is intentionally not inferred to be Vercel.
- Vercel attribution is optional; header use does not guarantee public featuring or leaderboard inclusion.

## References

- [Vercel AI Gateway App Attribution](https://vercel.com/docs/ai-gateway/ecosystem/app-attribution)
- [Toolang OpenRouter provider conventions](../../src/toolang/setup/routes.py)

# Vercel AI Gateway App Attribution

## Goal and success criteria

Send Toolang app attribution on requests belonging to the Vercel provider so the app can be identified in AI Gateway analytics and, where applicable, app listings.

Success means models owned by provider ID `vercel` carry `http-referer: https://toolang.ai` and `x-title: Toolang`; other provider IDs do not gain these headers.

## Scope and decisions

- Identify Vercel by the catalog provider ID `vercel`, matching the existing provider-ID convention used for OpenRouter. Do not infer attribution from an SDK package name or generic OpenAI-compatible protocol.
- Add the two Vercel-documented attribution headers using the existing Toolang attribution URL and app name. Do not add a Vercel-specific app ID, secret, or new user configuration.
- Keep existing OpenRouter attribution unchanged. Do not add support for other routers or change provider catalog contents.
- Let existing route header merging preserve normal per-model and mode overrides.

## Design touchpoints

- `src/toolang/setup/routes.py`: declare Vercel attribution as a provider-ID convention, alongside OpenRouter attribution.
- `tests/unit/setup/test_routes.py`: verify Gateway headers, isolation from other providers, and existing override precedence.

## Acceptance tests

1. Provider ID `vercel` resolves with `http-referer` and `x-title` attribution headers.
2. A different provider ID receives no Vercel-specific headers, even when it uses `@ai-sdk/gateway`; OpenRouter retains its existing conventions.
3. Explicit model headers continue to override convention headers case-insensitively.
4. The default verification suite passes.

## Risks

- Attribution applies only when the catalog provider ID is `vercel`; custom provider IDs are not inferred to be Vercel based on an SDK package or endpoint.
- Vercel attribution is optional; header use does not guarantee public featuring or leaderboard inclusion.

## References

- [Vercel AI Gateway App Attribution](https://vercel.com/docs/ai-gateway/ecosystem/app-attribution)
- [Toolang OpenRouter provider conventions](../../src/toolang/setup/routes.py)

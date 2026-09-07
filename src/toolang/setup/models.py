"""Ordering and compact selection within an effective model collection."""

from toolang.base.model_settings import apply_model_override
from toolang.base.types.model import ModelOverride, ModelRequest
from toolang.common.errors import ToolangError
from toolang.plugin.models.collections import ModelCollection
from toolang.plugin.models.resolution import apply_model_parameters


DEFAULT_PROVIDERS = (
    "alibaba",
    "anthropic",
    "deepseek",
    "google",
    "meta",
    "minimax",
    "mistral",
    "moonshotai",
    "openai",
    "openrouter",
    "xai",
    "zai",
    "zhipuai",
)


def order_models(
    models: ModelCollection, queries: tuple[str, ...] | None
) -> ModelCollection:
    """Authored ordering wins; the fallback ranks providers without excluding any."""
    return models.match(
        queries
        if queries is not None
        else (*[f"{p}/*" for p in DEFAULT_PROVIDERS], "*")
    ).compact()


def select_compact_model(
    models: ModelCollection, override: ModelOverride | None
) -> ModelRequest:
    """Select once from allowed models, independently of the normal Run model."""
    if override is not None and override.identity == "unset":
        raise ToolangError("automatic compaction is disabled by compact.model")
    eligible = models.match("*[tool_call; structured_output]")
    if override is None:
        if not eligible.entries:
            raise ToolangError(
                "compaction requires an allowed model with tool calls and structured output"
            )
        request = ModelRequest(eligible.entries[0].target.ref)
    else:
        request = apply_model_override(None, None, override)
        assert request is not None
        if not eligible.contains(request.ref):
            raise ToolangError(
                f"compact model {request.ref!r} must be available, allowed, and support "
                "tool calls and structured output"
            )
    target = eligible.resolve(request.ref).target
    apply_model_parameters(eligible, target, request.parameters)
    return request

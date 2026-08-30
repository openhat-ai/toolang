"""User-facing model selection error messages."""

from __future__ import annotations


NO_AVAILABLE_MODELS_MESSAGE = (
    "No available models.\n\n"
    "Set OPENAI_API_KEY or OPENROUTER_API_KEY, or start Ollama with local models.\n"
    "Run `too providers` for configuration status."
)

NO_MATCHED_MODELS_MESSAGE = (
    "No matched models.\n\n"
    "Run `too models --query QUERY` to try a query, or `too query models` for help."
)

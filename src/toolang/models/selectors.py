"""Model selector parsing."""

from __future__ import annotations

from toolang.selectors import Selector as ModelSelector
from toolang.selectors import parse_selector, split_selector_list


def split_model_selectors(items: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Split repeated and CSV model selector inputs."""

    return split_selector_list(items)


def parse_model_selector(raw: str) -> ModelSelector:
    """Parse one model selector."""

    return parse_selector(raw, domain="model")

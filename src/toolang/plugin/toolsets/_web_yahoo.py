"""Yahoo layout compatibility for DDGS 9.16's text engine.

Yahoo serves both relsrch and algo-only result containers. Keep DDGS's request
and redirect handling, but recognize both observed layouts without global
registry changes. Remove this adapter when upstream covers both variants.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import ClassVar

from ddgs.engines.yahoo import Yahoo
from ddgs.exceptions import DDGSException
from primp import RequestError


class _YahooSearch(Yahoo):
    items_xpath = (
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' relsrch ')] | "
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' algo ')]"
    )
    elements_xpath: ClassVar[Mapping[str, str]] = {
        "title": ".//h3//text()[not(ancestor::span[contains(@class, 'title-url')])]",
        "href": "(.//div[contains(@class, 'Title')]//a/@href)[1]",
        "body": ".//div[contains(@class, 'Text')]//text()",
    }


def search_yahoo(query: str, max_results: int, timeout: int) -> list[dict[str, str]]:
    engine = _YahooSearch(timeout=timeout)
    try:
        results = engine.search(query) or []
    except RequestError as exc:
        raise DDGSException(f"Yahoo request failed: {exc}") from exc
    return [asdict(result) for result in results[:max_results]]

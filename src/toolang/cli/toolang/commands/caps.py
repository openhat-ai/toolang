"""Toolang-specific cap command group factories."""

from __future__ import annotations

from functools import cache

import typer

from toolang.catalog.types import CapKind
from toolang.cli.common.routing import OptionalPrefixAgentGroup


@cache
def _apps() -> dict[CapKind, typer.Typer]:
    from toolang.cli.caps.commands import create_cap_apps

    return create_cap_apps(group_cls=OptionalPrefixAgentGroup)


def psyche_app() -> typer.Typer:
    return _apps()["psyche"]


def skill_app() -> typer.Typer:
    return _apps()["skill"]


def service_app() -> typer.Typer:
    return _apps()["service"]


def prompt_app() -> typer.Typer:
    return _apps()["prompt"]


__all__ = ["prompt_app", "psyche_app", "service_app", "skill_app"]

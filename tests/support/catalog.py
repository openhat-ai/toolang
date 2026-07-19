"""Catalog fixtures shared by integration tests."""

from __future__ import annotations

from pathlib import Path

from toolang.catalog import templates
from toolang.catalog.agent import LocalAgents


class FixtureLocalAgents(LocalAgents):
    """Local agent catalog with a template-backed test default."""

    def create(self, name: str, *, content: str | None = None) -> Path:
        if content is None:
            content = templates.render_template("agent", agent_name=name, name=name)
        return super().create(name, content=content)

"""Agent uptime uses shared compact duration formatting."""

from datetime import UTC, datetime, timedelta

import pytest

from toolang.cli.toolang.commands import agent


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "up 0s"), (68.9, "up 1m8s"), (3661, "up 1h1m1s")],
)
def test_agent_uptime_uses_compact_duration(
    monkeypatch: pytest.MonkeyPatch, seconds: float, expected: str
) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(agent, "_utc_now", lambda: started + timedelta(seconds=seconds))
    assert agent._human_uptime_since(started.isoformat()) == expected
    assert agent._human_uptime_since("invalid") is None

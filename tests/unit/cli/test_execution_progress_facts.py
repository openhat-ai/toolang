from __future__ import annotations

from toolang.cli.common.execution_progress.facts import elapsed_fact


def test_elapsed_normalizes_rounded_seconds_into_minutes() -> None:
    assert (
        elapsed_fact(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:01:59.600Z",
        )
        == "2m0s"
    )


def test_elapsed_rounds_seconds_to_whole_values() -> None:
    assert (
        elapsed_fact(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00.250Z",
        )
        == "250ms"
    )
    assert (
        elapsed_fact(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00.9996Z",
        )
        == "1s"
    )
    assert (
        elapsed_fact(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:02Z",
        )
        == "2s"
    )
    assert (
        elapsed_fact(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:02.400Z",
        )
        == "2s"
    )
    assert (
        elapsed_fact(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:02.600Z",
        )
        == "3s"
    )
    assert (
        elapsed_fact(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:59.600Z",
        )
        == "1m0s"
    )
    assert (
        elapsed_fact(
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:01:21.400Z",
        )
        == "1m21s"
    )


def test_elapsed_uses_unpadded_hours_minutes_and_seconds() -> None:
    assert elapsed_fact("2026-01-01T00:00:00Z", "2026-01-01T01:01:01Z") == "1h1m1s"
    assert elapsed_fact("2026-01-01T00:00:00Z", "2026-01-01T00:01:08Z") == "1m8s"

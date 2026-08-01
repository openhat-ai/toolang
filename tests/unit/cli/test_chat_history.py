from __future__ import annotations

import json
from pathlib import Path

import pytest

from toolang.cli.toolang.commands.chat.history import ChatInputHistoryStore


def test_chat_history_round_trips_recent_inputs_and_skips_bad_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text(
        '\n'.join(("not json", "[]", '{"text":""}', '{"text":"old"}')) + "\n",
        encoding="utf-8",
    )
    history = ChatInputHistoryStore(path, limit=2)

    history.append("new")
    history.append("latest")

    assert history.load() == ["new", "latest"]


def test_chat_history_compacts_to_recent_limit(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    history = ChatInputHistoryStore(path, limit=2, compact_limit=3)

    for text in ("one", "two", "three", "four"):
        history.append(text)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["text"] for record in records] == ["three", "four"]
    assert history.load() == ["three", "four"]


@pytest.mark.parametrize(
    "kwargs",
    (
        {"limit": 0},
        {"limit": 2, "compact_limit": 1},
        {"compact_size_bytes": 0},
    ),
)
def test_chat_history_rejects_invalid_limits(
    tmp_path: Path,
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        ChatInputHistoryStore(tmp_path / "history.jsonl", **kwargs)

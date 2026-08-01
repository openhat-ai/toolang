from __future__ import annotations

import json
from pathlib import Path

import pytest

from toolang.base.types.model import ModelInfo
from toolang.setup.records import ModelListRecord


def test_model_list_record_round_trips_complete_model_info(tmp_path: Path) -> None:
    path = tmp_path / "models" / "provider.json"
    model = ModelInfo(
        ref="provider/model",
        provider="provider",
        name="Model",
        model="model",
        selectors=("fast", "cheap"),
        adapter="responses",
        scope="remote",
        tags=("reasoning",),
        tools=False,
        streaming=False,
        context_window=128_000,
        max_output_tokens=8_192,
        input_price=1.25,
        output_price=5.0,
        details="A test model",
        metadata={"released": "2026-01-01", "tier": 2},
    )
    expected = ModelListRecord(
        provider="provider",
        fingerprint="abc123",
        generation=3,
        fetched_at=1234.5,
        models=(model,),
    )

    expected.save(path)

    assert ModelListRecord.load(path) == expected


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", 2, "unsupported model-list record version"),
        ("models", {}, "models must be a list"),
        ("generation", True, "generation must be an integer"),
        ("fetched_at", False, "fetched_at must be a number"),
    ],
)
def test_model_list_record_rejects_invalid_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    path = tmp_path / "provider.json"
    payload: dict[str, object] = {
        "version": 1,
        "provider": "provider",
        "fingerprint": "abc123",
        "generation": 1,
        "fetched_at": 1234.5,
        "models": [],
    }
    payload[field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match=message):
        ModelListRecord.load(path)

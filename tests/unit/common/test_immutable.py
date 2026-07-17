from __future__ import annotations

from collections.abc import Mapping
import operator
from typing import Any, cast

import pytest

from toolang.common.immutable import freeze_mapping, mutable_data


def test_freeze_mapping_copies_and_freezes_nested_containers() -> None:
    source = {
        "mapping": {"items": [1, 2]},
        "set": {3, 2},
        "buffer": bytearray(b"data"),
    }

    frozen = freeze_mapping(source)
    source["mapping"]["items"].append(3)
    source["set"].add(4)
    source["buffer"].extend(b"!")

    assert isinstance(frozen, Mapping)
    assert frozen["mapping"] == {"items": (1, 2)}
    assert frozen["set"] == frozenset({2, 3})
    assert frozen["buffer"] == b"data"
    with pytest.raises(TypeError):
        operator.setitem(cast(Any, frozen), "other", True)


def test_mutable_data_returns_independent_mutable_containers() -> None:
    frozen = freeze_mapping({"mapping": {"items": [1, 2]}, "set": {3, 2}})

    mutable = mutable_data(frozen)
    mutable["mapping"]["items"].append(3)
    mutable["set"].append(4)

    assert mutable == {
        "mapping": {"items": [1, 2, 3]},
        "set": [2, 3, 4],
    }
    assert frozen == {
        "mapping": {"items": (1, 2)},
        "set": frozenset({2, 3}),
    }


def test_mutable_data_copies_bytearray() -> None:
    source = bytearray(b"data")

    mutable = mutable_data(source)
    mutable.extend(b"!")

    assert mutable == bytearray(b"data!")
    assert source == bytearray(b"data")

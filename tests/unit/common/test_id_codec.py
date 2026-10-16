"""Pure codec properties independent of clocks, storage, and ID owners."""

import pytest

from toolang.common.id_codec import (
    decode_short_id,
    derive_id_key,
    encode_short_id,
    scramble_id,
)


@pytest.mark.parametrize("width", [2, 8, 16])
def test_base32_round_trips_boundaries(width):
    for value in (0, 1, (1 << (width * 5)) - 1):
        encoded = encode_short_id(value, width=width)
        assert len(encoded) == width
        assert decode_short_id(encoded, width=width) == value
        assert decode_short_id(" " + encoded.upper() + " ", width=width) == value


@pytest.mark.parametrize("value", [-1, 1 << 40])
def test_codec_rejects_values_outside_the_fixed_width(value):
    with pytest.raises(ValueError, match="out of range"):
        encode_short_id(value)
    with pytest.raises(ValueError, match="out of range"):
        scramble_id(value, width=8, key=b"key")


@pytest.mark.parametrize("value", ["", "123", "0000000i", "0000000o"])
def test_base32_rejects_invalid_codes(value):
    with pytest.raises(ValueError):
        decode_short_id(value)


def test_permutation_is_bijective_over_a_complete_small_domain():
    key = derive_id_key("test:v1", "alice")
    encoded = {scramble_id(value, width=2, key=key) for value in range(1 << 10)}
    assert encoded == set(range(1 << 10))
    for value in encoded:
        original = scramble_id(value, width=2, key=key, reverse=True)
        assert scramble_id(original, width=2, key=key) == value


def test_key_derivation_preserves_namespace_framing_case_and_unicode():
    values = [
        ("test:v1", "ab", "c"),
        ("test:v1", "a", "bc"),
        ("test:v2", "ab", "c"),
        ("test:v1", "Ab", "c"),
        ("test:v1", "é"),
        ("test:v1", "e\u0301"),
    ]
    assert len({derive_id_key(*value) for value in values}) == len(values)
    assert derive_id_key(*values[0]) == derive_id_key(*values[0])

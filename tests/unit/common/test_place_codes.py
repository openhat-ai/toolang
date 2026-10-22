"""Canonical place-code fields use raw hashes and preserve timestamp ordering."""

import pytest

from toolang.common.id_codec import decode_short_id, derive_id_key
from toolang.common.place_codes import (
    root_code,
    decode_place_code,
    encode_place_code,
    roaming_place_code,
    roaming_timestamp_ms,
    visiting_place_code,
)


@pytest.mark.parametrize("source", [0, 1, 2**32 - 1])
@pytest.mark.parametrize("timestamp", [0, 1, 2**48 - 1])
def test_roaming_codec_preserves_independent_component_boundaries(source, timestamp):
    token = encode_place_code("s", source, timestamp)
    assert len(token) == 18
    assert decode_place_code(token, marker="s") == (source, timestamp)
    assert token[1:8] == encode_place_code("s", source, 0)[1:8]
    assert token[8:] == encode_place_code("s", 0, timestamp)[8:]


@pytest.mark.parametrize("source", [0, 1, 2**48 - 1])
def test_visiting_codec_has_one_bounded_source_field(source):
    token = encode_place_code("t", source)
    assert len(token) == 11
    assert decode_place_code(token, marker="t") == (source, None)


@pytest.mark.parametrize(
    "source,timestamp",
    [
        (-1, 0),
        (2**32, 0),
        (0, -1),
        (0, 2**48),
        (True, 0),
        (0, False),
        (0.0, 1),
        (1, 1.0),
    ],
)
def test_packing_rejects_component_overflow_and_nonintegers(source, timestamp):
    with pytest.raises(ValueError, match="component"):
        encode_place_code("s", source, timestamp)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "0" * 17,
        "h" + "0" * 10,
        "r" + "0" * 16,
        "v" + "0" * 16,
        "S" + "0" * 17,
        "s" + "A" * 17,
        "s" + "o" * 17,
        "s" + "0" * 16,
        "s" + "0" * 18,
        "t" + "0" * 9,
        "t" + "0" * 11,
        "s80000000000000000",
        "s00000008000000000",
        "t8000000000",
        "aid@s" + "0" * 17,
        " s" + "0" * 17,
    ],
)
def test_decoder_rejects_partial_noncanonical_overflow_and_previous_ids(token):
    with pytest.raises(ValueError):
        decode_place_code(token)


def test_codec_requires_the_requested_placement_marker():
    with pytest.raises(ValueError):
        decode_place_code(encode_place_code("t", 1), marker="s")


def test_hash_fields_use_distinct_framed_domains_and_raw_base32():
    root = "/root"
    ref = "gh:owner/agent"
    path = "/a.too"
    assert len(root_code(root)) == 7
    assert decode_short_id(root_code(root), width=7) == int.from_bytes(
        derive_id_key("place:v1", root)[:4], "big"
    )
    assert decode_place_code(visiting_place_code(ref))[0] == int.from_bytes(
        derive_id_key("visiting:v1", ref)[:6], "big"
    )
    assert decode_place_code(roaming_place_code(path, 0))[0] == int.from_bytes(
        derive_id_key("roaming:v1", path)[:4], "big"
    )
    assert visiting_place_code(ref) != visiting_place_code("gh:other/agent")


def test_short_code_vectors_pin_the_pre_release_domains():
    assert root_code("/root") == "0knzkk6"
    assert visiting_place_code("gh:owner/agent") == "t7skgcwa0vh"
    assert roaming_place_code("/a.too", 32) == "s1eyd2x10000000010"


def test_roaming_order_is_chronological_within_a_path_and_decodable_across_paths():
    timestamps = (0, 1, 31, 32, 1023, 1024, 2**48 - 1)
    tokens = [roaming_place_code("/a.too", value) for value in timestamps]
    assert tokens == sorted(tokens)
    assert [roaming_timestamp_ms(token) for token in tokens] == list(timestamps)
    assert roaming_timestamp_ms(roaming_place_code("/other.too", 32)) == 32

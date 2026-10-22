"""Canonical instance fields use raw hashes and preserve timestamp ordering."""

import pytest

from toolang.common.id_codec import decode_short_id, derive_id_key
from toolang.common.instance_ids import (
    area_id,
    decode_instance_id,
    encode_instance_id,
    roaming_instance_id,
    roaming_timestamp_ms,
    visiting_instance_id,
)


@pytest.mark.parametrize("source", [0, 1, 2**32 - 1])
@pytest.mark.parametrize("timestamp", [0, 1, 2**48 - 1])
def test_site_codec_preserves_independent_component_boundaries(source, timestamp):
    token = encode_instance_id("s", source, timestamp)
    assert len(token) == 18
    assert decode_instance_id(token, marker="s") == (source, timestamp)
    assert token[1:8] == encode_instance_id("s", source, 0)[1:8]
    assert token[8:] == encode_instance_id("s", 0, timestamp)[8:]


@pytest.mark.parametrize("source", [0, 1, 2**48 - 1])
def test_hotel_codec_has_one_bounded_source_field(source):
    token = encode_instance_id("h", source)
    assert len(token) == 11
    assert decode_instance_id(token, marker="h") == (source, None)


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
        encode_instance_id("s", source, timestamp)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "0" * 17,
        "r" + "0" * 16,
        "v" + "0" * 16,
        "S" + "0" * 17,
        "s" + "A" * 17,
        "s" + "o" * 17,
        "s" + "0" * 16,
        "s" + "0" * 18,
        "h" + "0" * 9,
        "h" + "0" * 11,
        "s80000000000000000",
        "s00000008000000000",
        "h8000000000",
        "aid@s" + "0" * 17,
        " s" + "0" * 17,
    ],
)
def test_decoder_rejects_partial_noncanonical_overflow_and_previous_ids(token):
    with pytest.raises(ValueError):
        decode_instance_id(token)


def test_codec_requires_the_requested_placement_marker():
    with pytest.raises(ValueError):
        decode_instance_id(encode_instance_id("h", 1), marker="s")


def test_hash_fields_use_distinct_framed_domains_and_raw_base32():
    root = "/root"
    ref = "gh:owner/agent"
    path = "/a.too"
    assert len(area_id(root)) == 7
    assert decode_short_id(area_id(root), width=7) == int.from_bytes(
        derive_id_key("area:v1", root)[:4], "big"
    )
    assert decode_instance_id(visiting_instance_id(ref))[0] == int.from_bytes(
        derive_id_key("hotel:v1", ref)[:6], "big"
    )
    assert decode_instance_id(roaming_instance_id(path, 0))[0] == int.from_bytes(
        derive_id_key("site:v1", path)[:4], "big"
    )
    assert visiting_instance_id(ref) != visiting_instance_id("gh:other/agent")


def test_roaming_order_is_chronological_within_a_path_and_decodable_across_paths():
    timestamps = (0, 1, 31, 32, 1023, 1024, 2**48 - 1)
    tokens = [roaming_instance_id("/a.too", value) for value in timestamps]
    assert tokens == sorted(tokens)
    assert [roaming_timestamp_ms(token) for token in tokens] == list(timestamps)
    assert roaming_timestamp_ms(roaming_instance_id("/other.too", 32)) == 32

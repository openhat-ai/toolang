"""Canonical instance encoding has no filesystem, clock or allocator policy."""

import pytest

from toolang.common.id_codec import derive_id_key
from toolang.common.instance_ids import (
    decode_instance_id,
    encode_instance_id,
    roaming_instance_id,
    roaming_timestamp_ms,
    visiting_instance_id,
)


@pytest.mark.parametrize("marker", ["v", "r"])
@pytest.mark.parametrize("prefix", [0, 1, 2**32 - 1])
@pytest.mark.parametrize("suffix", [0, 1, 2**48 - 1])
def test_instance_codec_preserves_component_boundaries(marker, prefix, suffix):
    token = encode_instance_id(marker, prefix, suffix)
    assert len(token) == 17
    assert token[0] == marker
    assert decode_instance_id(token, marker=marker) == (prefix, suffix)
    assert encode_instance_id(marker, 0, 0) == marker + "0" * 16


@pytest.mark.parametrize(
    "prefix,suffix",
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
def test_packing_rejects_component_overflow_and_nonintegers(prefix, suffix):
    with pytest.raises(ValueError, match="component"):
        encode_instance_id("r", prefix, suffix)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "0" * 16,
        "R" + "0" * 16,
        "r" + "A" * 16,
        "r" + "o" * 16,
        "r" + "i" * 16,
        "r" + "l" * 16,
        "r" + "u" * 16,
        "r" + "0" * 15,
        "r" + "0" * 17,
        "r" + "0" * 15 + " ",
        " r" + "0" * 16,
        "a@r" + "0" * 16,
        "r" + "a" * 32,
    ],
)
def test_decoder_rejects_noncanonical_or_legacy_spellings(token):
    with pytest.raises(ValueError):
        decode_instance_id(token)


def test_codec_requires_the_requested_placement_marker():
    with pytest.raises(ValueError):
        decode_instance_id(encode_instance_id("v", 1, 2), marker="r")


def test_visiting_hashes_use_framed_independent_namespaces():
    first = visiting_instance_id("/root", "gh:owner/agent")
    changed_root = visiting_instance_id("/other", "gh:owner/agent")
    changed_url = visiting_instance_id("/root", "gh:other/agent")
    prefix, suffix = decode_instance_id(first)
    assert prefix == int.from_bytes(
        derive_id_key("instance-root:v1", "/root")[:4], "big"
    )
    assert suffix == int.from_bytes(
        derive_id_key("visiting-url:v1", "gh:owner/agent")[:6], "big"
    )
    assert decode_instance_id(changed_root)[1] == suffix
    assert decode_instance_id(changed_url)[0] == prefix
    assert len({first, changed_root, changed_url}) == 3


def test_roaming_order_is_chronological_within_a_path_and_decodable_across_paths():
    tokens = [
        roaming_instance_id("/a.too", value) for value in (0, 1, 31, 32, 2**48 - 1)
    ]
    assert tokens == sorted(tokens)
    assert [roaming_timestamp_ms(token) for token in tokens] == [
        0,
        1,
        31,
        32,
        2**48 - 1,
    ]
    assert roaming_timestamp_ms(roaming_instance_id("/different.too", 32)) == 32
    assert decode_instance_id(tokens[0])[0] == int.from_bytes(
        derive_id_key("roaming-path:v1", "/a.too")[:4], "big"
    )

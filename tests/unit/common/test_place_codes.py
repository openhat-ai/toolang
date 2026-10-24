"""Packed place codes preserve their public bit layout and timestamp ordering."""

import pytest

from toolang.common.id_codec import decode_short_id, derive_id_key, encode_short_id
from toolang.common.ids import ID_EPOCH
from toolang.common.place_codes import (
    PLACE_CODE_EPOCH_MS,
    root_code,
    decode_place_code,
    encode_place_code,
    place_source_id,
    roaming_place_code,
    roaming_timestamp_ms,
    visiting_place_code,
)


@pytest.mark.parametrize("source", [0, 1, 2**48 - 1])
@pytest.mark.parametrize("elapsed", [0, 1, 2**45 - 1])
def test_roaming_codec_packs_type_hash_and_elapsed_milliseconds(source, elapsed):
    timestamp = PLACE_CODE_EPOCH_MS + elapsed
    token = encode_place_code("roaming", source, timestamp)
    assert len(token) == 19
    assert token[0] in "01234567"
    assert decode_short_id(token, width=19) == (source << 45) | elapsed
    assert decode_place_code(token, placement="roaming") == (source, timestamp)


@pytest.mark.parametrize("source", [0, 1, 2**48 - 1])
def test_visiting_codec_packs_its_type_and_bounded_source_field(source):
    token = encode_place_code("visiting", source)
    assert len(token) == 10
    assert token[0] in "rstvwxyz"
    assert decode_short_id(token, width=10) == (0b11 << 48) | source
    assert decode_place_code(token, placement="visiting") == (source, None)


@pytest.mark.parametrize(
    "source,timestamp",
    [
        (-1, PLACE_CODE_EPOCH_MS),
        (2**48, PLACE_CODE_EPOCH_MS),
        (0, PLACE_CODE_EPOCH_MS - 1),
        (0, PLACE_CODE_EPOCH_MS + 2**45),
        (True, PLACE_CODE_EPOCH_MS),
        (0, False),
        (0.0, PLACE_CODE_EPOCH_MS),
        (1, float(PLACE_CODE_EPOCH_MS)),
    ],
)
def test_packing_rejects_component_overflow_and_nonintegers(source, timestamp):
    with pytest.raises(ValueError, match="component"):
        encode_place_code("roaming", source, timestamp)


@pytest.mark.parametrize("source", [-1, 2**48, True, 1.0])
def test_visiting_hash_bounds_are_checked(source):
    with pytest.raises(ValueError, match="48-bit"):
        encode_place_code("visiting", source)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "0" * 18,
        "0" * 20,
        "r" + "0" * 8,
        "r" + "0" * 10,
        "s" + "0" * 17,
        "t" + "0" * 10,
        "r" + "A" * 9,
        "r" + "o" * 9,
        "R" + "0" * 9,
        "aid@" + "0" * 19,
        " " + "0" * 19,
        "0" * 19 + " ",
        None,
        0,
    ],
)
def test_decoder_rejects_partial_noncanonical_and_previous_codes(token):
    with pytest.raises(ValueError):
        decode_place_code(token)


@pytest.mark.parametrize("width,shift,valid", [(10, 48, 0b11), (19, 93, 0b00)])
@pytest.mark.parametrize("tag", range(4))
def test_decoder_checks_all_type_patterns_at_both_widths(width, shift, valid, tag):
    token = encode_short_id(tag << shift, width=width)
    if tag == valid:
        decode_place_code(token)
    else:
        with pytest.raises(ValueError, match="type"):
            decode_place_code(token)


def test_codec_requires_the_requested_placement_type():
    with pytest.raises(ValueError):
        decode_place_code(encode_place_code("visiting", 1), placement="roaming")
    with pytest.raises(ValueError):
        decode_place_code(
            encode_place_code("roaming", 1, PLACE_CODE_EPOCH_MS), placement="visiting"
        )


def test_place_epoch_matches_existing_id_families():
    assert PLACE_CODE_EPOCH_MS == 1_767_225_600_000
    assert PLACE_CODE_EPOCH_MS == int(ID_EPOCH.timestamp()) * 1_000


@pytest.mark.parametrize(
    "key,expected",
    [
        (bytes(32), "00000000"),
        (b"\xff" * 32, "zzzzzzzz"),
        (b"\x80" + bytes(31), "g0000000"),
        (bytes(4) + b"\x01" + bytes(27), "00000001"),
        (bytes(5) + b"\xff" * 27, "00000000"),
    ],
    ids=["zero", "maximum", "high-bit", "low-bit", "truncated-tail"],
)
def test_root_code_uses_all_40_hash_bits_in_eight_characters(
    monkeypatch, key, expected
):
    monkeypatch.setattr("toolang.common.place_codes.derive_id_key", lambda *_: key)
    assert root_code("/root") == expected


def test_hash_fields_use_distinct_framed_domains_and_raw_base32():
    root = "/root"
    ref = "gh:owner/agent"
    path = "/a.too"
    assert len(root_code(root)) == 8
    assert decode_short_id(root_code(root), width=8) == int.from_bytes(
        derive_id_key("place:v1", root)[:5], "big"
    )
    for placement, source, domain, token in (
        ("visiting", ref, "visiting:v1", visiting_place_code(ref)),
        ("roaming", path, "roaming:v1", roaming_place_code(path, PLACE_CODE_EPOCH_MS)),
    ):
        expected = int.from_bytes(derive_id_key(domain, source)[:6], "big")
        assert decode_place_code(token)[0] == expected
        assert decode_short_id(place_source_id(placement, source), width=10) == expected
    assert visiting_place_code(ref) != visiting_place_code("gh:other/agent")


def test_short_code_vectors_pin_the_domains_and_packed_layout():
    assert root_code("/root") == "4xfwwsny"
    assert visiting_place_code("gh:owner/agent") == "zskgcwa0vh"
    assert (
        roaming_place_code("/a.too", PLACE_CODE_EPOCH_MS + 32) == "2xwt5t3mm0000000010"
    )


def test_roaming_order_is_chronological_and_keeps_the_raw_source_prefix():
    elapsed = (0, 1, 31, 32, 1023, 1024, 3_599_999, 3_600_000, 86_400_000, 2**45 - 1)
    timestamps = [PLACE_CODE_EPOCH_MS + value for value in elapsed]
    tokens = [roaming_place_code("/a.too", value) for value in timestamps]
    assert tokens == sorted(tokens)
    assert [roaming_timestamp_ms(token) for token in tokens] == timestamps
    assert {token[:10] for token in tokens} == {place_source_id("roaming", "/a.too")}
    assert (
        roaming_timestamp_ms(roaming_place_code("/other.too", timestamps[3]))
        == timestamps[3]
    )

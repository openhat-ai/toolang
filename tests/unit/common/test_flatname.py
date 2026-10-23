"""Flat Name Encoding preserves strings without filesystem or URL interpretation."""

from itertools import product

import pytest

from toolang.common.flatname import decode_flatname, encode_flatname


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("", "+_"),
        ("Foo", "Foo"),
        ("foo", "foo"),
        ("/usr/local/bin", "%usr%local%bin"),
        (
            "https://example.com/a/b?q=1&x=2#top",
            "https+3a%%example.com%a%b+3fq=1&x=2#top",
        ),
        ("a%2Fb", "a+252Fb"),
        ("a+b", "a++b"),
        ("+2f", "++2f"),
        ("+_", "++_"),
        (r"C:\Users\Foo", "C+3a+5cUsers+5cFoo"),
        ("my file.txt", "my+20file.txt"),
        (" ", "+20"),
        ("file ", "file+20"),
        ("file. ", "file.+20"),
        ("con.", "con+2e"),
        (".", "+2e"),
        ("..", "+2e+2e"),
        ("name...", "name+2e+2e+2e"),
        (".env", ".env"),
        ("a..b", "a..b"),
        ("a/../b", "a%..%b"),
        ("\u4e2d\u6587", "+e4+b8+ad+e6+96+87"),
        ("\u00e9", "+c3+a9"),
        ("e\u0301", "e+cc+81"),
        ("\u0080", "+c2+80"),
        ("\u07ff", "+df+bf"),
        ("\u0800", "+e0+a0+80"),
        ("\ud7ff", "+ed+9f+bf"),
        ("\udbff", "+ed+af+bf"),
        ("\udc00", "+ed+b0+80"),
        ("\ue000", "+ee+80+80"),
        ("\uffff", "+ef+bf+bf"),
        ("\U00010000", "+f0+90+80+80"),
        ("\U0010ffff", "+f4+8f+bf+bf"),
        ("\U0001f600", "+f0+9f+98+80"),
        ("\x00", "+00"),
        ("\t\r\n\x7f", "+09+0d+0a+7f"),
        ('<>:"\\|?*', "+3c+3e+3a+22+5c+7c+3f+2a"),
        ("\ud800", "+ed+a0+80"),
        ("\udfff", "+ed+bf+bf"),
        ("\ud800\udc00", "+ed+a0+80+ed+b0+80"),
    ],
)
def test_canonical_examples(value, encoded):
    assert encode_flatname(value) == encoded
    assert decode_flatname(encoded) == value


def test_common_filename_and_url_characters_remain_readable():
    value = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        "-_.~!$&'(),;=@#[]{}^`"
    )
    assert encode_flatname(value) == value
    assert decode_flatname(value) == value


@pytest.mark.parametrize(
    "value", ["CON", "con", "nul", "NUL", "CON.txt", "NuL.log", "COM1", "LPT9"]
)
def test_windows_device_names_are_not_sanitized(value):
    assert encode_flatname(value) == value
    assert decode_flatname(value) == value


@pytest.mark.parametrize(
    ("tokens", "width"),
    [
        pytest.param(tuple(chr(code) for code in range(128)), 2, id="ascii-pairs"),
        pytest.param(
            (
                "/",
                "%",
                "+",
                "+_",
                "+2f",
                ".",
                " ",
                "\x00",
                "\x7f",
                "\u0080",
                "\u4e2d",
                "\ud800",
                "\udc00",
                "\U0010ffff",
            ),
            3,
            id="mixed-token-triples",
        ),
    ],
)
def test_strings_round_trip_without_collisions_or_forbidden_characters(tokens, width):
    values = {"", *tokens}
    values.update("".join(parts) for parts in product(tokens, repeat=width))
    encoded_values = set()
    for value in sorted(values):
        encoded = encode_flatname(value)
        assert decode_flatname(encoded) == value
        assert encoded and encoded.isascii()
        assert all(0x21 <= ord(char) < 0x7F for char in encoded)
        assert not set(encoded).intersection('<>:"/\\|?*')
        assert not encoded.endswith((" ", "."))
        encoded_values.add(encoded)
    assert len(encoded_values) == len(values)


def test_unicode_boundaries_and_normalization_variants_remain_distinct():
    values = [
        "\u00e9",
        "e\u0301",
        "\uac00",
        "\u1100\u1161",
        "\uffff",
        "\U00010000",
        "\U0010ffff",
        "\ud800\udc00",
        "\U0001f469\u200d\U0001f4bb\ufe0f",
    ]
    encoded_values = [encode_flatname(value) for value in values]
    assert len(set(encoded_values)) == len(values)
    assert [decode_flatname(encoded) for encoded in encoded_values] == values


def test_long_strings_are_neither_rejected_nor_truncated():
    value = "https://example.com/Foo/\u4e2d+%" * 1024 + "..."
    encoded = encode_flatname(value)
    assert len(encoded) > 255
    assert decode_flatname(encoded) == value


@pytest.mark.parametrize(
    ("encoded", "value"),
    [
        ("", ""),
        ("+41", "A"),
        ("+2f", "/"),
        ("+2F", "/"),
        ("+2B", "+"),
        ("+25", "%"),
        ("+2e", "."),
        ("+C3+A9", "\u00e9"),
        ("++2f", "+2f"),
        ("+2b2f", "+2f"),
        ("++_", "+_"),
        ("++++", "++"),
        ("%2F", "/2F"),
        ("+252F", "%2F"),
        ("+++2f", "+/"),
        ("+2b+5f", "+_"),
        ("+0000", "\x0000"),
        ("+4142", "A42"),
        ("pre+e4+B8+ad%++post+25+20+2e", "pre\u4e2d/+post% ."),
    ],
)
def test_decoding_accepts_aliases_without_recursive_or_url_decoding(encoded, value):
    assert decode_flatname(encoded) == value


@pytest.mark.parametrize("hex_format", ["02x", "02X"])
def test_decoding_adjacent_ascii_byte_escapes(hex_format):
    encoded = "".join(f"+{code:{hex_format}}" for code in range(128))
    assert decode_flatname(encoded) == "".join(chr(code) for code in range(128))


def test_decoding_literal_ascii_does_not_validate_filename_characters():
    value = "".join(chr(code) for code in range(128) if chr(code) not in "%+")
    assert decode_flatname(value) == value


@pytest.mark.parametrize(
    "encoded",
    [
        "+",
        "+0",
        "+g0",
        "+0g",
        "+ 0",
        "+0 ",
        "+-1",
        "+_x",
        "x+_",
        "+__",
        "+c3",
        "+80",
        "+c0+af",
        "+ed+a0",
        "+f4+90+80+80",
        "\u4e2d",
        "\ud800",
        "+++",
        "%+_",
        "prefix%++suffix+",
        "+c2",
        "+e0+a0",
        "+f0+90+80",
        "+c1+bf",
        "+e0+9f+bf",
        "+f0+8f+bf+bf",
        "+f5+80+80+80",
        "+fe",
        "+ff",
        "+c2A",
        "+e2%+82+ac",
        "+e4++b8+ad",
    ],
)
def test_decoding_rejects_malformed_tokens_and_bytes(encoded):
    with pytest.raises(ValueError):
        decode_flatname(encoded)

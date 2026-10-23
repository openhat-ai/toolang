"""Flat Name Encoding (FN Encoding) for readable, reversible filename spellings.

This codec preserves ASCII case and common URL/path punctuation. It does not
validate source strings, check filename lengths, or escape Windows device names.
"""

_ESCAPED_BYTES = frozenset(b'%<>:"\\|?*')
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def encode_flatname(value: str) -> str:
    """Encode a string as ASCII, using ``%`` for slashes and ``+hh`` for bytes."""
    if not value:
        return "+_"

    content = value.rstrip(".")
    parts: list[str] = []
    for byte in content.encode("utf-8", errors="surrogatepass"):
        if byte == 0x2F:
            parts.append("%")
        elif byte == 0x2B:
            parts.append("++")
        elif 0x21 <= byte <= 0x7E and byte not in _ESCAPED_BYTES:
            parts.append(chr(byte))
        else:
            parts.append(f"+{byte:02x}")

    parts.extend("+2e" for _ in range(len(value) - len(content)))
    return "".join(parts)


def decode_flatname(value: str) -> str:
    """Decode FN Encoding once, raising ``ValueError`` for malformed encodings.

    Hex digits may use either case, and literal bytes may be escaped. UTF-8
    uses ``surrogatepass`` to preserve isolated surrogates in Python strings.
    """
    if value == "+_":
        return ""
    if not value.isascii():
        raise ValueError("FN Encoding must contain only ASCII characters")

    decoded = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            decoded.append(0x2F)
            index += 1
        elif char == "+":
            if value[index : index + 2] == "++":
                decoded.append(0x2B)
                index += 2
            else:
                digits = value[index + 1 : index + 3]
                if len(digits) != 2 or any(c not in _HEX_DIGITS for c in digits):
                    raise ValueError(f"Invalid FN Encoding escape at position {index}")
                decoded.append(int(digits, 16))
                index += 3
        else:
            decoded.append(ord(char))
            index += 1

    return decoded.decode("utf-8", errors="surrogatepass")

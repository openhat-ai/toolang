"""Highlight queries and overlap selection remain independent of rendering."""

from toolang.lang.highlight import Capture, captures, resolve


def test_installed_query_recognizes_parameter_docs_and_unicode():
    source = "## @param _ 中文 🧭\nagic echo(_):\n  {{_}}\n".encode()
    found = captures(source)
    assert any(item.name == "comment.documentation" for item in found)
    assert any(
        item.name == "keyword" and source[item.start_byte : item.end_byte] == b"@param"
        for item in found
    )
    assert any(item.name == "variable.parameter" for item in found)
    assert any(
        item.name == "function" and source[item.start_byte : item.end_byte] == b"echo"
        for item in found
    )
    for item in resolve(found):
        source[item.start_byte : item.end_byte].decode()


def test_overlap_policy_handles_nested_partial_tied_and_empty_ranges():
    found = [
        Capture(0, 20, "comment", 9),
        Capture(2, 5, "string", 1),
        Capture(2, 5, "keyword", 2),
        Capture(3, 4, "property", 0),
        Capture(8, 12, "type", 2),
        Capture(10, 14, "function", 3),
        Capture(15, 18, "z", 3),
        Capture(15, 18, "a", 3),
        Capture(19, 19, "ignored"),
    ]
    expected = [
        (0, 2, "comment"),
        (2, 3, "keyword"),
        (3, 4, "property"),
        (4, 5, "keyword"),
        (5, 8, "comment"),
        (8, 10, "type"),
        (10, 14, "function"),
        (14, 15, "comment"),
        (15, 18, "a"),
        (18, 20, "comment"),
    ]
    for values in (found, list(reversed(found))):
        assert [
            (item.start_byte, item.end_byte, item.name) for item in resolve(values)
        ] == expected


def test_large_source_and_incomplete_source_are_highlightable():
    source = ("## docs\nagic echo:\n  Hi 🧭.\n" * 2000).encode()
    spans = resolve(captures(source))
    assert len(spans) >= 6000
    assert all(
        left.end_byte <= right.start_byte for left, right in zip(spans, spans[1:])
    )
    assert captures(b"agic echo(_:):\n")

from __future__ import annotations

import pytest

from toolang.lang.source_edit import (
    declaration_fragment,
    replace_declaration,
    source_declarations,
)


def test_declaration_edit_preserves_file_comments_unicode_and_neighbors() -> None:
    source = (
        "#!/usr/bin/env too\n##! Module documentation\n# File comment\n\n"
        "# Attached comment\n## Item documentation\nagic echo(_: Text):\n"
        "  # Body comment\n  Echo café: {{_}}\n\n"
        "# Separate file comment\n\nflow run():\n  run echo\n"
    )
    echo, flow = source_declarations(source)
    assert echo.key == "agic:echo"
    assert echo.line == 7
    assert echo.source.startswith("# Attached comment\n## Item documentation\n")
    assert echo.source.endswith("  Echo café: {{_}}\n")
    assert flow.key == "flow:run"
    edited = replace_declaration(source, echo, "agic echo(_: Text):\n  {{_}}")
    assert edited == (
        "#!/usr/bin/env too\n##! Module documentation\n# File comment\n\n"
        "agic echo(_: Text):\n  {{_}}\n\n"
        "# Separate file comment\n\nflow run():\n  run echo\n"
    )


@pytest.mark.parametrize(
    ("source", "key"),
    [
        ("agic:\n  Hello.\n", "agic:_"),
        ("flow:\n  pass\n", "flow:_"),
        ("instruct:\n  Hello.\n", "instruct:default"),
        ("context:\n  Hello.\n", "context:default"),
        ("instruct goal:\n  Hello.\n", "instruct:goal"),
        ("context facts:\n  Hello.\n", "context:facts"),
        ("struct Result:\n  passed: Boolean\n", "struct:Result"),
        ("psyche careful:\n  Be careful.\n", "psyche:careful"),
        ("skill review:\n  description = Review.\n  Review.\n", "skill:review"),
        (
            "service docs:\n  description = Docs.\n  protocol = http\n  target = https://example.com\n",
            "service:docs",
        ),
        ("prompt ask:\n  Ask.\n", "prompt:ask"),
        ("task work:\n  Work.\n", "task:work"),
        ("chore check:\n  Check.\n", "chore:check"),
    ],
)
def test_declaration_keys(source: str, key: str) -> None:
    assert declaration_fragment(source, key) == source


def test_nested_inline_agics_are_not_top_level_items() -> None:
    source = "flow:\n  repeat 2 times:\n    run: Think.\n"
    assert [item.key for item in source_declarations(source)] == ["flow:_"]


@pytest.mark.parametrize(
    "source",
    [
        "agic other:\n  Hi.\n",
        "agic echo:\n  Hi.\nagic other:\n  Hi.\n",
        "# Detached\n\nagic echo:\n  Hi.\n",
        "##! Module\nagic echo:\n  Hi.\n",
        "#!/usr/bin/env too\nagic echo:\n  Hi.\n",
        "agic echo:\n  Hi.\n# Footer\n",
        "with psyche careful\nagic echo:\n  Hi.\n",
    ],
)
def test_fragment_rejects_unowned_content(source: str) -> None:
    with pytest.raises(ValueError):
        declaration_fragment(source, "agic:echo")


def test_splice_preserves_crlf_and_missing_final_newline() -> None:
    source = "agic first:\r\n  Café.\r\n\r\nagic second:\r\n  Second."
    first, second = source_declarations(source)
    assert first.source == "agic first:\r\n  Café.\r\n"
    assert second.source == "agic second:\r\n  Second."
    assert replace_declaration(source, second, "agic second:\n  Changed.") == (
        "agic first:\r\n  Café.\r\n\r\nagic second:\n  Changed."
    )

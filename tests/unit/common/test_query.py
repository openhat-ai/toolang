import pytest
from toolang.common.errors import ToolangError
from toolang.common.policy import resolve_query_sentinels
from toolang.lang.format import format_query_text


def test_raw_query_formatting_preserves_nested_and_empty_matches() -> None:
    assert (
        format_query_text('one[family in (a,b)],,two[name="x,y"],')
        == 'one[family in (a,b)], , two[name="x,y"],'
    )


def test_policy_sentinels_are_standalone_and_quoted_identity_remains_a_query() -> None:
    assert resolve_query_sentinels(("all",), label="allow models") is None
    assert resolve_query_sentinels(("none",), label="allow models") == ()
    assert resolve_query_sentinels(('"all"',), label="allow models") == ('"all"',)
    with pytest.raises(ToolangError, match="cannot mix queries with all or none"):
        resolve_query_sentinels(("all,openai/*",), label="allow models")
    with pytest.raises(ToolangError, match="cannot mix queries with all or none"):
        resolve_query_sentinels(("openai/*", "none"), label="allow models")

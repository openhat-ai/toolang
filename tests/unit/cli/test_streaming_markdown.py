"""Incremental Markdown partitioning tests."""

from toolang.cli.common.execution_progress.streaming_markdown import (
    split_stable_markdown,
)


def test_single_markdown_block_remains_live() -> None:
    assert split_stable_markdown("one unfinished paragraph") == (
        "",
        "one unfinished paragraph",
    )


def test_later_block_makes_the_leading_block_stable() -> None:
    assert split_stable_markdown("# Heading\n\nParagraph") == (
        "# Heading\n\n",
        "Paragraph",
    )


def test_all_but_the_last_of_multiple_blocks_become_stable() -> None:
    assert split_stable_markdown("first\n\nsecond\n\nthird") == (
        "first\n\nsecond\n\n",
        "third",
    )


def test_list_is_not_split_until_a_later_top_level_block_starts() -> None:
    assert split_stable_markdown("- one\n- two\n\nafter") == (
        "- one\n- two\n\n",
        "after",
    )


def test_blank_lines_inside_a_fence_do_not_split_the_code_block() -> None:
    source = "```py\na = 1\n\nprint(a)\n```\n\nafter"
    assert split_stable_markdown(source) == (
        "```py\na = 1\n\nprint(a)\n```\n\n",
        "after",
    )

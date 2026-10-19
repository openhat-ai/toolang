"""Formatting uses grammar-owned structural comment and literal text boundaries."""

import pytest

from toolang.lang import Program, format_source
from toolang.lang.cst import error_nodes, parse


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "comment",
    [
        '# key=value  ->  result,none,, "a#b"',
        "# Keep  01 lanes and 2 time = literal.",
        "# Résumé: x=y ->  Text,  keep,,",
    ],
)
@pytest.mark.parametrize(
    "source",
    [
        "with skill ./résumé {comment}\n",
        "struct Result: {comment}\n  value:Text\n",
        "struct Result:\n  value:Text {comment}\n",
        "chore work: {comment}\n  Work.\n",
        "chore work:\n  description = Résumé {comment}\n  Work.\n",
        "agic work(): {comment}\n  pass\n",
        "flow work(): {comment}\n  pass\n",
        'agic work():\n  tools = vendor/tool#v1[description="Résumé # exact"] {comment}\n  user: Work.\n',
        "agic work():\n  context = none {comment}\n  user: Work.\n",
        "flow work():\n  run worker {comment}\n",
        "flow work():\n  let result = run worker {comment}\n",
        "flow work():\n  async run worker {comment}\n",
        "flow work():\n  let handle=async run worker {comment}\n",
        "flow work():\n  spawn worker {comment}\n",
        "flow work():\n  exec worker {comment}\n",
        "flow work():\n  seek reviewer worker {comment}\n",
        "flow work():\n  await handle {comment}\n",
        "flow work():\n  generate 02 in 01 lanes using worker {comment}\n",
        "flow work():\n  map in 01 lanes using worker {comment}\n",
        "flow work():\n  keep in 01 lanes if predicate {comment}\n",
        "flow work():\n  drop last 01 {comment}\n",
        "flow work():\n  sort ascending in 2 lane by score {comment}\n",
        "flow work():\n  reduce using worker {comment}\n",
        "flow work():\n  repeat 01 times: {comment}\n    let value = Body.\n    until ready {comment}\n",
    ],
)
def test_structural_comment_bytes_survive_formatting(source, comment, newline):
    source = source.format(comment=comment).replace("\n", newline)
    assert list(error_nodes(parse(source.encode()).root_node)) == []
    formatted = format_source(source)
    assert formatted.count(comment) == source.count(comment)
    for line in formatted.splitlines():
        if line.endswith(comment):
            prefix = line.removesuffix(comment)
            assert prefix.endswith("  ") and not prefix.endswith("   ")
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "source",
    [
        "flow work():\n  let note = {text}\n",
        "flow work():\n  run: {text}\n",
        "flow work():\n  ask: {text}\n",
        "flow work():\n  repeat 1 time:\n    let value = Body.\n    until: {text}\n",
        "flow work():\n  run -> Text[]: Items.\n  reduce:\n    Merge {{_}}.\n    from: {text}\n",
        "agic work():\n  user: {text}\n",
        "context notes: {text}\n",
        "instruct notes: {text}\n",
        "prompt notes:\n  Begin.\n  {text}\n",
        "chore notes:\n  Begin.\n  {text}\n",
    ],
)
def test_comment_like_literal_content_stays_text(source, newline):
    text = '# Keep  01 lanes and 2 time = literal,none,, "a#b" -> Text.'
    source = source.replace("{text}", text).replace("\n", newline)
    formatted = format_source(source)
    assert text in formatted
    assert format_source(formatted) == formatted
    for candidate in (source, formatted):
        Program.from_source(candidate)


def test_bom_and_unicode_reference_do_not_shift_the_comment_boundary():
    source = "\ufeffwith skill ./résumé # Keep x=y  -> z,,\n"
    assert format_source(source) == "\ufeffwith skill ./résumé  # Keep x=y  -> z,,\n"

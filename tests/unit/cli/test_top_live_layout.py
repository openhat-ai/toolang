"""Terminal alignment, scope totals and plain spend presentation."""

import io

from prompt_toolkit.keys import Keys
from rich.console import Console
from toolang.cli.common.activity_view import Activity, cost
from toolang.execution.schemas import ActivityMetrics
from tests.unit.cli.test_activity_view import page, feed


def render(state, width=180):
    output = io.StringIO()
    Console(file=output, width=width, height=30, color_system=None).print(
        state.render(width=width, height=30)
    )
    return output.getvalue().splitlines()


def test_numbers_align_with_headers_and_footer_stays_at_bottom():
    snapshot = page()
    snapshot.agent = "agent:爱丽丝"
    snapshot.stats = ActivityMetrics(
        model=24,
        tool=46,
        input_tokens=128400,
        cached_tokens=96000,
        output_tokens=12800,
        cost=1.28,
        time=750,
        tokens_complete=True,
    )
    state = Activity(None)
    feed(state, snapshot)
    lines = render(state)
    header = next(line for line in lines if line.startswith("AGENT"))
    row = next(line for line in lines if line.startswith("爱丽丝"))
    from rich.cells import cell_len

    for title, value in (
        ("IN", "128.4k"),
        ("CACHED", "96.0k"),
        ("OUT", "12.8k"),
        ("SPEND", "$1.28"),
        ("TIME+", "12m30s"),
    ):
        assert cell_len(header[: header.index(title) + len(title)]) == cell_len(
            row[: row.index(value) + len(value)]
        )
    assert (
        header.index("IN")
        < header.index("CACHED")
        < header.index("OUT")
        < header.index("SPEND")
    )
    assert len(lines) == 30 and "F10Quit" in lines[-1]
    before = header.index("SPEND")
    snapshot.stats.model = 999
    feed(state, snapshot)
    assert (
        next(line for line in render(state) if line.startswith("AGENT")).index("SPEND")
        == before
    )
    assert cost(ActivityMetrics(cost=1.28, estimated=True, partial=True)) == "$1.28"


def test_markdown_result_uses_runnable_name_in_the_list():
    snapshot = page()
    snapshot.roots[1].title = "review_project"
    snapshot.roots[1].summary = (
        "**Handling summary** **Batch** · " + "Long reply. " * 20
    )
    state = Activity(None, view="execution")
    feed(state, snapshot)
    assert state.rows()[-1].activity == "succeeded · review_project"


def test_narrow_details_can_reach_every_field_and_result_line():
    snapshot = page()
    state = Activity("agent:alice", view="execution")
    snapshot.roots[1].stats = ActivityMetrics(
        input_tokens=123456789,
        cached_tokens=23456789,
        output_tokens=34567890,
        cost=1.28,
        estimated=True,
    )
    feed(state, snapshot)
    state.selected = state.rows()[-1].key
    state.details = True
    state.result_key = (*state.selected, "succeeded")
    state.result_text = "\n".join(f"Result line {i}" for i in range(30))
    seen = []
    for _ in range(40):
        output = io.StringIO()
        Console(file=output, width=80, height=18, color_system=None).print(
            state.render(width=80, height=18)
        )
        lines = output.getvalue().splitlines()
        assert len(lines) == 18 and "F10Quit" in lines[-1]
        seen.append(output.getvalue())
        state.key(Keys.PageDown)
    text = "\n".join(seen)
    assert "123456789" in text and "23456789" in text and "34567890" in text
    assert "Spend source: estimated" in text
    assert "Result line 29" in text

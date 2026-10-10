"""Dashboard layout, pending windows, selection and styled result paging."""

import io

import pytest
from prompt_toolkit.keys import Keys
from rich.cells import cell_len
from rich.console import Console

from toolang.cli.common.activity_view import Activity
from tests.unit.cli.test_activity_view import feed, page


def render(state, width=180, height=24):
    stream = io.StringIO()
    Console(file=stream, width=width, height=height, color_system=None).print(
        state.render(width=width, height=height)
    )
    return stream.getvalue().splitlines()


@pytest.mark.parametrize(
    "agent, title",
    [(None, "Team"), ("agent:alice", "Agent alice"), ("agent:team", "Agent team")],
)
def test_full_screen_identity_clock_settings_and_single_agent_row(agent, title):
    state = Activity(agent, view="agent")
    snapshot = page()
    snapshot.agent = agent or snapshot.agent
    feed(state, snapshot)
    lines = render(state)
    assert len(lines) == 24 and lines[0].startswith(title + "  ")
    assert len(lines[0].rstrip()[-8:].split(":")) == 3
    assert "Stats: session  Activity: 30m" in "\n".join(lines[:4])
    assert "q Quit" in lines[-1]
    assert len(state.rows()) == 1
    assert "Refresh" not in "\n".join(lines)


@pytest.mark.parametrize(
    "view, tree, columns",
    [
        ("agent", False, ()),
        ("thread", False, ("THREAD",)),
        ("execution", False, ("THREAD", "RUN")),
        ("execution", True, ("THREAD", "RUN", "STEP")),
    ],
)
def test_columns_express_level_and_sort(view, tree, columns):
    state = Activity(None, view=view, tree=tree)
    feed(state, page())
    heading = next(
        line for line in render(state, width=220) if line.startswith("AGENT")
    )
    assert (
        tuple(label for label in ("THREAD", "RUN", "STEP") if label in heading)
        == columns
    )
    assert " S " in heading and "TIME+" in heading and "ACTIVITY(30m)↓" in heading
    state.key(Keys.F6)
    heading = next(
        line for line in render(state, width=220) if line.startswith("AGENT")
    )
    assert "SPEND↓" in heading and "ACTIVITY(30m)↓" not in heading


def test_window_change_hides_old_values_until_matching_checkpoint():
    state = Activity(None)
    feed(state, page())
    state.key(Keys.F8)
    before = "\n".join(render(state))
    assert "Stats: 1h" in before and "Updating" in before and "$1.28" not in before
    state.attach()
    replacement = page()
    replacement.since = "1h"
    replacement.stats.cost = 0.2
    state.feed("activity_page", replacement.model_dump())
    assert "$0.20" not in "\n".join(render(state))
    state.feed("activity_checkpoint", {"agents": [replacement.agent]})
    after = "\n".join(render(state))
    assert "$0.20" in after and "Updating" not in after


@pytest.mark.parametrize("width,height", [(80, 18), (120, 24), (220, 30), (40, 5)])
def test_rows_never_wrap_or_add_ellipsis(width, height):
    snapshot = page()
    snapshot.roots[0].title = "agent::agic:" + "中a\u0301" * 200
    snapshot.paths = []
    state = Activity(None, view="execution")
    feed(state, snapshot)
    lines = render(state, width, height)
    assert len(lines) == height
    assert all(cell_len(line) <= width for line in lines)
    assert all("…" not in line for line in lines)
    assert "agent::" not in "\n".join(lines)
    assert state.rows()[0].activity.startswith("agic:")


def test_ctrl_navigation_follows_details_and_markdown_is_cached():
    state = Activity("agent:alice", view="execution")
    feed(state, page())
    state.details = True
    render(state)
    first = state.selected
    state.key(Keys.ControlN)
    assert state.selected is not None and state.selected != first
    state.result_key = (*state.selected, "succeeded")
    state.result_text = (
        "# Result title\n\n**Important result**\n\n```python\nanswer = 42\n```"
    )
    render(state)
    plain = "\n".join(line.plain for line in state.result_lines)
    assert (
        "Result title" in plain
        and "Important result" in plain
        and "answer = 42" in plain
    )
    assert "# Result title" not in plain and "**" not in plain and "```" not in plain
    assert any(line.spans for line in state.result_lines)
    cached = state.result_lines
    render(state)
    assert state.result_lines is cached
    state.details_offset = 10
    state.key(Keys.ControlP)
    render(state)
    assert state.selected == first and state.details_offset == 0


def test_details_reports_matching_counts_for_the_selected_level():
    from toolang.cli.common.activity_dashboard import details
    from toolang.execution.activity import ActivityQuery

    state = Activity(None, view="thread", query=ActivityQuery(text="one"))
    snapshot = page()
    snapshot.filter = "one"
    snapshot.thread_matched = 1
    snapshot.thread_eligible = 4
    feed(state, snapshot)
    state.details = True
    console = Console(width=180)
    render(state)
    text = "\n".join(line.plain for line in details(state, state.rows(), console, 180))
    assert "Threads: 1/4 matched  Loaded: 1/1" in text
    state.key("e")
    render(state)
    text = "\n".join(line.plain for line in details(state, state.rows(), console, 180))
    assert "Runs: 2/2 matched  Loaded: 2/2" in text


def test_large_history_formats_only_visible_table_rows(monkeypatch):
    from toolang.cli.common import activity_dashboard
    from tests.unit.cli.test_activity_view import node

    snapshot = page()
    snapshot.paths = []
    snapshot.roots = [
        node(f"run_{i:08}", root=f"run_{i:08}", status="succeeded") for i in range(1000)
    ]
    state = Activity(None, view="execution")
    feed(state, snapshot)
    state.selected = (snapshot.agent, snapshot.roots[-1].id)
    calls = 0
    original = activity_dashboard.cost

    def cost(metrics):
        nonlocal calls
        calls += 1
        return original(metrics)

    monkeypatch.setattr(activity_dashboard, "cost", cost)
    lines = render(state, height=24)
    assert any("run_00000999" in line for line in lines)
    assert calls <= 24, "Offscreen rows must not incur terminal formatting work"
    # Redirected/single snapshots still include all rows.
    once = state.render(width=180, height=24, once=True)
    output = io.StringIO()
    Console(file=output, width=180, color_system=None).print(once)
    assert sum("run_" in line for line in output.getvalue().splitlines()) == 1000


@pytest.mark.parametrize("stale,reconnecting", [(True, False), (False, True)])
def test_header_does_not_claim_idle_from_stale_counts(stale, reconnecting):
    snapshot = page()
    snapshot.active = snapshot.failed = 0
    snapshot.stale = stale
    state = Activity(None)
    feed(state, snapshot)
    state.reconnecting = reconnecting
    lines = render(state)
    assert "idle" not in lines[1]


def test_three_line_terminal_keeps_identity_headings_and_status():
    state = Activity(None)
    feed(state, page())
    lines = render(state, height=3)
    assert len(lines) == 3
    assert lines[0].startswith("Team")
    assert lines[1].startswith("AGENT")
    assert "q Quit" in lines[2]

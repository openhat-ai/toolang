"""Dashboard layout, pending windows, selection and styled result paging."""

import io
import re

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


@pytest.mark.parametrize("width", [40, 80, 180])
def test_header_height_and_separator_are_stable_across_updates(width):
    from dataclasses import replace

    state = Activity(None)
    snapshot = page()
    feed(state, snapshot)
    initial = render(state, width)
    heading = next(i for i, line in enumerate(initial) if line.startswith("AGENT"))
    assert not initial[heading - 1].strip()
    state.query = replace(state.query, text="review", active=True, since="1d")
    state.recent_label = "1w"
    snapshot.stats.model = 100000
    snapshot.failed = 100
    snapshot.since = state.query.since
    snapshot.filter = state.query.text
    snapshot.active_only = state.query.active
    state.attach()
    feed(state, snapshot)
    changed = render(state, width)
    assert state.snapshots[snapshot.agent].stats.model == 100000
    assert "100000" in "\n".join(changed[:heading])
    assert changed[heading].startswith("AGENT")
    assert not changed[heading - 1].strip()


@pytest.mark.parametrize("width", [40, 80, 180])
def test_header_reflow_aligns_values_within_each_metric_column(width):
    from toolang.cli.common.activity_dashboard import header

    state = Activity(None)
    feed(state, page())
    columns = {}
    for line in header(state, width):
        for metric in re.finditer(
            r"(Threads|Runs|Models|Tools|In|Cached|Out|Spend):\s+(\S+)", line.plain
        ):
            columns.setdefault(metric.start(), set()).add(metric.start(2))
    assert len(columns) == (4 if width == 180 else 2)
    assert all(len(starts) == 1 for starts in columns.values())


@pytest.mark.parametrize("agent", [None, "agent:alice"])
@pytest.mark.parametrize("eligible,active,failed", [(0, 0, 0), (7, 0, 0), (7, 2, 1)])
def test_header_runs_counts_activity_eligible_runs_before_filters(
    agent, eligible, active, failed
):
    from toolang.cli.common.activity_dashboard import header
    from toolang.execution.activity import ActivityQuery

    state = Activity(agent, query=ActivityQuery(text="unmatched", active=True))
    snapshot = page()
    snapshot.filter = state.query.text
    snapshot.active_only = True
    snapshot.eligible = eligible
    snapshot.active, snapshot.failed = active, failed
    snapshot.matched = snapshot.available = 0
    snapshot.roots = snapshot.paths = []
    feed(state, snapshot)
    text = "\n".join(line.plain for line in header(state, 180))
    assert re.findall(r"Runs:\s+(\S+)", text) == [str(eligible)]


def test_team_header_runs_aggregates_agents():
    from toolang.cli.common.activity_dashboard import header

    state = Activity(None)
    alice, bob = page(), page()
    bob.agent = "agent:bob"
    alice.eligible, bob.eligible = 7, 3
    for snapshot in (alice, bob):
        state.feed("activity_page", snapshot.model_dump())
    state.feed("activity_checkpoint", {"agents": [alice.agent, bob.agent]})
    text = "\n".join(line.plain for line in header(state, 180))
    assert re.findall(r"Runs:\s+(\S+)", text) == ["10"]


def test_status_bar_contains_only_function_key_hints_and_no_incomplete():
    from toolang.cli.common.activity_dashboard import status_bar

    state = Activity(None)
    snapshot = page()
    snapshot.complete = False
    feed(state, snapshot)
    bar = status_bar(state, 180)
    for hint in (
        "F1Help",
        "F4Filter",
        "F5View",
        "F6Sort",
        "F7Activity",
        "F8Stats",
        "F10Quit",
    ):
        assert hint in bar.plain
    for hidden in ("a Agent", "t Thread", "e Run", "Enter", "q Quit", "Incomplete"):
        assert hidden not in bar.plain
    console = Console()
    assert (
        bar.get_style_at_offset(console, 0).bgcolor
        != bar.get_style_at_offset(console, 2).bgcolor
    )


@pytest.mark.parametrize("width", [40, 50, 80])
def test_narrow_status_bar_keeps_complete_key_cells_and_quit(width):
    from toolang.cli.common.activity_dashboard import status_bar

    state = Activity(None)
    feed(state, page())
    bar = status_bar(state, width).plain
    assert "F1Help" in bar and "F5View" in bar and "F10Quit" in bar
    assert set(bar.split()) <= {
        "F1Help",
        "F4Filter",
        "F5View",
        "F6Sort",
        "F7Activity",
        "F8Stats",
        "F10Quit",
    }


@pytest.mark.parametrize("text", ["configuration-source-" * 8, "配置e\u0301" * 80])
def test_long_filter_keeps_input_tail_and_cursor_visible(text):
    from toolang.cli.common.activity_dashboard import status_bar

    state = Activity(None)
    state.key(Keys.F4)
    state.key(Keys.BracketedPaste, text + "last")
    bar = status_bar(state, 40).plain
    assert bar.startswith("Filter: ") and "last█" in bar
    assert "Active:" in bar and cell_len(bar) == 40


@pytest.mark.parametrize("other_thread", [False, True])
def test_f5_cycles_all_views_and_restores_tree_selection_without_resubscribing(
    other_thread,
):
    from tests.unit.cli.test_activity_view import node

    state = Activity(None)
    snapshot = page()
    if other_thread:
        thread = node("term_other", kind="thread", thread="term_other")
        thread.changed = 200
        root = node("run_other", root="run_other", thread="term_other")
        root.changed = 200
        snapshot.threads.append(thread)
        snapshot.roots.append(root)
    feed(state, snapshot)
    query = state.query
    for view, tree in (("thread", False), ("execution", False), ("execution", True)):
        state.key(Keys.F5)
        assert (state.view, state.tree) == (view, tree)
    state.selected = ("agent:alice", "run_child.2")
    for view in ("agent", "thread", "execution", "execution"):
        state.key(Keys.F5)
        assert state.view == view
    assert state.tree and state.selected == ("agent:alice", "run_child.2")
    assert state.query == query and not state.dirty
    assert state.key(Keys.F10)


@pytest.mark.parametrize("width", [40, 80])
def test_narrow_table_keeps_columns_and_clips_without_horizontal_scrolling(width):
    from toolang.cli.common.activity_dashboard import clip, table

    state = Activity(None, view="execution", tree=True)
    feed(state, page())
    heading, rows = table(state, state.rows(), 220, False)
    narrow_heading, narrow_rows = table(state, state.rows(), width, False)
    assert "MODEL" in narrow_heading.plain and "TOOL" in narrow_heading.plain
    assert narrow_heading.plain == clip(heading, width).plain
    assert [row.plain for row in narrow_rows] == [
        clip(row, width).plain for row in rows
    ]
    for key in (">", "<"):
        state.key(key)
        actual_heading, actual_rows = table(state, state.rows(), width, False)
        assert actual_heading == narrow_heading and actual_rows == narrow_rows


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
    assert "F10Quit" in lines[-1]
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
def test_header_keeps_stale_counts_unknown(stale, reconnecting):
    snapshot = page()
    snapshot.active = snapshot.failed = 0
    snapshot.stale = stale
    state = Activity(None)
    feed(state, snapshot)
    state.reconnecting = reconnecting
    lines = render(state)
    assert re.findall(r"(?:Runs|Threads):\s+(\S+)", lines[1]) == ["-", "-"]


def test_three_line_terminal_keeps_identity_headings_and_status():
    state = Activity(None)
    feed(state, page())
    lines = render(state, height=3)
    assert len(lines) == 3
    assert lines[0].startswith("Team")
    assert lines[1].startswith("AGENT")
    assert "F10Quit" in lines[2]


@pytest.mark.parametrize("reconnecting", [False, True])
def test_result_reload_waits_for_recovered_checkpoint(reconnecting):
    state = Activity("agent:alice", view="execution")
    snapshot = page()
    feed(state, snapshot)
    state.details = True
    key = (snapshot.agent, "run_done", "succeeded")
    state.selected = key[:2]
    state.result_key = key
    state.result_text = "Unavailable"
    state.reconnecting = reconnecting
    state.attach()
    state.feed("activity_page", snapshot.model_dump())
    assert state.result_key == key
    state.feed("activity_checkpoint", {"agents": [snapshot.agent]})
    assert state.result_key == (None if reconnecting else key)
    assert state.selected == key[:2]


@pytest.mark.parametrize("offline", [False, True])
def test_run_details_describe_selected_run_independently_of_table(offline):
    from toolang.cli.common.activity_dashboard import details

    snapshot = page()
    snapshot.roots[0].title = "agent::flow:review_project"
    if offline:
        snapshot.presence = "offline"
        snapshot.roots[0].status = "succeeded"
        snapshot.roots[0].summary = "Configuration updated"
    state = Activity("agent:alice", view="execution")
    feed(state, snapshot)
    state.selected = (snapshot.agent, "run_root")
    state.details = True
    rows = state.rows()
    text = "\n".join(line.plain for line in details(state, rows, Console(), 180))
    assert "flow:review_project" in text
    assert "agent::" not in text
    assert rows[0].activity == ("-" if offline else "run_child.2")


def test_step_details_do_not_inherit_table_tree_prefixes():
    from toolang.cli.common.activity_dashboard import details

    snapshot = page()
    snapshot.paths[-1].title = "model-name"
    snapshot.paths[-1].summary = "Analyze configuration"
    state = Activity("agent:alice", view="execution", tree=True)
    feed(state, snapshot)
    state.selected = (snapshot.agent, "run_child.2")
    state.details = True
    rows = state.rows()
    assert "└─" in next(row.activity for row in rows if row.key == state.selected)
    text = "\n".join(line.plain for line in details(state, rows, Console(), 180))
    assert "model-name · Analyze configuration" in text
    assert "└─" not in text

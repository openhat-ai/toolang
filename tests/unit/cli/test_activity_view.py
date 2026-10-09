"""Top ownership, active trees, selection, and range controls."""

import io

import pytest
from prompt_toolkit.keys import Keys
from rich.console import Console

from toolang.cli.common.activity_view import Activity, duration, since
from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivityMetrics, ActivityNode, ActivitySnapshot


def node(
    ref,
    *,
    kind="run",
    parent=None,
    status="running",
    root="run_root",
    thread="term_one",
    cost=0.5,
):
    return ActivityNode(
        id=ref,
        kind=kind,
        thread=thread,
        root=root,
        parent=parent,
        status=status,
        title=ref,
        changed=10,
        created=1,
        stats=ActivityMetrics(model=1, cost=cost),
    )


def page():
    return ActivitySnapshot(
        agent="agent:alice",
        revision=1,
        session="session",
        observed=100,
        since="session",
        recent=1800,
        stats=ActivityMetrics(model=24, tool=46, cost=1.28),
        active=1,
        eligible=2,
        matched=2,
        available=2,
        threads=[node("term_one", kind="thread")],
        roots=[node("run_root"), node("run_done", status="succeeded", root="run_done")],
        paths=[
            node("run_root.1", kind="step", parent="run_root", status="succeeded"),
            node("run_child", parent="run_root.1"),
            node("run_child.2", kind="step", parent="run_child"),
        ],
    )


def feed(state, snapshot):
    state.feed("activity_page", snapshot.model_dump())
    state.feed("activity_checkpoint", {"agents": [snapshot.agent]})


def test_tree_uses_root_ownership_and_complete_step_refs():
    state = Activity(None, view="execution", tree=True)
    feed(state, page())
    rows = state.rows()
    assert [(row.root, row.id) for row in rows] == [
        ("run_root", "run_root"),
        ("run_root", "run_root.1"),
        ("run_root", "run_child"),
        ("run_root", "run_child.2"),
        ("run_done", "run_done"),
    ]
    assert rows[1].activity == "└─ succeeded · run_root.1"
    assert rows[2].activity == "   └─ run_child"
    assert rows[3].parent == "run_child"
    assert rows[3].stats.model == 1
    assert rows[0].stats.model != state.snapshots["agent:alice"].stats.model
    state.selected = rows[2].key
    state.key(Keys.Left)
    assert [row.id for row in state.rows()] == [
        "run_root",
        "run_root.1",
        "run_child",
        "run_done",
    ]
    state.key(Keys.Right)
    assert len(state.rows()) == 5


def test_root_completion_atomically_removes_tree_and_selects_root():
    state = Activity(None, view="execution", tree=True)
    snapshot = page()
    feed(state, snapshot)
    state.selected = (snapshot.agent, "run_child.2")
    snapshot.roots[0].status = "succeeded"
    snapshot.paths = []
    snapshot.revision = 2
    state.feed("activity_page", snapshot.model_dump())
    assert len(state.rows()) == 5
    state.feed("activity_checkpoint", {"agents": [snapshot.agent]})
    assert len(state.rows()) == 2
    assert state.selected == (snapshot.agent, "run_root")


def test_offline_tree_collapses_and_reconnecting_does_not_invent_offline():
    state = Activity(None, view="execution", tree=True)
    snapshot = page()
    snapshot.presence = "offline"
    snapshot.stale = True
    feed(state, snapshot)
    assert len(state.rows()) == 2
    assert state.rows()[0].activity.startswith("stale")
    state.reconnecting = True
    state.key("a")
    assert state.rows()[0].activity.startswith("unknown")


@pytest.mark.parametrize("view", ["agent", "thread", "execution"])
def test_unobserved_offline_agent_does_not_invent_last_seen_or_syncing(view):
    snapshot = page().model_copy(
        update=dict(
            session=None,
            observed=None,
            presence="offline",
            stale=True,
            complete=False,
            roots=[],
            paths=[],
            threads=[],
            active=0,
        )
    )
    state = Activity(None, view=view)
    feed(state, snapshot)
    assert state.rows()[0].activity == "offline · activity unavailable"


def test_offline_incomplete_snapshot_keeps_last_seen_without_claiming_syncing():
    snapshot = page()
    snapshot.presence = "offline"
    snapshot.stale = True
    snapshot.complete = False
    state = Activity(None)
    feed(state, snapshot)
    summary = state.rows()[0].activity
    assert "last seen" in summary and "counts incomplete" in summary
    assert "syncing" not in summary


def test_view_controls_do_not_change_query_and_editors_consume_shortcuts():
    state = Activity(None)
    feed(state, page())
    query = state.query
    for key in ("t", "e", Keys.F5, Keys.F6, "a"):
        state.key(key)
    assert state.query == query
    assert not state.dirty
    state.key(Keys.F4)
    state.key("e")
    assert state.view == "agent"
    assert state.buffer == "e"
    state.key(Keys.ControlM)
    assert state.query.text == "e"
    assert state.dirty
    assert state.display_query.text == ""


def test_filter_cancel_discards_active_toggle_and_text():
    state = Activity(None)
    state.key(Keys.F4)
    state.key(Keys.ControlA)
    state.key("a")
    state.key(Keys.Escape)
    assert not state.query.active
    assert state.query.text == ""
    assert not state.dirty
    state.key(Keys.F4)
    state.key(Keys.ControlA)
    state.key(Keys.BracketedPaste, "math__double")
    state.key(Keys.ControlM)
    assert state.query.active and state.query.text == "math__double"
    assert state.dirty


@pytest.mark.parametrize("value", ["9" * 24 + "w", "9" * 400 + "w"])
def test_extreme_stats_range_is_a_validation_error(value):
    with pytest.raises(ValueError):
        since(value)


def test_nonfinite_duration_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        duration("9" * 400 + "w")


def test_single_agent_header_and_tree_columns_at_narrow_width():
    state = Activity("agent:alice", view="execution", tree=True)
    feed(state, page())
    output = io.StringIO()
    console = Console(file=output, width=160, color_system=None)
    console.print(state.render(width=160, once=True))
    text = output.getvalue()
    assert "Agent Stats" in text and "RUN" in text and "STEP" in text
    assert "run_child.2" in text
    assert "AGENT" not in text
    state.key("a")
    assert state.rows() == []


def test_duration_start_is_fixed_and_explicit_stats_does_not_change_recent():
    assert since("1h", now=3600) == "1970-01-01T00:00:00+00:00"
    state = Activity(None, query=ActivityQuery())
    state.key(Keys.F8)
    state.key(Keys.ControlU)
    for key in "all":
        state.key(key)
    state.key(Keys.ControlM)
    assert state.query.since == "all"
    assert state.query.recent == 1800


def test_query_edit_cannot_relabel_frames_from_previous_subscription():
    state = Activity(None)
    feed(state, page())
    state.attach()
    state.feed("activity_page", page().model_dump())
    state.key(Keys.F8)
    state.key(Keys.ControlU)
    for key in "all":
        state.key(key)
    state.key(Keys.ControlM)
    state.feed("activity_checkpoint", {"agents": ["agent:alice"]})
    assert state.display_query.since == "session"
    state.attach()
    replacement = page()
    replacement.since = "all"
    replacement.stats.cost = 9
    feed(state, replacement)
    assert state.display_query.since == "all"
    assert state.snapshots[replacement.agent].stats.cost == 9


@pytest.mark.parametrize("details", [False, True])
def test_narrow_terminal_keeps_selected_row_and_footer_visible(details):
    snapshot = page()
    snapshot.session_start = 1791540000
    snapshot.observed = 1791540100
    snapshot.roots = [
        node(f"run_{index:08}", root=f"run_{index:08}", thread="script_ab123456")
        for index in range(40)
    ]
    state = Activity("agent:alice", view="execution")
    feed(state, snapshot)
    state.selected = (snapshot.agent, snapshot.roots[-1].id)
    state.details = details
    output = io.StringIO()
    console = Console(file=output, width=80, height=24, color_system=None)
    console.print(state.render(width=80, height=24))
    lines = output.getvalue().splitlines()
    assert len(lines) <= 24
    assert any(
        line.startswith("     $0.50") and "run_00000039" in line for line in lines
    )
    assert "Inspect: too alice inspect run_00000039" in output.getvalue()
    assert "q Quit" in lines[-1]


def test_header_keeps_unavailable_statistics_unknown():
    snapshot = page()
    snapshot.stats = ActivityMetrics(
        model=None, tool=None, cost=None, time=None, complete=False
    )
    snapshot.complete = False
    state = Activity(None)
    feed(state, snapshot)
    output = io.StringIO()
    Console(file=output, width=160).print(state.render(width=160, once=True))
    assert "Agent Stats: MODEL -  TOOL -  COST -  TIME -" in output.getvalue()


def test_list_summary_follows_tree_execution_order():
    snapshot = page()
    later = node("run_root.2", kind="step", parent="run_root")
    later.position = [2]
    earlier = node("run_root.1", kind="step", parent="run_root")
    earlier.position = [1]
    earlier.created = 5  # Parallel work can begin in a different order.
    snapshot.paths = [later, earlier]
    state = Activity(None, view="execution")
    feed(state, snapshot)
    assert state.rows()[0].activity == "run_root.1 · 2 current calls"
    state.key(Keys.F5)
    assert [row.id for row in state.rows()[:3]] == [
        "run_root",
        "run_root.1",
        "run_root.2",
    ]


def test_header_counts_follow_the_view_without_counting_tree_rows():
    snapshot = page()
    snapshot.thread_count = 7
    snapshot.thread_eligible = 4
    snapshot.thread_matched = 1
    state = Activity(None, view="thread")
    feed(state, snapshot)

    def rendered():
        output = io.StringIO()
        Console(file=output, width=240).print(state.render(width=240, once=True))
        return output.getvalue()

    assert "Threads 1/4 matched/eligible" in rendered()
    state.key("a")
    assert "Agents 1/1 matched/eligible" in rendered()
    state.key("e")
    state.key(Keys.F5)
    assert "Root runs 2/2 matched/eligible" in rendered()
    assert "Loaded 2/2" in rendered()

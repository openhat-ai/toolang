"""Top presents exact owned identities independently of accounting and I/O."""

from contextlib import closing
from dataclasses import replace
import json

import pytest
from rich.cells import cell_len
from rich.console import Console
from typer.testing import CliRunner
from tests.support.execution_fixtures import project_run_start
from toolang.base.types.message import Message
from toolang.common.layout import AgentLayout
from toolang.execution.store import RunStore

from toolang.cli.common.top_view import (
    PERIODS,
    RECENTS,
    SORT_FIELDS,
    TopView,
    next_value,
    operation,
    runnable,
    spend,
)
from toolang.cli.toolang.main import app
from toolang.execution.schemas import LiveSnapshot, LiveTreeSnapshot, RunNode, StepNode
from toolang.execution.types import (
    CallCounters,
    Occurrence,
    OccurrencePosition,
    TokenUsage,
)
from toolang.stats.schemas import CompletedRoots, CompletionCounts, RecentUsage
from toolang.stats.types import Metrics, timestamp
from toolang.up.schemas import SourceActivity, TopSnapshot
from toolang.up.types import RuntimeInstance, RuntimeInstanceSnapshot

NOW = 1792326877
AT = timestamp(NOW)
START = timestamp(NOW - 100)
ROOT = "run_7p2m9k4w"


def step(identity="run_7p2m9k4w.0", parent=ROOT, **values):
    return replace(
        StepNode(
            identity,
            parent,
            "tool",
            "tool",
            "running",
            START,
            START,
            name="shell__exec",
            plugin="shell",
            invoked=True,
        ),
        **values,
    )


def frame(nodes=(), *, metrics=Metrics(), completed=(), counts=None, stale=False):
    instance = RuntimeInstance("alice", "resident", None, "source-a", "running", ())
    source = SourceActivity(
        identity=instance.identity,
        label=instance.label,
        placement=instance.placement,
        live=LiveSnapshot(
            "epoch-a",
            START if stale else AT,
            (LiveTreeSnapshot(ROOT, "chat_top", tuple(nodes)),) if nodes else (),
        ),
        live_stale=stale,
        usage=RecentUsage(
            observed_at=AT, start=timestamp(NOW - 3600), end=AT, metrics=metrics
        ),
        completed=CompletedRoots(
            observed_at=AT,
            start=timestamp(NOW - 300),
            end=AT,
            counts=counts or CompletionCounts(succeeded=len(completed)),
            roots=tuple(completed),
        ),
    )
    return TopSnapshot(
        observed_at=AT,
        period=3600,
        recent=300,
        inventory=RuntimeInstanceSnapshot((instance,)),
        sources=(source,),
    )


def root(**values):
    return replace(
        RunNode(
            ROOT,
            None,
            "flow:review",
            "running",
            START,
            started_at=START,
            calls=CallCounters(12, 18),
            usage=TokenUsage(42000, 3100, 26000),
            cost=0.38,
        ),
        **values,
    )


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        ("flow:review", "", "flow:review"),
        ("agic:_", "", "agic:"),
        ("flow:<entry>", "", "flow:"),
        ("flow:", "", "flow:"),
        ("pkg::review", "agic", "agic:review"),
        ("pkg::review", "flow", "flow:review"),
    ],
)
def test_runnable_labels(value, kind, expected):
    assert runnable(value, kind) == expected


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        (step(kind="model", name="openai/gpt-5"), "openai/gpt-5"),
        (
            step(kind="model", name="openai/gpt-5", invoked=False),
            "preparing openai/gpt-5",
        ),
        (step(name="shell__exec", plugin="other"), "shell.exec"),
        (step(name="unrecognized"), "unrecognized"),
        (step(name="runtime__chdir", plugin="runtime"), "runtime.chdir"),
        (step(name="runtime__compact", plugin="runtime"), "runtime.compact"),
        (step(name="runtime__run", plugin="other"), "runtime.run"),
        (step(name="runtime__map", plugin="runtime"), "runtime.map"),
        (
            step(
                name="runtime__run",
                plugin="runtime",
                target="agic:review",
                asynchronous=True,
            ),
            "async run agic:review",
        ),
        (
            step(name="runtime__await", plugin="runtime", target="worker"),
            "await worker",
        ),
        (
            step(
                name="runtime__spawn",
                plugin="runtime",
                target="flow:worker",
                invoked=False,
            ),
            "preparing spawn flow:worker",
        ),
        (
            step(
                kind="run",
                operation="run",
                target="agic:review",
                name="",
                asynchronous=True,
            ),
            "async run agic:review",
        ),
        (
            step(kind="map", operation="map", target="agic:review", name=""),
            "map agic:review",
        ),
    ],
)
def test_step_identity_and_invocation_labels(node, expected):
    assert operation(node) == expected


@pytest.mark.parametrize("name", ["run", "spawn", "await", "exec"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_shared_native_and_runtime_operations(name, asynchronous):
    native = step(
        kind=name,
        operation=name,
        name="",
        target="agic:worker",
        asynchronous=asynchronous,
    )
    tool = step(
        name=f"runtime__{name}",
        plugin="runtime",
        target="agic:worker",
        asynchronous=asynchronous,
    )
    assert operation(native) == operation(tool)
    assert native.kind == name and tool.kind == "tool"


def test_expansion_folding_parallel_boundaries_and_lifetime_identity():
    parent = root()
    serial = step(kind="run", operation="run", name="", target="agic:worker")
    child = RunNode(
        "run_child",
        serial.id,
        "agic:worker",
        "running",
        START,
        occurrence=Occurrence(item=OccurrencePosition(2, 8)),
    )
    model = step("run_child.0", child.id, kind="model", name="openai/gpt-5")
    snapshot = frame((parent, serial, child, model))
    view = TopView()
    rows = view.project(snapshot)
    assert [row.status for row in rows] == [
        "flow:review",
        "run agic:worker",
        "agic:worker · item 3/8",
        "openai/gpt-5",
    ]
    assert all(row.root == ROOT for row in rows)
    assert all(row.thread == "chat_top" for row in rows)
    assert "openai" not in rows[0].status
    view.selected = rows[2].key
    view.left()
    rows = view.project(snapshot)
    assert rows[2].status == "agic:worker · openai/gpt-5 · item 3/8"
    view.selected = rows[0].key
    view.left()
    rows = view.project(snapshot)
    assert len(rows) == 1 and rows[0].status == "flow:review · openai/gpt-5"
    assert rows[0].node is parent and rows[0].node.calls.model_calls == 12
    parallel = replace(serial, kind="par", operation="par", target="")
    rows = view.project(frame((parent, parallel, child, model)))
    assert rows[0].status == "flow:review · par · 1 active"
    second = replace(child, id="run_sibling")
    rows = view.project(frame((parent, parallel, child, model, second)))
    assert rows[0].status == "flow:review · par · 2 active"
    assert "openai" not in rows[0].status


def test_retained_terminal_step_does_not_claim_its_child_activity_or_target_ownership():
    launch = step(
        name="runtime__spawn",
        plugin="runtime",
        target="agic:external",
        status="succeeded",
    )
    child = RunNode("run_child", launch.id, "agic:owned", "running", START)
    leaf = step("run_child.0", child.id, kind="model", name="openai/gpt-5")
    snapshot = frame((root(), launch, child, leaf))
    view = TopView()
    view.project(snapshot)
    view.selected = view.rows[1].key
    view.left()
    rows = view.project(snapshot)
    assert rows[1].status == "succeeded · spawn agic:external · 1 active"
    assert len(rows) == 2


def test_parallel_children_keep_live_admission_order_instead_of_sorting_random_ids():
    parallel = step(kind="map", operation="map", target="agic:worker")
    first = RunNode(
        "run_zz",
        parallel.id,
        "agic:worker",
        "running",
        START,
        occurrence=Occurrence(item=OccurrencePosition(0, 2)),
    )
    second = replace(
        first, id="run_aa", occurrence=Occurrence(item=OccurrencePosition(1, 2))
    )
    rows = TopView().project(frame((root(), parallel, first, second)))
    assert [row.node.id for row in rows[2:]] == ["run_zz", "run_aa"]


def test_selection_moves_to_ancestor_completion_keeps_root_and_epoch_resets_folds():
    snapshot = frame((root(), step()))
    view = TopView()
    view.project(snapshot)
    view.selected = view.rows[1].key
    root_key = view.rows[0].key
    view.folded.add(root_key)
    view.project(snapshot)
    assert view.selected == root_key
    view.list_mode = True
    view.project(snapshot)
    view.list_mode = False
    assert root_key in view.folded
    finished = replace(root(), status="failed", finished_at=AT)
    rows = view.project(frame(completed=(finished,), counts=CompletionCounts(failed=1)))
    assert rows[0].status == "failed" and view.selected == root_key
    newer = frame((root(), step()))
    newer.sources[0].live = replace(newer.sources[0].live, epoch="epoch-b")
    assert len(view.project(newer)) == 2 and not view.folded


def test_header_fixed_fields_right_edge_footer_and_normal_width_clipping():
    snapshot = frame(
        (root(), step(name="shell__" + "x" * 200)),
        metrics=Metrics(512000, 54000, 360000, 2460000, 76, 188),
    )
    view = TopView()
    header = view.header(snapshot, 140)
    assert header[0].cell_len == 140
    assert header[2].plain.rstrip().endswith("right now") and header[
        3
    ].plain.rstrip().endswith("last 1h")
    assert header[4].plain.rstrip().endswith("last 5m")
    assert all(line.cell_len == 140 for line in header if line.plain)
    assert (
        "76 model calls, 188 tool calls, 512k input, 360k cached, 54k output, $2.4600 spend"
        in header[3].plain
    )
    text = view.render(snapshot, width=140).plain
    assert "F7 Runs(15m)" in text and "F8 Usage(4h)" in text
    assert "Enter" not in text and "started" not in text and "REF" not in text
    assert text.count(ROOT) == 2
    assert (
        "MODEL" in text
        and "TOOL" in text
        and "INPUT" in text
        and "CACHED" in text
        and "OUTPUT" in text
    )
    assert all(cell_len(line) <= 140 for line in text.splitlines())
    assert "…" in text
    empty = view.render(frame(), width=140).plain
    assert (
        "0 model calls, 0 tool calls, 0 input, 0 cached, 0 output, $0.0000 spend"
        in empty
    )
    assert "runs: 0 active, 0 succeeded, 0 failed, 0 canceled" in empty
    assert next_value(RECENTS, 900) == 60 and next_value(PERIODS, 28800) == 3600


def test_unknown_coverage_stale_duration_and_details_placeholder():
    snapshot = frame((root(started_at=timestamp(NOW - 500)), step()), stale=True)
    snapshot.sources[0].usage = None
    snapshot.sources[0].completed = None
    view = TopView(details=True)
    text = view.render(snapshot, width=140, height=25).plain
    assert "- active" in text and "- succeeded" in text and "- model calls" in text
    assert "updating" not in text and "live unavailable" not in text
    assert "6m40s" in text  # Frozen at the source's last successful observation.
    assert "Details are not defined yet" in text
    assert "F3 Close" in text
    assert len(text.splitlines()) == 25


@pytest.mark.parametrize("cached_live", [False, True])
def test_stopped_resident_has_zero_active_roots_without_live_warnings(cached_live):
    done = root(id="run_done", status="succeeded", finished_at=AT)
    snapshot = frame((root(), step()), completed=(done,), stale=True)
    snapshot.inventory = RuntimeInstanceSnapshot(
        tuple(
            replace(instance, state="not running", drivers=None)
            for instance in snapshot.inventory.instances
        )
    )
    if not cached_live:
        snapshot.sources[0].live = None
    assert snapshot.summary.active == 0
    assert snapshot.summary.resident == 1
    assert snapshot.summary.completed is not None
    assert snapshot.summary.completed.total == 1
    view = TopView()
    text = view.render(snapshot, width=160, height=25).plain
    assert "runs: 0 active, 1 succeeded, 0 failed, 0 canceled" in text
    assert "partial" not in text and "live unavailable" not in text
    assert "live stale" not in text
    assert [row.root for row in view.rows] == [done.id]


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_unavailable_live_remains_unknown_for_potentially_running_instances(state):
    snapshot = frame((), stale=True)
    snapshot.inventory = RuntimeInstanceSnapshot(
        tuple(
            replace(instance, state=state) for instance in snapshot.inventory.instances
        )
    )
    snapshot.sources[0].live = None
    snapshot.sources[0].live_error = "connection reset"
    assert snapshot.summary.active is None
    text = TopView().render(snapshot, width=140, height=25).plain
    assert "- active" in text and "partial" not in text
    assert "live unavailable" in text


@pytest.mark.parametrize("present", [False, True])
def test_roaming_facts_contribute_only_while_in_inventory(
    present,
):
    completed = tuple(
        root(id=f"run_closed_{index}", status="succeeded", finished_at=AT)
        for index in range(2)
    )
    snapshot = frame(
        (root(), step()),
        completed=completed,
        stale=True,
        metrics=Metrics(model_calls=5),
    )
    snapshot.sources[0].live_error = "connection reset"
    snapshot.sources[0].placement = "roaming"
    snapshot.sources[0].label = "aid@r70200d1f#instance"
    snapshot.inventory = RuntimeInstanceSnapshot(
        tuple(
            replace(instance, placement="roaming", name="aid")
            for instance in snapshot.inventory.instances
        )
        if present
        else ()
    )
    summary = snapshot.summary
    assert summary.active == (None if present else 0)
    assert summary.completed is not None and summary.completed.total == (
        2 if present else 0
    )
    assert summary.usage is not None and summary.usage.model_calls == (
        5 if present else 0
    )
    view = TopView()
    text = view.render(snapshot, width=160, height=30).plain
    assert ("live unavailable" in text.splitlines()[0]) == present
    assert "partial" not in text
    assert any(row.root == ROOT for row in view.rows) == present
    assert {row.root for row in view.rows if row.root_finished} == {
        node.id for node in completed if present
    }


@pytest.mark.parametrize("error", [None, "source unavailable"])
def test_out_of_scope_errors_do_not_affect_summaries_or_notices(error):
    snapshot = frame(())
    retained = snapshot.sources[0].model_copy(
        update={
            "identity": "departed",
            "label": "aid@r70200d1f#instance",
            "placement": "roaming",
            "live_stale": True,
            "completed": None,
            "activity_error": error,
        }
    )
    snapshot.sources = (*snapshot.sources, retained)
    assert snapshot.summary.active == 0 and snapshot.summary.completed is not None
    assert snapshot.summary.completed.total == 0
    text = TopView().render(snapshot, width=140, height=25).plain
    assert "runs unavailable" not in text.splitlines()[0]
    assert "updating" not in text and "partial" not in text
    assert "live stale" not in text and "live unavailable" not in text


def test_summary_emphasizes_only_nonzero_values_and_dims_captions():
    console = Console()
    header = TopView().header(
        frame(metrics=Metrics(512000, 54000, 360000, 2460000, 76, 188)), 140
    )
    assert "agents: 1 resident, 0 visiting, 0 roaming" in header[2].plain
    for line, caption in zip(header[2:5], ("right now", "last 1h", "last 5m")):
        assert line.plain.startswith("  ") and line.plain.endswith("  ")
        caption_at = line.plain.rindex(caption)
        for index in range(caption_at, caption_at + len(caption)):
            style = line.get_style_at_offset(console, index)
            assert style.dim and style.color is None and style.bgcolor is None
        for index in range(len(line) - 17):
            style = line.get_style_at_offset(console, index)
            assert not style.bold and not style.dim and style.bgcolor is None
    for token in ("76", "188", "512k", "$2.4600"):
        style = header[3].get_style_at_offset(console, header[3].plain.index(token))
        assert style.color is not None and style.color.name == "cyan"
    for line, token in (
        (header[2], "resident"),
        (header[2], "0 visiting"),
        (header[4], "0 active"),
    ):
        assert line.get_style_at_offset(console, line.plain.index(token)).color is None
    empty = TopView().header(frame(), 140)
    assert (
        empty[3].get_style_at_offset(console, empty[3].plain.index("$0.0000")).color
        is None
    )


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
def test_whole_rows_follow_root_intensity_and_selection_keeps_finished_dim(status):
    launch = step(status="succeeded")
    child = RunNode("run_child", launch.id, "agic:worker", "running", START)
    completed = root(id="run_finished", status=status, finished_at=AT)
    snapshot = frame((root(), launch, child), completed=(completed,))
    view = TopView()
    view.project(snapshot)
    view.selected = view.rows[-1].key
    lines = view.render(snapshot, width=140, height=25).split("\n")
    console = Console()
    active_rows = [line for line in lines if ROOT in line.plain]
    assert len(active_rows) == 3
    assert "succeeded" in active_rows[1].plain
    for line in active_rows:
        assert line.cell_len == 140
        for index in range(len(line)):
            style = line.get_style_at_offset(console, index)
            assert style.dim is False and style.bold is False
            assert style.color is None and style.bgcolor is None
    finished = next(line for line in lines if completed.id in line.plain)
    assert finished.cell_len == 140
    for index in range(len(finished)):
        style = finished.get_style_at_offset(console, index)
        assert style.dim is True and style.bold is False
        assert style.color is None and style.reverse is not True
        assert style.bgcolor is not None
        assert style.bgcolor.get_truecolor() == (0, 95, 95)


@pytest.mark.parametrize("width", [140, 200])
def test_active_root_marker_stays_lightweight_when_expanded_folded_or_listed(width):
    completed = root(id="run_finished", status="succeeded", finished_at=AT)
    snapshot = frame((root(), step()), completed=(completed,))
    view = TopView()

    def statuses():
        lines = view.render(snapshot, width=width, height=25).plain.splitlines()
        start = next(line.index("STATUS") for line in lines if "AGENT THREAD" in line)
        active = next(line for line in lines if ROOT in line)
        finished = next(line for line in lines if completed.id in line)
        assert active.index("flow:review") == start
        assert active[start - 2 : start] == "+ "
        assert finished.index("succeeded") == start
        return [
            line[start - 2 :].rstrip()
            for line in lines
            if ROOT in line or completed.id in line
        ]

    assert statuses() == ["+ flow:review", "  └─ shell.exec", "  succeeded"]
    view.left()
    assert statuses() == ["+ flow:review · shell.exec", "  succeeded"]
    view.list_mode = True
    assert statuses() == ["+ flow:review · shell.exec", "  succeeded"]


def test_htop_header_selection_and_bottom_bars_cover_the_terminal_width():
    snapshot = frame((root(), step()), stale=True)
    view = TopView()
    lines = view.render(snapshot, width=160, height=35).split("\n")
    console = Console()
    assert len(lines) == 35 and all(line.cell_len == 160 for line in lines)
    headings = next(line for line in lines if "AGENT THREAD" in line.plain)
    selected = next(line for line in lines if "+ flow:review" in line.plain)
    for index in range(160):
        heading = headings.get_style_at_offset(console, index)
        assert heading.color is not None and heading.color.name == "black"
        assert heading.bgcolor is not None and heading.bgcolor.name == "green"
        assert heading.bold is False and heading.dim is False
        style = selected.get_style_at_offset(console, index)
        assert style.bgcolor is not None and style.bgcolor.get_truecolor() == (
            0,
            95,
            95,
        )
        assert style.dim is False
        assert lines[-2].get_style_at_offset(console, index).bgcolor is None
    footer = lines[-1]
    for label in ("F1", "F3", "F5", "F7", "F8", "F10"):
        key_style = footer.get_style_at_offset(console, footer.plain.index(label))
        assert key_style.color is not None and key_style.color.name == "white"
        assert key_style.bgcolor is not None and key_style.bgcolor.name == "black"
    for index in (footer.plain.index("Help"), len(footer) - 1):
        action = footer.get_style_at_offset(console, index)
        assert action.color is not None and action.color.name == "black"
        assert action.bgcolor is not None and action.bgcolor.name == "cyan"


def test_unicode_and_captured_line_breaks_stay_in_one_table_row():
    node = step(name="unrecognized\n工具\t" + "名" * 100)
    snapshot = frame((root(), node))
    text = TopView().render(snapshot, width=140, height=20).plain
    assert len(text.splitlines()) == 20
    assert all(cell_len(line) <= 140 for line in text.splitlines())
    assert "unrecognized 工具 " in text and "…" in text
    assert node.name.startswith("unrecognized\n")


@pytest.mark.parametrize("amount", [0.0001, 999.99, 1234.56, 999999.99])
def test_approximate_or_large_spend_keeps_numeric_and_status_columns_aligned(amount):
    snapshot = frame((root(cost=amount, cost_approximate=True), step()))
    lines = TopView().render(snapshot, width=140).plain.splitlines()
    parent = next(line for line in lines if "flow:review" in line)
    child = next(line for line in lines if "└─" in line)
    assert parent.index("flow:review") == child.index("└─")
    assert f"${amount:.4f}" in parent and "~" not in parent
    assert snapshot.sources[0].live.trees[0].nodes[0].cost_approximate


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(None, "-"), (0, "$0.0000"), (0.00384, "$0.0038"), (2.46, "$2.4600")],
)
def test_spend_uses_four_decimal_places(amount, expected):
    assert spend(amount) == expected


@pytest.mark.parametrize(
    "label", ["gus#runtime", "aid@r70200d1f#runtime", "worker@v12345678#runtime"]
)
def test_table_and_coverage_use_instance_name_without_runtime_id(label):
    snapshot = frame((root(),), stale=True, counts=CompletionCounts(succeeded=2))
    source = snapshot.sources[0]
    source.label = label
    view = TopView()
    view.project(snapshot)
    selected = view.selected
    text = view.render(snapshot, width=160).plain
    name = label.partition("#")[0]
    assert next(line for line in text.splitlines() if ROOT in line).startswith(
        name + " "
    )
    assert "live stale" not in text
    assert "completed loaded" not in text
    assert "#runtime" not in text
    assert source.label == label and view.selected == selected


def test_large_call_counts_keep_numeric_and_status_columns_aligned():
    snapshot = frame((root(calls=CallCounters(123_400_000, 234_500_000)), step()))
    lines = TopView().render(snapshot, width=140).plain.splitlines()
    parent = next(line for line in lines if "flow:review" in line)
    child = next(line for line in lines if "└─" in line)
    assert parent.index("flow:review") == child.index("└─")


@pytest.mark.parametrize("width", [40, 80, 100, 140, 200])
@pytest.mark.parametrize("list_mode", [False, True])
@pytest.mark.parametrize("height", [None, 25])
def test_thread_column_keeps_root_ownership_and_cell_alignment(
    width, list_mode, height
):
    thread = "chat_工具e\u0301"
    done = root(
        id="run_done", status="succeeded", finished_at=AT, thread_id="chat_done"
    )
    snapshot = frame((root(thread_id="ignored"), step()), completed=(done,))
    source = snapshot.sources[0]
    source.live = replace(
        source.live,
        trees=(
            replace(source.live.trees[0], thread_id=thread),
            LiveTreeSnapshot("run_other", "chat_other", (root(id="run_other"),)),
        ),
    )
    view = TopView(list_mode=list_mode)
    lines = view.render(snapshot, width=width, height=height).plain.splitlines()
    heading = next(line for line in lines if line.startswith("AGENT "))
    assert heading.split()[:3] == ["AGENT", "THREAD", "RUN"]
    expected = {ROOT: thread, "run_other": "chat_other", "run_done": "chat_done"}
    rows = [line for line in lines if line.startswith("alice ")]
    assert len(rows) == (3 if list_mode else 4)
    for line, row in zip(rows, view.rows, strict=True):
        assert row.thread == expected[row.root]
        assert line.split()[:3] == ["alice", row.thread, row.root]
        assert cell_len(line[: line.index(row.thread)]) == heading.index("THREAD")
        assert cell_len(line[: line.index(row.root)]) == heading.index("RUN")
    assert all(cell_len(line) == width for line in lines)


def test_completed_thread_missing_from_older_snapshot_is_unknown():
    snapshot = frame(completed=(root(status="succeeded", finished_at=AT),))
    data = json.loads(snapshot.model_dump_json())
    data["sources"][0]["completed"]["roots"][0].pop("thread_id")
    restored = TopSnapshot.model_validate(data)
    view = TopView()
    lines = view.render(restored, width=140).plain.splitlines()
    row = next(line for line in lines if ROOT in line)
    assert row.split()[:3] == ["alice", "-", ROOT]
    assert view.rows[0].thread is None


def sorting_frame():
    low = root(
        id="run_low",
        calls=CallCounters(1, 2),
        usage=TokenUsage(10, 20, 30),
        cost=0.001,
        started_at=timestamp(NOW - 10),
    )
    high = root(
        id="run_high",
        calls=CallCounters(8, 9),
        usage=TokenUsage(100, 200, 300),
        cost=0.009,
        started_at=timestamp(NOW - 100),
    )
    unknown = root(
        id="run_unknown",
        calls=CallCounters(),
        usage=None,
        cost=None,
        started_at=None,
    )
    finished = tuple(
        replace(node, id=node.id + "_done", status="succeeded", finished_at=AT)
        for node in (low, high, unknown)
    )
    snapshot = frame(completed=finished)
    source = snapshot.sources[0]
    source.live = replace(
        source.live,
        trees=tuple(
            LiveTreeSnapshot(node.id, "chat_top", (node, step(node.id + ".0", node.id)))
            for node in (low, high, unknown)
        ),
    )
    return snapshot


@pytest.mark.parametrize("sort_field", SORT_FIELDS)
@pytest.mark.parametrize("descending", [False, True])
def test_numeric_sorting_keeps_partitions_subtrees_unknowns_and_selection(
    sort_field,
    descending,
):
    snapshot = sorting_frame()
    view = TopView()
    view.project(snapshot)
    selected = view.selected = view.rows[1].key
    view.sort_field = sort_field
    view.sort_descending = descending
    rows = view.project(snapshot)
    order = [row.root for row in rows if row.node.id == row.root]
    expected = (
        ["run_high", "run_low", "run_unknown"]
        if descending
        else ["run_low", "run_high", "run_unknown"]
    )
    if not descending and sort_field in {"MODEL", "TOOL"}:
        expected = ["run_unknown", "run_low", "run_high"]
    assert order == expected + [name + "_done" for name in expected]
    assert view.selected == selected
    for index, row in enumerate(rows):
        if row.parent is not None:
            assert rows[index - 1].root == row.root
    text = view.render(snapshot, width=160).plain
    assert ("▿" if descending else "▵") + sort_field in text


def test_default_order_tracks_tree_changes_without_clock_or_fold_reordering():
    snapshot = sorting_frame()
    source = snapshot.sources[0]
    source.live = replace(source.live, trees=source.live.trees[:2])
    view = TopView()
    view.project(snapshot)
    assert view.rows[0].root == "run_low"  # Most recent lifecycle timestamp.
    selected = view.selected
    snapshot.observed_at = timestamp(NOW + 1)
    view.project(snapshot)
    assert view.rows[0].root == "run_low"
    high = source.live.trees[1]
    source.live = replace(
        source.live,
        trees=(
            source.live.trees[0],
            replace(
                high,
                nodes=(high.nodes[0], replace(high.nodes[1], invoked=False)),
            ),
        ),
    )
    view.project(snapshot)
    assert view.rows[0].root == "run_high" and view.selected == selected
    snapshot.observed_at = timestamp(NOW + 10)
    view.left()
    view.list_mode = True
    view.project(snapshot)
    assert view.rows[0].root == "run_high" and view.selected == selected
    assert len(view._activity) == 2


def test_sort_picker_and_mouse_hit_targets_follow_rendered_rows_and_columns():
    snapshot = sorting_frame()
    view = TopView()
    lines = view.render(snapshot, width=160, height=20).plain.splitlines()
    header_y = next(
        i for i, line in enumerate(lines) if "AGENT" in line and "STATUS" in line
    )
    view.click(lines[header_y].index("THREAD"), header_y)
    assert view.sort_field is None
    view.click(lines[header_y].index("MODEL"), header_y)
    assert view.sort_field == "MODEL" and view.sort_descending
    lines = view.render(snapshot, width=160, height=20).plain.splitlines()
    view.click(lines[header_y].index("MODEL"), header_y)
    assert not view.sort_descending
    view.sort()
    view.move(5)
    view.left()
    assert view.sort_menu and view.sort_choice == 6 and not view.sort_choice_descending
    view.sort()
    assert view.sort_field == "SPEND" and not view.sort_descending
    view.move(100)
    lines = view.render(snapshot, width=140, height=16).plain.splitlines()
    assert view.offset > 0
    row_y = next(i for i, line in enumerate(lines) if "run_" in line)
    key = view._row_hits[row_y]
    view.click(130, row_y)
    assert view.selected == key
    view.sort()
    view.move(-100)
    view.sort()
    assert view.sort_field is None


@pytest.mark.parametrize("sort_field", SORT_FIELDS)
@pytest.mark.parametrize("descending", [False, True])
def test_sort_prefix_keeps_headings_values_and_mouse_targets_fixed(
    sort_field, descending
):
    snapshot = frame((root(),))
    view = TopView()
    baseline = view.render(snapshot, width=200).plain.splitlines()
    y = next(i for i, line in enumerate(baseline) if line.startswith("AGENT "))
    names = ("AGENT", "THREAD", "RUN", *SORT_FIELDS, "STATUS")
    positions = {name: baseline[y].index(name) for name in names}
    row = next(line for line in baseline if ROOT in line)
    view.sort_field, view.sort_descending = sort_field, descending
    lines = view.render(snapshot, width=200).plain.splitlines()
    assert {name: lines[y].index(name) for name in names} == positions
    assert next(line for line in lines if ROOT in line) == row
    assert ("▿" if descending else "▵") + sort_field in lines[y]
    view.click(positions[sort_field] - 1, y)
    assert view.sort_field == sort_field and view.sort_descending != descending


@pytest.mark.parametrize("transition", ["resize", "close_sort", "remove_rows"])
def test_viewport_reveals_earlier_rows_when_the_whole_table_fits(transition):
    snapshot = sorting_frame()
    view = TopView(sort_menu=transition == "close_sort")
    view.project(snapshot)
    view.selected = view.rows[-1].key
    initial_height = 19 if transition == "close_sort" else 16
    view.render(snapshot, width=140, height=initial_height)
    assert view.offset > 0
    selected = view.selected
    if transition == "close_sort":
        view.sort_menu = False
    elif transition == "remove_rows":
        source = snapshot.sources[0]
        source.live = replace(source.live, trees=source.live.trees[:1])
    height = 25 if transition == "resize" else initial_height
    text = view.render(snapshot, width=140, height=height).plain
    assert view.page_size >= len(view.rows)
    assert view.offset == 0
    assert view.selected == selected
    assert all(row.root in text for row in view.rows)


@pytest.mark.parametrize("width", [40, 80, 140])
@pytest.mark.parametrize("height", [1, 2, 3, 8, 12])
@pytest.mark.parametrize("panel", [None, "help", "details", "sort_menu"])
def test_short_viewport_preserves_footer_selection_and_visible_mouse_targets(
    width, height, panel
):
    snapshot = sorting_frame()
    view = TopView()
    view.project(snapshot)
    view.selected = view.rows[-1].key
    selected = view.selected
    if panel:
        setattr(view, panel, True)
    lines = view.render(snapshot, width=width, height=height).plain.splitlines()
    assert len(lines) == height
    assert "F10" in lines[-1]
    assert all(cell_len(line) == width for line in lines)
    assert all(0 <= y < height - 1 for y in view._row_hits)
    assert all(
        0 <= y < height - 1 and lines[y].startswith("AGENT")
        for y, _, _, _ in view._sort_hits
    )
    assert view.selected == selected
    view.render(snapshot, width=140, height=35)
    assert view.selected == selected and selected in view._row_hits.values()


@pytest.mark.parametrize("choice, delta", [(0, -1), (len(SORT_FIELDS), 1)])
def test_sort_picker_boundary_keeps_the_chosen_direction(choice, delta):
    view = TopView(sort_menu=True, sort_choice=choice, sort_choice_descending=False)
    view.move(delta)
    assert view.sort_choice == choice and not view.sort_choice_descending


def test_real_mouse_events_sort_unicode_headings_and_select_finished_rows(
    tmp_path, monkeypatch
):
    import asyncio

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from toolang.cli.common.top import top_application
    from toolang.up.top import TopObserver

    class Output(DummyOutput):
        def get_size(self):
            return Size(rows=25, columns=160)

    snapshot = sorting_frame()
    snapshot.sources[0].label = "助手@r70200d1f#runtime"
    observer = TopObserver(tmp_path, agent=None, period=3600, recent=300, refresh=1)
    monkeypatch.setattr(observer, "tick", lambda: snapshot)
    view = TopView()
    phase = 0

    async def scenario():
        nonlocal phase
        with create_pipe_input() as pipe:
            app = top_application(observer, view, input=pipe, output=Output())

            def click(x, y):
                pipe.send_text(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m")

            def rendered(_):
                nonlocal phase
                if phase == 0:
                    y, x, _, _ = next(
                        hit for hit in view._sort_hits if hit[3] == "MODEL"
                    )
                    phase = 1
                    click(x + 1, y)
                elif phase == 1 and view.sort_field == "MODEL":
                    phase = 2
                    y = next(
                        y
                        for y, key in view._row_hits.items()
                        if key[2] == "run_low_done"
                    )
                    click(150, y)
                elif (
                    phase == 2
                    and view.selected is not None
                    and view.selected[2] == "run_low_done"
                ):
                    phase = 3
                    pipe.send_text("q")

            app.after_render += rendered
            await asyncio.wait_for(app.run_async(), timeout=5)

    try:
        asyncio.run(scenario())
        assert phase == 3 and view.sort_descending
    finally:
        observer.close()


def test_non_tty_json_empty_scope_and_refresh_validation(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--root", str(tmp_path), "top", "--json", "--period", "8h", "--recent", "15m"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["sources"] == [] and data["period"] == 28800 and data["recent"] == 900
    assert not (tmp_path / "stats.db").exists()
    for refresh in ("0", "-1", "nan", "inf"):
        result = runner.invoke(
            app, ["--root", str(tmp_path), "top", "--refresh", refresh]
        )
        assert result.exit_code == 2


@pytest.mark.parametrize("output", [None, "--once", "--json"])
def test_resident_prefix_recovers_offline_completion_without_starting_runtime(
    tmp_path, capsys, output
):
    import toolang.cli.toolang.main as cli

    for name in ("alice", "bob"):
        layout = AgentLayout.resident(tmp_path, name)
        layout.home.mkdir(parents=True)
        (layout.home / "agent.too").write_text("agic:\n  Reply directly.\n")
        with closing(RunStore(layout.run_store)) as store:
            project_run_start(
                store,
                run_id=f"run_{name}",
                thread_id="chat_top",
                input=Message.user("fixture"),
                started_at=timestamp(NOW - 20),
            )
            store.finish_run(run_id=f"run_{name}", finished_at=timestamp(NOW))
    # Use the dispatch entry point, so this exercises target-first normalization.
    from unittest.mock import patch

    with patch("toolang.up.top.time.time", return_value=NOW):
        args = ["--root", str(tmp_path), "alice", "top"]
        assert cli.main(args + ([output] if output else [])) == 0
    text = capsys.readouterr().out
    if output == "--json":
        data = json.loads(text)
        assert data["agent"] == "alice" and data["summary"]["resident"] == 1
        assert data["summary"]["completed"]["total"] == 1
        assert len(data["sources"]) == 1
        assert data["sources"][0]["label"] == "alice"
    else:
        assert "run_alice" in text and "run_bob" not in text


@pytest.mark.parametrize("width", [40, 80, 100, 140])
@pytest.mark.parametrize("failure", [False, True])
def test_padded_header_and_single_footer_fit_resizes_with_real_failures(width, failure):
    snapshot = frame((root(), step(name="工具" * 30)))
    source = snapshot.sources[0]
    if failure:
        snapshot.inventory = replace(
            snapshot.inventory, diagnostics=("private/path: cannot read",)
        )
        source.activity_error = source.usage_error = (
            "private/path: malformed record\ntraceback"
        )
    view = TopView(scope="too gus top")
    lines = view.render(snapshot, width=width, height=35).split("\n")
    assert len(lines) == 35 and all(line.cell_len == width for line in lines)
    assert lines[0].plain[-8:].count(":") == 2
    assert lines[0].plain.startswith("too gus top")
    assert ("agents" in lines[0].plain) == failure
    if failure and width == 40:
        assert "+2" in lines[0].plain
    assert "private/path" not in "\n".join(line.plain for line in lines)
    assert lines[-1].plain.startswith("F1 Help") and "F10 Quit" in lines[-1].plain
    assert all("unavailable" not in line.plain for line in lines[1:])
    header = view.header(snapshot, width)
    for line in header[2:-1]:
        assert line.plain.startswith("  ") and line.plain.endswith("  ")
        assert line.cell_len == width
    assert view.page_size == 35 - len(header) - 2


@pytest.mark.parametrize(
    "state, observed, diagnostic, running",
    [
        ("running", None, "effective launch drivers are missing or invalid", 1),
        ("unknown", "preparing", "observed preparing", 0),
        ("unknown", "starting", "observed starting", 0),
        ("not running", "failed", None, 0),
        ("unknown", "running", "cannot inspect runtime", None),
    ],
)
def test_agent_counts_keep_membership_and_readable_metrics_independent(
    state, observed, diagnostic, running
):
    snapshot = frame(metrics=Metrics(model_calls=9))
    snapshot.inventory = RuntimeInstanceSnapshot(
        tuple(
            replace(item, state=state, observed_state=observed, diagnostic=diagnostic)
            for item in snapshot.inventory.instances
        )
    )
    summary = snapshot.summary
    assert summary.running == running and summary.total == summary.resident == 1
    assert summary.completed is not None and summary.completed.total == 0
    assert summary.usage is not None and summary.usage.model_calls == 9
    text = TopView().render(snapshot, width=140).plain
    assert "agents unavailable" not in text
    assert "partial" not in text and "updating" not in text
    assert snapshot.model_dump()["summary"]["running"] == running


def test_running_counts_agents_independently_of_roots_and_retained_history():
    snapshot = frame((root(),))
    snapshot.sources[0].live = replace(
        snapshot.sources[0].live,
        trees=tuple(
            LiveTreeSnapshot(f"run_{index}", "chat_top", (root(id=f"run_{index}"),))
            for index in range(3)
        ),
    )
    instance = snapshot.inventory.instances[0]
    snapshot.inventory = RuntimeInstanceSnapshot(
        (
            instance,
            replace(instance, name="bob", identity="source-b", state="not running"),
            replace(instance, name="worker", identity="source-c", placement="roaming"),
        )
    )
    snapshot.sources = (
        *snapshot.sources,
        frame().sources[0].model_copy(update={"identity": "source-b"}),
        frame()
        .sources[0]
        .model_copy(update={"identity": "source-c", "placement": "roaming"}),
        frame()
        .sources[0]
        .model_copy(update={"identity": "departed", "placement": "visiting"}),
    )
    summary = snapshot.summary
    assert (
        summary.running,
        summary.total,
        summary.resident,
        summary.visiting,
        summary.roaming,
    ) == (2, 3, 2, 0, 1)
    assert summary.active == 3


@pytest.mark.parametrize(
    "case", ["pending", "window", "old_usage", "provider", "paging", "page_failure"]
)
def test_routine_missing_or_delayed_observations_do_not_create_notices(case):
    snapshot = frame()
    source = snapshot.sources[0]
    if case in ("pending", "window"):
        source.live = None
        source.live_stale = True
        source.completed = source.usage = None
    elif case == "old_usage":
        source.usage.observed_at = timestamp(NOW - 60)
    elif case == "provider":
        source.usage.metrics = Metrics(
            model_calls=1,
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            spend_micros=None,
        )
    elif case == "paging":
        source.completed.counts = CompletionCounts(succeeded=101)
    else:
        source.page_error = "database locked"
    view = TopView()
    assert view.notices(snapshot) == []
    text = view.render(snapshot, width=140, height=25).plain
    for message in (
        "partial",
        "updating",
        "unavailable",
        "stats as of",
        "completed loaded",
        "database locked",
    ):
        assert message not in text


def test_initial_discovery_failure_is_not_rendered_as_empty_inventory():
    snapshot = frame().model_copy(
        update={
            "inventory_updating": True,
            "inventory": RuntimeInstanceSnapshot((), ("permission denied",)),
            "sources": (),
        }
    )
    assert snapshot.summary.total is None and snapshot.summary.running is None
    view = TopView()
    text = view.render(snapshot, width=140).plain
    assert (
        "agents: - resident, - visiting, - roaming" in text
        and "agents unavailable" in text.splitlines()[0]
    )
    assert "permission denied" not in text

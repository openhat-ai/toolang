"""Top presents exact owned identities independently of accounting and I/O."""

from contextlib import closing
from dataclasses import replace
import json

import pytest
from rich.cells import cell_len
from typer.testing import CliRunner
from tests.support.execution_fixtures import project_run_start
from toolang.base.types.message import Message
from toolang.common.layout import AgentLayout
from toolang.execution.store import RunStore

from toolang.cli.common.top_view import (
    PERIODS,
    RECENTS,
    TopView,
    next_value,
    operation,
    runnable,
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
    ("value", "expected"),
    [
        ("flow:review", "flow:review"),
        ("agic:_", "agic:"),
        ("flow:<entry>", "flow:"),
        ("flow:", "flow:"),
        ("pkg::agic:review", "agic:review"),
    ],
)
def test_runnable_labels(value, expected):
    assert runnable(value) == expected


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
    assert "openai" not in rows[0].status
    view.selected = rows[2].key
    view.left()
    rows = view.project(snapshot)
    assert rows[2].status == "agic:worker · openai/gpt-5 · item 3/8 [+]"
    view.selected = rows[0].key
    view.left()
    rows = view.project(snapshot)
    assert len(rows) == 1 and rows[0].status == "flow:review · openai/gpt-5 [+]"
    assert rows[0].node is parent and rows[0].node.calls.model_calls == 12
    parallel = replace(serial, kind="par", operation="par", target="")
    rows = view.project(frame((parent, parallel, child, model)))
    assert rows[0].status == "flow:review · par · 1 active [+]"
    second = replace(child, id="run_sibling")
    rows = view.project(frame((parent, parallel, child, model, second)))
    assert rows[0].status == "flow:review · par · 2 active [+]"
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
    assert rows[1].status == "succeeded · spawn agic:external · 1 active [+]"
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
    assert header[2].plain.endswith("agents") and header[3].plain.endswith(
        "runs last 5m"
    )
    assert header[4].plain.endswith("stats last 1h")
    assert all(line.cell_len == 140 for line in header if line.plain)
    assert (
        "76 model calls, 188 tool calls, 512k input, 360k cached, 54k output, $2.46 spend"
        in header[4].plain
    )
    text = view.render(snapshot, width=140).plain
    assert "F7 Runs(15m)" in text and "F8 Stats(4h)" in text
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
        "0 model calls, 0 tool calls, 0 input, 0 cached, 0 output, $0.00 spend" in empty
    )
    assert "0 active, 0 completed: 0 succeeded, 0 failed, 0 canceled" in empty
    assert next_value(RECENTS, 900) == 60 and next_value(PERIODS, 28800) == 3600


def test_unknown_coverage_stale_duration_and_details_placeholder():
    snapshot = frame((root(started_at=timestamp(NOW - 500)), step()), stale=True)
    snapshot.sources[0].usage = None
    snapshot.sources[0].completed = None
    view = TopView(details=True)
    text = view.render(snapshot, width=140, height=25).plain
    assert "- active" in text and "- completed" in text and "- model calls" in text
    assert "updating" in text and "stale" in text
    assert "6m40s" in text  # Frozen at the source's last successful observation.
    assert "Details are not defined yet" in text
    assert "F3 Close" in text
    assert len(text.splitlines()) == 25


def test_unicode_and_captured_line_breaks_stay_in_one_table_row():
    node = step(name="unrecognized\n工具\t" + "名" * 100)
    snapshot = frame((root(), node))
    text = TopView().render(snapshot, width=140, height=20).plain
    assert len(text.splitlines()) == 20
    assert all(cell_len(line) <= 140 for line in text.splitlines())
    assert "unrecognized 工具 " in text and "…" in text
    assert node.name.startswith("unrecognized\n")


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


def test_resident_prefix_recovers_offline_completion_without_starting_runtime(
    tmp_path, capsys
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
                origin="chat",
                input=Message.user("fixture"),
                started_at=timestamp(NOW - 20),
            )
            store.finish_run(run_id=f"run_{name}", finished_at=timestamp(NOW))
    # Use the dispatch entry point, so this exercises target-first normalization.
    from unittest.mock import patch

    with patch("toolang.up.top.time.time", return_value=NOW):
        assert cli.main(["--root", str(tmp_path), "alice", "top", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["agent"] == "alice" and data["summary"]["resident"] == 1
    assert data["summary"]["active"] is None
    assert data["summary"]["completed"]["total"] == 1
    assert len(data["sources"]) == 1
    assert data["sources"][0]["completed"]["roots"][0]["id"] == "run_alice"
    assert not list(tmp_path.rglob("stats.db"))
    assert not list(tmp_path.rglob("status.json"))

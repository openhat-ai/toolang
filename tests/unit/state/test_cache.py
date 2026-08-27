from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.lang.ast import Program, SettleStmt
from toolang.state.cache import (
    canonical_json,
    layer_revision_dir,
    load_agent_revisions,
    load_current_agent_revision,
    load_current_revision,
    load_home_layer,
    load_root_layer,
    persist_agent_revision,
    publish_layer_current,
    write_layer,
)
from toolang.state.source import scan_source
from toolang.state.state import StateModule, agent_state_revision


def _layout(root: Path) -> AgentLayout:
    return AgentLayout.resident(root, "alice")


def _write_root(layout: AgentLayout) -> str:
    return write_layer(
        layout=layout,
        scope="root",
        source=scan_source(layout.root, ()),
        resolutions=(),
        config={},
        caps=(),
        modules=(),
        files={},
    )


def _write_home(
    layout: AgentLayout,
    source_text: str = "agic default:\n  Ready.\n",
) -> str:
    layout.home.mkdir(parents=True, exist_ok=True)
    layout.program.write_text(source_text, encoding="utf-8")
    source = scan_source(layout.home, ("agent.too",))
    return write_layer(
        layout=layout,
        scope="home",
        source=source,
        resolutions=(),
        config={"models": {"default": "fast"}},
        caps=(),
        modules=(
            StateModule(
                name="agent",
                kind="agent",
                authored_path="agent.too",
                materialized_path="files/agent.too",
                digest=sha256(source_text.encode()).hexdigest(),
                program=Program.from_source(source_text),
            ),
        ),
        files={"agent.too": source_text.encode()},
    )


def test_layer_json_exact_bytes_are_the_revision(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    revision = _write_root(layout)
    revision_dir = layer_revision_dir(layout, "root", revision)
    encoded = (revision_dir / "layer.json").read_bytes()

    assert encoded == canonical_json(json.loads(encoded))
    assert sha256(encoded).hexdigest() == revision
    assert revision_dir.name == revision
    assert len(revision) == 64
    assert not encoded.endswith(b"\n")


def test_canonical_json_rejects_nonfinite_numbers() -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})


def test_agent_state_revision_round_trips_exact_layers(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    root_revision = _write_root(layout)
    home_revision = _write_home(layout)
    revision = persist_agent_revision(
        layout,
        root_revision=root_revision,
        home_revision=home_revision,
    )

    assert revision == agent_state_revision(root_revision, home_revision)
    assert load_current_agent_revision(layout) == revision
    assert load_agent_revisions(layout, revision) == (
        revision,
        root_revision,
        home_revision,
    )
    layers = layout.agent_state / "revs" / revision / "layers.json"
    assert sha256(layers.read_bytes()).hexdigest() == revision


def test_layer_publish_and_load_use_revision_strings(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    root_revision = _write_root(layout)
    home_revision = _write_home(layout, "agic hello:\n  Hello.\n")
    publish_layer_current(layout, "root", root_revision)
    publish_layer_current(layout, "home", home_revision)

    assert load_current_revision(layout, "root") == root_revision
    assert load_current_revision(layout, "home") == home_revision
    assert load_root_layer(layout).revision == root_revision
    home = load_home_layer(layout)
    assert home.revision == home_revision
    assert home.config == {"models": {"default": "fast"}}
    assert home.program.find_agic("hello") is not None


def test_home_layer_loads_program_without_reparsing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    source_text = "agic hello:\n  Hello.\n\nflow work:\n  settle hello\n"
    revision = _write_home(layout, source_text)
    publish_layer_current(layout, "home", revision)

    def fail_parse(_cls: type[Program], _source: str) -> Program:
        raise AssertionError("persisted program must not be reparsed")

    monkeypatch.setattr(Program, "from_source", classmethod(fail_parse))

    program = load_home_layer(layout).program
    assert program.find_agic("hello")
    flow = program.find_flow("work")
    assert flow is not None
    assert isinstance(flow.stmts[0], SettleStmt)


@pytest.mark.parametrize("damage", ["missing", "extra", "modified"])
def test_layer_manifest_rejects_file_set_and_content_damage(
    tmp_path: Path,
    damage: str,
) -> None:
    layout = _layout(tmp_path)
    revision = _write_home(layout)
    revision_dir = layer_revision_dir(layout, "home", revision)
    program = revision_dir / "files" / "agent.too"
    if damage == "missing":
        program.unlink()
    elif damage == "extra":
        (revision_dir / "files" / "extra.txt").write_text("extra", encoding="utf-8")
    else:
        program.write_text("changed", encoding="utf-8")

    with pytest.raises(ValueError):
        load_home_layer(layout, revision)


def test_layer_loader_rejects_noncanonical_json(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    revision = _write_root(layout)
    layer = layer_revision_dir(layout, "root", revision) / "layer.json"
    document = json.loads(layer.read_text(encoding="utf-8"))
    layer.write_text(json.dumps(document, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="not canonical JSON"):
        load_root_layer(layout, revision)


def test_layer_rejects_nonportable_file_path(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    with pytest.raises(ValueError, match="portable and relative"):
        write_layer(
            layout=layout,
            scope="root",
            source=scan_source(tmp_path, ()),
            resolutions=(),
            config={},
            caps=(),
            modules=(),
            files={"../escape": b"bad"},
        )


def test_agent_state_revision_rejects_noncanonical_revision() -> None:
    with pytest.raises(ValueError, match="root revision"):
        agent_state_revision("short", "0" * 64)

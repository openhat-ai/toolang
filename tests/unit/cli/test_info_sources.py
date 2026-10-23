"""Info prepares source inputs without depending on retained execution homes."""

import pytest

from toolang.cli.toolang.main import main
from toolang.common.layout import AgentLayout
from toolang.up import source as agent_sources
from toolang.up.allocation import allocate_roaming


@pytest.mark.parametrize("placement", ["roaming", "visiting"])
def test_info_prepares_a_fresh_source_without_a_retained_instance(
    tmp_path, monkeypatch, capsys, placement
):
    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    content = "flow main():\n  pass\n"
    if placement == "roaming":
        source = tmp_path / "alice.too"
        source.write_text(content)
        selector = str(source)
        layout = AgentLayout.roaming(source)
    else:
        selector = "https://example.com/alice.too"
        layout = agent_sources.visiting_layout(selector, toolang_root=tmp_path)
        monkeypatch.setattr(
            agent_sources, "fetch_agent_ref", lambda *args, **kwargs: content
        )
    assert not layout.home.exists()
    assert main(["info", selector]) == 0
    text = capsys.readouterr().out
    assert "Home:" in text and "not running" in text
    assert layout.agent_state.exists()
    assert not layout.run_store.exists()
    assert not tuple((tmp_path / ".runtime" / "active").glob("*/*.json"))


def test_info_preparation_failure_preserves_an_active_launch(
    tmp_path, monkeypatch, capsys
):
    from toolang.up.instance import read_instance
    from toolang.up.workload import ForegroundWorkload

    monkeypatch.setenv("TOOLANG_ROOT", str(tmp_path))
    source = tmp_path / "alice.too"
    source.write_text("flow main():\n  pass\n")
    layout = allocate_roaming(source, toolang_root=tmp_path)
    with ForegroundWorkload(
        layout, toolang_root=tmp_path, drivers=frozenset()
    ) as active:
        before = read_instance(layout.runtime_status)

        def fail(*args):
            raise ValueError("invalid project configuration")

        monkeypatch.setattr(agent_sources, "_sync_roaming_project", fail)
        assert main(["info", str(source)]) == 1
        text = capsys.readouterr().out
        assert (
            "Home:" in text
            and "running" in text
            and "invalid project configuration" in text
        )
        assert read_instance(layout.runtime_status) == before
        assert active.path.is_file()

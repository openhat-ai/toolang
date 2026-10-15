"""Source acquisition stays separate from kind/name Cap selection."""

from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import state as caps
from toolang.state.collections import cap_collection
from toolang.state.prepare import prepare_agent_state
from toolang.state.schemas import CapInfo
from toolang.up.source import parse_agent_target


@pytest.mark.parametrize(
    "ref",
    [
        "owner/name",
        "owner/repo/name",
        "example.com/name",
        "oh:owner/name",
        "openhat:owner/name",
        "other:owner/name",
    ],
)
def test_unsupported_sources_do_not_probe_or_fetch(monkeypatch, ref):
    def unexpected(*args, **kwargs):
        pytest.fail("unsupported source attempted network access")

    monkeypatch.setattr(caps, "urlopen", unexpected)
    for operation in (
        lambda: parse_agent_target(ref),
        lambda: caps.resolve_remote_ref("skill", ref),
    ):
        with pytest.raises(ValueError, match="unsupported Source Ref"):
            operation()


@pytest.mark.parametrize("prefix", ["gh:", "github:", "github://"])
def test_exact_cap_aliases_do_not_use_shorthand_probes(monkeypatch, prefix):
    monkeypatch.setattr(
        caps,
        "_github_repo_default_branch",
        lambda *args: pytest.fail("unexpected default-branch probe"),
    )
    probes = []
    monkeypatch.setattr(
        caps, "_github_remote_exists", lambda kind, ref: probes.append(ref) or True
    )
    canonical = "github://owner/repo/skills/some-name@rev"
    assert (
        caps.resolve_remote_ref(
            "skill", f"{prefix}owner/repo/skills/some-name/SKILL.md@rev"
        )
        == canonical
    )
    assert probes == [canonical]


@pytest.mark.parametrize("prefix", ["gh:", "github:"])
def test_cap_shorthand_retains_kind_specific_probes(monkeypatch, prefix):
    monkeypatch.setattr(caps, "_github_repo_default_branch", lambda *args: "trunk")
    probes = []

    def exists(kind, ref):
        probes.append(ref)
        return ref == "github://owner/agent-skills/some-name@trunk"

    monkeypatch.setattr(caps, "_github_remote_exists", exists)
    assert (
        caps.resolve_remote_ref("skill", f"{prefix}owner/some-name")
        == "github://owner/agent-skills/some-name@trunk"
    )
    assert probes == [
        "github://owner/agents/skills/some-name@trunk",
        "github://owner/agent-skills/some-name@trunk",
    ]


@pytest.mark.parametrize("kind", ["skill", "psyche", "service", "prompt"])
def test_http_caps_are_prepared_and_projected_consistently(tmp_path, monkeypatch, kind):
    url = f"https://example.com/some-name{'/SKILL.md' if kind == 'skill' else '.md'}?version=2"
    calls = []

    def fetch(target):
        calls.append(target)
        if kind == "service":
            return b"---\ndescription: Review\ntransport: http\ntarget: https://example.com/mcp\n---\nReview.\n"
        return b"---\ndescription: Review\n---\nReview.\n"

    monkeypatch.setattr(caps, "_fetch_url_bytes", fetch)
    layout = AgentLayout.resident(tmp_path, "gus")
    layout.home.mkdir(parents=True)
    layout.program.write_text(f"with {kind} {url}\nflow chat():\n  pass\n")
    state = prepare_agent_state(layout)
    (entry,) = state.module_caps["agent"]
    (view,) = cap_collection((entry,), root=tmp_path, agent_name="gus").items
    info = CapInfo.from_cap(entry, agent_name="gus")
    assert calls == [url]
    assert view.data["cap"] == info.cap == f"{kind}/some-name"
    assert (
        view.data["source"]
        == info.source
        == (url.replace("/SKILL.md", "") if kind == "skill" else url)
    )
    assert "ref" not in view.data
    assert Path(entry.path).read_bytes() == fetch(url)
    assert cap_collection((entry,), root=tmp_path, agent_name="gus").query(
        f'{kind}/some-name[source="{info.source}"]'
    )

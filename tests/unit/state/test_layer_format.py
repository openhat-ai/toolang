"""Acceptance checks for directly usable, source-independent State revisions."""

from hashlib import sha256
import json
from pathlib import Path

import pytest

from toolang.catalog import source as source_owner
from toolang.common.layout import AgentLayout
from toolang.common.source import Acquisition
from toolang.lang.ast import Program
from toolang.state.cache import (
    canonical_json,
    layer_revision_dir,
    validate_agent_revision,
)
from toolang.state.prepare import load_agent_state, prepare_agent_state


def layout_at(root):
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow work():\n  pass\n")
    return layout


def document(layout, state):
    return json.loads(
        (
            layer_revision_dir(layout, "home", state.home_revision) / "layer.json"
        ).read_text()
    )


def install_sources(monkeypatch, targets):
    def acquire(kind, declared_ref, **kwargs):
        resolved, body = targets[declared_ref]
        entry = "skills/style/SKILL.md" if kind == "skill" else "prompts/style.md"
        return Acquisition(resolved, entry, {entry: body.encode()}, "style")

    monkeypatch.setattr(source_owner, "acquire", acquire)
    monkeypatch.setattr(
        source_owner,
        "reusable",
        lambda kind, declared, resolved: targets[declared][0] == resolved,
    )


def test_raw_inputs_direct_values_and_timestamped_identity(tmp_path, monkeypatch):
    layout = layout_at(tmp_path)
    raw = b'# Original bytes\r\n[workspaces]\r\nrepo = "."\r\n'
    layout.config.write_bytes(raw)
    (layout.home / "prompts").mkdir()
    (layout.home / "prompts/style.md").write_text(
        "---\ndescription: Style\n---\nBe concise.\n"
    )
    state = prepare_agent_state(layout)
    layer = document(layout, state)
    assert set(layer) == {
        "schema",
        "scope",
        "created_at",
        "config",
        "source",
        "materialized",
        "modules",
        "caps",
        "chores",
    }
    assert layer["source"]["config.toml"] == {
        "size": len(raw),
        "sha256": sha256(raw).hexdigest(),
    }
    directory = layer_revision_dir(layout, "home", state.home_revision)
    assert (directory / "source/config.toml").read_bytes() == raw
    assert not (directory / "files").exists()
    assert layer["caps"][0] == {
        "ref": "prompt/style",
        "source": "prompts/style.md",
        "form": "authored",
        "value": {"meta": {"description": "Style"}, "body": "Be concise."},
    }
    assert state.created_at
    assert state.home_revision == sha256(canonical_json(layer)).hexdigest()
    again = prepare_agent_state(layout, force=True)
    assert again.revision == state.revision
    assert again.created_at == state.created_at

    def no_parse(*args, **kwargs):
        pytest.fail("restoration must not reinterpret source syntax")

    monkeypatch.setattr(Program, "from_source", no_parse)
    read = Path.read_text

    def only_documents(path, *args, **kwargs):
        assert path.name in {"layer.json", "layers.json", "current"}
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", only_documents)
    restored = load_agent_state(layout, state.revision)
    assert restored.prompts["style"].read_content() == "Be concise."
    assert restored == state


def test_default_source_change_with_unchanged_inputs_and_frozen_history(
    tmp_path, monkeypatch
):
    layout = layout_at(tmp_path)
    layout.program.write_text("with prompt owner/style\nflow work():\n  pass\n")
    targets = {"owner/style": ("future-a:owner/style@v1", "First.")}
    install_sources(monkeypatch, targets)
    first = prepare_agent_state(layout)
    targets["owner/style"] = ("oh:owner/style@v1", "Second.")
    second = prepare_agent_state(layout)
    assert first.revision != second.revision
    assert first.prompts["style"].read_content() == "First."
    assert second.prompts["style"].read_content() == "Second."
    layer = document(layout, second)
    resolution = layer["caps"][0]["resolution"]
    assert resolution["declared_ref"] == "owner/style"
    assert resolution["materialized"].startswith(
        sha256(b"oh:owner/style@v1").hexdigest() + "/"
    )

    def no_source(*args, **kwargs):
        pytest.fail("offline history must not consult source/default selection")

    monkeypatch.setattr(source_owner, "acquire", no_source)
    monkeypatch.setattr(source_owner, "reusable", no_source)
    monkeypatch.setattr(source_owner, "parse_remote_source", no_source)
    assert (
        load_agent_state(layout, first.revision).prompts["style"].read_content()
        == "First."
    )
    assert (
        load_agent_state(layout, second.revision).prompts["style"].read_content()
        == "Second."
    )
    validate_agent_revision(layout, first.revision)
    validate_agent_revision(layout, second.revision)


def test_same_name_candidates_from_distinct_sources_remain_separate(
    tmp_path, monkeypatch
):
    layout = layout_at(tmp_path)
    layout.program.write_text(
        "with prompt provider-a:owner/style\nflow work():\n  pass\n"
    )
    (layout.home / "runnables").mkdir()
    (layout.home / "runnables/check.too").write_text(
        "with prompt provider-b:owner/style\nflow check():\n  pass\n"
    )
    targets = {
        "provider-a:owner/style": ("provider-a:owner/style@v1", "First."),
        "provider-b:owner/style": ("provider-b:owner/style@v1", "Second."),
    }
    install_sources(monkeypatch, targets)
    state = prepare_agent_state(layout)
    layer = document(layout, state)
    assert len(layer["caps"]) == 2
    assert {item["ref"] for item in layer["caps"]} == {"prompt/style"}
    assert len(layer["materialized"]) == 2
    assert [cap.read_content() for cap in state.caps_for("agent")] == ["First."]
    assert [cap.read_content() for cap in state.caps_for("check")] == ["Second."]
    validate_agent_revision(layout, state.revision)


def test_mutable_location_bytes_and_a_b_a_create_new_revisions(tmp_path, monkeypatch):
    layout = layout_at(tmp_path)
    layout.program.write_text("with prompt future:owner/style\nflow work():\n  pass\n")
    targets = {"future:owner/style": ("future:owner/style", "First.")}
    install_sources(monkeypatch, targets)
    first = prepare_agent_state(layout)
    assert prepare_agent_state(layout, force=True).revision == first.revision
    targets["future:owner/style"] = ("future:owner/style", "Second.")
    second = prepare_agent_state(layout, force=True)
    targets["future:owner/style"] = ("future:owner/style", "First.")
    returned = prepare_agent_state(layout, force=True)
    assert len({first.revision, second.revision, returned.revision}) == 3
    assert (
        first.prompts["style"].read_content()
        == returned.prompts["style"].read_content()
    )


def test_rejected_sources_have_no_captured_bytes(tmp_path):
    layout = layout_at(tmp_path)
    (layout.home / "runnables").mkdir()
    (layout.home / "runnables/broken.too").write_text("not valid Toolang\n")
    state = prepare_agent_state(layout)
    layer = document(layout, state)
    record = layer["source"]["runnables/broken.too"]
    assert record["error"]
    assert "accepted" not in record
    assert not (
        layer_revision_dir(layout, "home", state.home_revision)
        / "source/runnables/broken.too"
    ).exists()
    validate_agent_revision(layout, state.revision)


@pytest.mark.parametrize("damage", ["time", "value", "file", "extra", "missing"])
def test_integrity_covers_documents_and_exact_file_sets(tmp_path, damage):
    layout = layout_at(tmp_path)
    state = prepare_agent_state(layout)
    directory = layer_revision_dir(layout, "home", state.home_revision)
    if damage in {"time", "value"}:
        layer = document(layout, state)
        if damage == "time":
            layer["created_at"] = "2000-01-01T00:00:00.000000Z"
        else:
            layer["modules"][0]["value"] = {}
        (directory / "layer.json").write_bytes(canonical_json(layer))
    elif damage == "file":
        (directory / "source/agent.too").write_text("tampered")
    elif damage == "extra":
        (directory / "materialized/extra").write_text("unexpected")
    else:
        (directory / "source/agent.too").unlink()
    with pytest.raises(ValueError):
        validate_agent_revision(layout, state.revision)


def test_watcher_rechecks_default_selection_without_file_changes(tmp_path, monkeypatch):
    import asyncio
    from toolang.state.watcher import StateWatcher

    layout = layout_at(tmp_path)
    layout.program.write_text("with prompt owner/style\nflow work():\n  pass\n")
    targets = {"owner/style": ("first:owner/style@v1", "First.")}
    install_sources(monkeypatch, targets)
    watcher = StateWatcher(layout)
    first = asyncio.run(watcher.refresh())
    assert asyncio.run(watcher._perform_check(requested=False)).state is first
    targets["owner/style"] = ("oh:owner/style@v1", "Second.")
    changed = asyncio.run(watcher._perform_check(requested=False)).state
    assert changed.revision != first.revision
    assert changed.prompts["style"].read_content() == "Second."


def test_conflicting_bytes_for_shared_acquisition_are_rejected(tmp_path, monkeypatch):
    layout = layout_at(tmp_path)
    layout.config.write_text(
        '[prompts]\none = {ref="first:owner/style"}\ntwo = {ref="second:owner/style"}\n'
    )
    targets = {
        "first:owner/style": ("canonical:owner/style@v1", "First."),
        "second:owner/style": ("canonical:owner/style@v1", "Second."),
    }
    install_sources(monkeypatch, targets)
    with pytest.raises(ValueError, match="conflicting acquired file"):
        prepare_agent_state(layout)


def test_conflicting_module_acquisitions_exclude_only_the_conflicting_program(
    tmp_path, monkeypatch
):
    layout = layout_at(tmp_path)
    runnables = layout.home / "runnables"
    runnables.mkdir()
    for name in ("first", "second"):
        (runnables / f"{name}.too").write_text(
            f"with prompt future:owner/style\nflow {name}():\n  pass\n"
        )
    (runnables / "other.too").write_text("flow other():\n  pass\n")
    bodies = iter((b"First.", b"Second."))

    def acquire(*args, **kwargs):
        return Acquisition(
            "future:owner/style@v1", "style.md", {"style.md": next(bodies)}, "style"
        )

    monkeypatch.setattr(source_owner, "acquire", acquire)
    monkeypatch.setattr(source_owner, "reusable", lambda *args: True)
    state = prepare_agent_state(layout)
    assert set(state.runnables) == {"work", "first", "other"}
    assert state.caps_for("first")[0].read_content() == "First."
    rejected = next(file for file in state.files if not file.accepted)
    assert rejected.key == "runnables/second.too"
    assert rejected.message and "conflicting acquired files" in rejected.message
    layer = document(layout, state)
    assert len(layer["caps"]) == len(layer["materialized"]) == 1
    validate_agent_revision(layout, state.revision)
    assert prepare_agent_state(layout).revision == state.revision


@pytest.mark.parametrize("form", ["authored", "referenced"])
@pytest.mark.parametrize("number", [".nan", ".inf", "-.inf"])
def test_nonfinite_metadata_excludes_only_its_resource(
    tmp_path, monkeypatch, form, number
):
    layout = layout_at(tmp_path)
    prompts = layout.home / "prompts"
    prompts.mkdir()
    (prompts / "good.md").write_text("Valid prompt.")
    content = f"---\nweights:\n  - {number}\n---\nInvalid metadata."
    if form == "authored":
        source = "prompts/bad.md"
        (layout.home / source).write_text(content)
    else:
        source = "agent.too"
        layout.program.write_text(
            "with prompt future:owner/style\nflow work():\n  pass\n"
        )
        install_sources(
            monkeypatch, {"future:owner/style": ("future:owner/style@v1", content)}
        )
    state = prepare_agent_state(layout)
    assert set(state.prompts) == {"good"}
    rejected = next(file for file in state.files if not file.accepted)
    assert rejected.key == source
    assert rejected.message and "finite" in rejected.message
    validate_agent_revision(layout, state.revision)


@pytest.mark.parametrize("second_assets", [{}, {"second.py": b"pass\n"}])
def test_shared_acquisitions_require_the_same_file_set(
    tmp_path, monkeypatch, second_assets
):
    layout = layout_at(tmp_path)
    layout.config.write_text(
        '[skills]\none = {ref="future:first"}\ntwo = {ref="future:second"}\n'
    )

    def acquire(kind, declared_ref, **kwargs):
        assets = (
            {"first.py": b"pass\n"} if declared_ref == "future:first" else second_assets
        )
        return Acquisition(
            "future:registry/review@v1",
            "SKILL.md",
            {"SKILL.md": b"Review carefully.", **assets},
            "review",
        )

    monkeypatch.setattr(source_owner, "acquire", acquire)
    with pytest.raises(ValueError, match="conflicting acquired file"):
        prepare_agent_state(layout)


def test_equal_bytes_at_a_new_commit_change_provenance(tmp_path, monkeypatch):
    layout = layout_at(tmp_path)
    ref = "gh:owner/repo/style.md@main"
    layout.program.write_text(f"with prompt {ref}\nflow work():\n  pass\n")
    version = ["a" * 40]
    fetched = []
    monkeypatch.setattr(source_owner, "_resolve_github_commit", lambda ref: version[0])
    monkeypatch.setattr(
        source_owner,
        "_fetch_github_file",
        lambda ref: fetched.append(ref.rev) or b"Same content.",
    )
    first = prepare_agent_state(layout)
    version[0] = "b" * 40
    assert prepare_agent_state(layout).revision == first.revision
    second = prepare_agent_state(layout, force=True)
    assert second.revision != first.revision
    assert (
        second.prompts["style"].read_content() == first.prompts["style"].read_content()
    )
    assert second.prompts["style"].ref.endswith("@" + version[0])
    assert prepare_agent_state(layout, force=True).revision == second.revision
    assert fetched == ["a" * 40, "b" * 40]


def test_explicit_source_is_unaffected_by_default_selection(tmp_path, monkeypatch):
    layout = layout_at(tmp_path)
    layout.program.write_text(
        "with prompt explicit:owner/style\nflow work():\n  pass\n"
    )
    targets = {"explicit:owner/style": ("registry.example:owner/style@v1", "Stable.")}
    install_sources(monkeypatch, targets)
    first = prepare_agent_state(layout)
    targets["owner/style"] = ("openhat:owner/style@v2", "New default.")
    assert prepare_agent_state(layout).revision == first.revision


def test_generic_acquisition_reuse_keeps_original_name_and_all_files(
    tmp_path, monkeypatch
):
    layout = layout_at(tmp_path)
    layout.program.write_text("with skill future:review\nflow work():\n  pass\n")
    captured = Acquisition(
        "future:registry/review@v1",
        "content/SKILL.md",
        {
            "content/SKILL.md": b"---\ndescription: Review\n---\nReview carefully.",
            "content/check.py": b"pass\n",
            "LICENSE": b"License text.",
        },
        "review",
    )
    received = []

    def acquire(kind, ref, *, cached=None, **kwargs):
        if cached is not None:
            received.append(cached)
        return cached or captured

    monkeypatch.setattr(source_owner, "acquire", acquire)
    monkeypatch.setattr(source_owner, "reusable", lambda *args: True)
    first = prepare_agent_state(layout)
    layout.program.write_text(layout.program.read_text() + "\n# Local change\n")
    second = prepare_agent_state(layout)
    assert second.skills["review"].read_content() == "Review carefully."
    assert received[0].name == "review"
    assert dict(received[0].files) == dict(captured.files)
    assert (
        document(layout, first)["materialized"]
        == document(layout, second)["materialized"]
    )
    validate_agent_revision(layout, second.revision)


@pytest.mark.parametrize(
    "damage",
    [
        "config",
        "cap-source",
        "cap-value",
        "module-source",
        "default-source",
        "chore-title",
        "chore-schedule",
        "orphaned-acquisition",
    ],
)
def test_integrity_checks_stored_shapes_without_interpreting_source(
    tmp_path, monkeypatch, damage
):
    import shutil
    from toolang.state.cache import validate_layer_revision

    layout = layout_at(tmp_path)
    (layout.home / "prompts").mkdir()
    (layout.home / "prompts/style.md").write_text("Be concise.")
    (layout.home / "chores").mkdir()
    (layout.home / "chores/review.md").write_text("Review changes.")
    state = prepare_agent_state(layout)
    layer = document(layout, state)
    if damage == "config":
        layer["config"] = {"workspaces": {"repo": "relative/path"}}
    elif damage == "cap-source":
        layer["caps"][0]["source"] = "agent.too"
    elif damage == "cap-value":
        layer["caps"][0]["value"]["body"] = 42
    elif damage == "module-source":
        layer["modules"][0]["source"] = None
    elif damage == "default-source":
        from toolang.lang.ast import Span, to_data

        layer["modules"][0]["source"] = None
        layer["modules"][0]["value"] = to_data(Program(span=Span(1)))
    elif damage.startswith("chore-"):
        layer["chores"][0]["value"][damage.removeprefix("chore-")] = " "
    else:
        layer["materialized"]["a" * 64 + "/unused.md"] = {
            "size": 0,
            "sha256": sha256(b"").hexdigest(),
        }
    encoded = canonical_json(layer)
    revision = sha256(encoded).hexdigest()
    original = layer_revision_dir(layout, "home", state.home_revision)
    forged = layer_revision_dir(layout, "home", revision)
    shutil.copytree(original, forged)
    (forged / "layer.json").write_bytes(encoded)
    monkeypatch.setattr(
        Program,
        "from_source",
        lambda *args: pytest.fail("integrity must not parse source"),
    )
    with pytest.raises(ValueError):
        validate_layer_revision(layout, "home", revision)


def test_authored_skill_keeps_public_definition_location(tmp_path):
    from toolang.state.schemas import CapInfo

    layout = layout_at(tmp_path)
    directory = layout.home / "skills/review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\ndescription: Review\n---\nReview carefully."
    )
    state = prepare_agent_state(layout)
    assert (
        CapInfo.from_cap(state.skills["review"], agent_name=layout.name).definition_file
        == "agents/alice/skills/review"
    )
    assert document(layout, state)["caps"][0]["source"] == "skills/review/SKILL.md"


def test_rejected_composition_does_not_publish_its_revision(tmp_path):
    from toolang.state.cache import load_current_agent_revision

    layout = layout_at(tmp_path)
    layout.config.write_text('[workspaces]\nrepo = "."\n')
    before = prepare_agent_state(layout)
    with pytest.raises(ValueError, match="workspace name already exists"):
        prepare_agent_state(layout, workspace_additions={"repo": str(tmp_path)})
    assert load_current_agent_revision(layout) == before.revision
    assert load_agent_state(layout).revision == before.revision


def test_nested_cap_metadata_round_trips_through_json(tmp_path):
    layout = layout_at(tmp_path)
    services = layout.home / "services"
    services.mkdir()
    (services / "review.md").write_text(
        "---\ndescription: Review\ntransport: http\ntarget: https://example.com/mcp\n"
        "headers:\n  x-test: value\nenv: [FIRST, SECOND]\n---\nReview changes."
    )
    state = prepare_agent_state(layout)
    meta = document(layout, state)["caps"][0]["value"]["meta"]
    assert meta["headers"] == {"x-test": "value"}
    assert meta["env"] == ["FIRST", "SECOND"]
    restored = load_agent_state(layout, state.revision)
    assert restored.services["review"].meta == state.services["review"].meta
    validate_agent_revision(layout, state.revision)


def test_visiting_dependency_failure_excludes_only_its_program(tmp_path, monkeypatch):
    from toolang.up import source as agent_source

    layout = agent_source.materialize_visiting_program(
        agent_source.HttpRef("https://example.com/dev.too"),
        "with skill https://example.com/missing\nflow main():\n  pass\n",
        toolang_root=tmp_path,
    )
    runnable = layout.home / "runnables/check.too"
    runnable.parent.mkdir()
    runnable.write_text("flow check():\n  pass\n")

    def unavailable(*args, **kwargs):
        raise ValueError("remote skill unavailable")

    monkeypatch.setattr(source_owner, "acquire", unavailable)
    state = prepare_agent_state(layout)
    assert "check" in state.runnables
    assert "main" not in state.runnables
    layer = document(layout, state)
    agent = next(item for item in layer["modules"] if item["ref"] == "agent")
    assert agent["source"] is None
    assert "resolution" not in agent
    assert layer["source"]["agent.too"]["error"]
    validate_agent_revision(layout, state.revision)


def test_local_visiting_program_edits_publish_without_stale_provenance(
    tmp_path, monkeypatch
):
    from toolang.up import source as agent_source
    from toolang.state import prepare

    original = "flow original():\n  pass\n"
    layout = agent_source.materialize_visiting_program(
        agent_source.HttpRef("https://example.com/dev.too"),
        original,
        toolang_root=tmp_path,
    )
    acquired = prepare_agent_state(layout)
    assert document(layout, acquired)["modules"][0]["resolution"]
    layout.program.write_text("flow edited():\n  pass\n")
    edited = prepare_agent_state(layout)
    assert set(edited.runnables) == {"edited"}
    layer = document(layout, edited)
    assert "resolution" not in layer["modules"][0]
    assert not layer["materialized"]
    validate_agent_revision(layout, edited.revision)
    assert set(load_agent_state(layout, acquired.revision).runnables) == {"original"}

    with monkeypatch.context() as patch:
        patch.setattr(
            prepare,
            "_prepare_layer_revision",
            lambda **kwargs: pytest.fail("unchanged local edit should reuse State"),
        )
        assert prepare_agent_state(layout).revision == edited.revision
    layout.program.write_text(original)
    restored = prepare_agent_state(layout)
    assert (
        document(layout, restored)["modules"][0]["resolution"]
        == document(layout, acquired)["modules"][0]["resolution"]
    )
    assert len({acquired.revision, edited.revision, restored.revision}) == 3
    validate_agent_revision(layout, restored.revision)


def test_configured_aliases_keep_their_own_declared_refs(tmp_path, monkeypatch):
    layout = layout_at(tmp_path)
    refs = {
        "alpha": "gh:owner/repo/style.md@main",
        "beta": "github:owner/repo/style.md@main",
    }
    layout.config.write_text(
        "[prompts]\n"
        + "".join(f'{name} = {{ref="{ref}"}}\n' for name, ref in refs.items())
    )
    monkeypatch.setattr(source_owner, "_resolve_github_commit", lambda ref: "c" * 40)
    monkeypatch.setattr(source_owner, "_fetch_github_file", lambda ref: b"Shared.")
    first = prepare_agent_state(layout)
    caps = document(layout, first)["caps"]
    assert {
        item["ref"].split("/")[1]: item["resolution"]["declared_ref"] for item in caps
    } == refs
    assert len(document(layout, first)["materialized"]) == 1

    def no_fetch(*args, **kwargs):
        pytest.fail("unchanged aliases must retain their own reusable acquisitions")

    monkeypatch.setattr(source_owner, "_fetch_github_file", no_fetch)
    layout.program.write_text(layout.program.read_text() + "\n# Local edit\n")
    second = prepare_agent_state(layout)
    assert set(second.prompts) == {"alpha", "beta"}
    validate_agent_revision(layout, second.revision)


def test_repeated_with_declarations_keep_one_cap_and_the_first_location(
    tmp_path, monkeypatch
):
    layout = layout_at(tmp_path)
    declaration = "with prompt https://example.com/style.md\n"
    layout.program.write_text(declaration * 2 + "flow work():\n  pass\n")
    monkeypatch.setattr(source_owner, "_fetch_url_bytes", lambda url: b"Be concise.")
    state = prepare_agent_state(layout)
    assert len(document(layout, state)["caps"]) == 1
    assert state.prompts["style"].source.line == 1
    assert len(state.modules["agent"].withs) == 2
    assert (
        load_agent_state(layout, state.revision).prompts["style"].read_content()
        == "Be concise."
    )
    validate_agent_revision(layout, state.revision)


@pytest.mark.parametrize("module", ["research", "DeepSearch", "_helper"])
def test_retained_runnable_module_names_decode_without_rewriting_sources(
    tmp_path, module
):
    from toolang.state.cache import _decode_modules

    layout = layout_at(tmp_path)
    modules = layout.home / "runnables"
    modules.mkdir()
    source = modules / f"{module}.too"
    source.write_text("flow():\n  pass\n")
    state = prepare_agent_state(layout)
    layer = document(layout, state)
    entry = next(item for item in layer["modules"] if item["ref"] == module)
    entry["ref"] = f"runnables::{module}"
    layer["modules"].sort(key=lambda item: item["ref"])
    programs, sources, digests = _decode_modules(layer)
    assert list(programs) == sorted(programs)
    assert list(sources) == sorted(sources)
    assert list(digests) == sorted(digests)
    assert set(programs) == {"agent", module}
    assert sources[module] == f"runnables/{module}.too"
    assert entry["ref"] == f"runnables::{module}"
    assert source.read_text() == "flow():\n  pass\n"

"""State formats converge independently while sharing authored source."""

import asyncio
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from pathlib import Path

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import cache, format as state_format, prepare, state
from toolang.state.format import AGENT_STATE_FORMAT
from toolang.state.watcher import StateWatcher


def _prepare_format(root, version, legacy=False, start=None):
    """Emulate another release in an isolated interpreter, without old binaries."""
    with pytest.MonkeyPatch.context() as patch:
        for owner in (state_format, state, cache, prepare):
            patch.setattr(owner, "AGENT_STATE_FORMAT", version)
        if legacy:
            patch.setattr(
                cache,
                "state_root",
                lambda layout, scope: (
                    layout.place_state if scope == "root" else layout.home_state
                ),
            )
            patch.setattr(cache, "_agent_state_root", lambda layout: layout.agent_state)
        layout = AgentLayout.resident(Path(root), "alice")
        if start is not None:
            # Both formats must acquire their complete lock set independently.
            with (
                cache._agent_publish_lock(layout),
                cache.layer_build_lock(layout, "root"),
                cache.layer_build_lock(layout, "home"),
            ):
                start.wait(timeout=10)
        first = prepare.prepare_agent_state(layout)
        for force in (False, True, False):
            assert prepare.prepare_agent_state(layout, force=force).revision == (
                first.revision
            )
        watcher = StateWatcher(layout, initial_state=first)
        assert asyncio.run(watcher._perform_check(requested=False)).state is first
        cache.validate_agent_revision(layout, first.revision)
        return first.root_revision, first.home_revision, first.revision


def _layout(root):
    layout = AgentLayout.resident(root, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("flow work():\n  pass\n")
    prompt = layout.place / "prompts/style.md"
    prompt.parent.mkdir()
    prompt.write_text("First style.\n")
    return layout


def _stores(layout, version, legacy=False):
    anchors = layout.place_state, layout.home_state, layout.agent_state
    return tuple(anchor if legacy else anchor / f"v{version}" for anchor in anchors)


def _snapshot(stores):
    return {
        path: path.read_bytes()
        for store in stores
        for path in (
            store / "current",
            store / "build.lock",
            store / "publish.lock",
            *(store / "revs").rglob("*"),
        )
        if path.is_file()
    }


@pytest.mark.parametrize("legacy", [False, True])
def test_alternating_formats_converge_and_accept_shared_source_changes(
    tmp_path, legacy
):
    layout = _layout(tmp_path)
    other = AGENT_STATE_FORMAT - 1 if legacy else AGENT_STATE_FORMAT + 1
    writers = ((AGENT_STATE_FORMAT, False), (other, legacy))
    snapshots = {}
    previous = {}
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as pool:
        for source_version in range(2):
            if source_version:
                layout.program.write_text("# Changed source\nflow work():\n  pass\n")
                (layout.place / "prompts/style.md").write_text("Changed style.\n")
            expected = {}
            for _ in range(3):
                for version, old_layout in writers:
                    revisions = pool.submit(
                        _prepare_format, str(tmp_path), version, old_layout
                    ).result(timeout=20)
                    if version in expected:
                        assert revisions == expected[version]
                    else:
                        if source_version:
                            assert all(
                                new != old
                                for new, old in zip(revisions, previous[version])
                            )
                        expected[version] = revisions
                    stores = _stores(layout, version, old_layout)
                    for store, revision in zip(stores, revisions):
                        assert (store / "current").read_text().strip() == revision
                        assert (
                            len(list((store / "revs").iterdir())) == source_version + 1
                        )
                    for foreign, foreign_legacy in writers:
                        if foreign != version and foreign in snapshots:
                            assert (
                                _snapshot(_stores(layout, foreign, foreign_legacy))
                                == snapshots[foreign]
                            )
                    snapshots[version] = _snapshot(stores)
            previous = expected


def test_different_formats_acquire_derived_locks_and_prepare_concurrently(tmp_path):
    layout = _layout(tmp_path)
    versions = AGENT_STATE_FORMAT, AGENT_STATE_FORMAT + 1
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager:
        start = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as pool:
            futures = [
                pool.submit(_prepare_format, str(tmp_path), version, False, start)
                for version in versions
            ]
            results = [future.result(timeout=20) for future in futures]
    for version, revisions in zip(versions, results):
        for store, revision in zip(_stores(layout, version), revisions):
            assert [path.name for path in (store / "revs").iterdir()] == [revision]
    assert all(left != right for left, right in zip(*results))


@pytest.mark.parametrize("legacy", [False, True])
def test_adoption_ignores_old_stores_without_reading_or_mutating_them(
    tmp_path, monkeypatch, legacy
):
    layout = _layout(tmp_path)
    old = prepare.prepare_agent_state(layout)
    stores = _stores(layout, AGENT_STATE_FORMAT)
    foreign_stores = _stores(layout, AGENT_STATE_FORMAT - 1, legacy)
    for current, foreign in zip(stores, foreign_stores):
        if legacy:
            for path in current.iterdir():
                path.rename(foreign / path.name)
            current.rmdir()
        else:
            current.rename(foreign)
    saved = _snapshot(foreign_stores)
    open_file = Path.open

    def forbid_foreign_reads(path, *args, **kwargs):
        assert path not in saved, f"State adoption read a foreign file: {path}"
        return open_file(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbid_foreign_reads)
        with pytest.raises(FileNotFoundError):
            prepare.load_agent_state(layout, old.revision)
        new = prepare.prepare_agent_state(layout)
        assert prepare.prepare_agent_state(layout).revision == new.revision
        assert new.revision != old.revision
        for loader, revision in (
            (cache.load_root_layer, old.root_revision),
            (cache.load_home_layer, old.home_revision),
            (prepare.load_agent_state, old.revision),
            (cache.validate_agent_revision, old.revision),
        ):
            with pytest.raises(FileNotFoundError):
                loader(layout, revision)
        assert prepare.load_agent_state(layout).revision == new.revision
    assert _snapshot(foreign_stores) == saved
    for store in stores:
        assert len(list((store / "revs").iterdir())) == 1


def test_exact_history_load_and_validation_do_not_require_current_pointers(tmp_path):
    layout = _layout(tmp_path)
    historical = prepare.prepare_agent_state(layout)
    for store in _stores(layout, AGENT_STATE_FORMAT):
        (store / "current").unlink()
    layout.program.unlink()
    (layout.place / "prompts/style.md").unlink()
    assert prepare.load_agent_state(layout, historical.revision) == historical
    cache.validate_agent_revision(layout, historical.revision)

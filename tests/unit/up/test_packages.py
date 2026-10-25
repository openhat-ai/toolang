from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
from zipfile import ZipFile

import pytest

from toolang.up import packages


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source with spaces"
    module = root / "src" / "toolang"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("")
    (module / "example.py").write_text("value = 1\n")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "toolang"\nversion = "0.4.0b1"\n'
    )
    (root / "hatch_build.py").write_text("# Build hook\n")
    monkeypatch.setattr(packages, "repository_source_version", lambda _: "v0.4.0b1")
    return root


@pytest.fixture
def builds(monkeypatch):
    calls = []
    monkeypatch.setattr(packages.shutil, "which", lambda _: "/tools/uv")

    def build(command, **kwargs):
        assert kwargs == {"capture_output": True, "text": True}
        calls.append(command)
        _write_wheel(Path(command[command.index("--out-dir") + 1]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(packages.subprocess, "run", build)
    return calls


def _write_wheel(output):
    wheel = output / "toolang-0.4.0b1-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "toolang-0.4.0b1.dist-info/METADATA", "Name: toolang\nVersion: 0.4.0b1\n"
        )
    return wheel


@pytest.mark.parametrize(
    "version", ["0.4.0", "0.4.0a1", "0.4.0b1", "0.4.0rc1", "0.4.0.dev1"]
)
def test_installed_packages_use_the_exact_distribution_version(
    tmp_path, monkeypatch, version
):
    from toolang.common import version as versions

    monkeypatch.setattr(packages, "development_source", lambda: (False, None))
    monkeypatch.setattr(versions, "package_version", lambda _: version)
    assert packages.resolve_guest_package(None, cache=tmp_path) == (
        f"toolang=={version}",
        None,
    )
    assert not list(tmp_path.iterdir())


def test_explicit_wheel_bypasses_installation_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(
        packages, "development_source", lambda: pytest.fail("must not inspect metadata")
    )
    wheel = tmp_path / "toolang-0.4.0a1-py3-none-any.whl"
    assert packages.resolve_guest_package(wheel, cache=tmp_path) == (None, wheel)


def test_editable_source_builds_from_its_origin_not_the_working_directory(
    source, tmp_path, monkeypatch, builds
):
    monkeypatch.setattr(packages, "development_source", lambda: (True, source))
    monkeypatch.chdir(tmp_path)
    requirement, artifact = packages.resolve_guest_package(
        None, cache=tmp_path / "cache"
    )
    assert requirement is None
    assert artifact is not None
    assert artifact.is_file()
    assert builds[0][-1] == str(source)
    assert not (source / "dist").exists()


def test_editable_without_a_source_path_fails_without_an_index_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(packages, "development_source", lambda: (True, None))
    with pytest.raises(ValueError, match="Cannot locate.*--dev PATH"):
        packages.resolve_guest_package(None, cache=tmp_path)


def test_unchanged_source_reuses_a_cached_wheel(source, tmp_path, builds):
    cache = tmp_path / "cache"
    events = []
    first = packages.build_source_wheel(source, cache=cache, progress=events.append)
    assert (
        packages.build_source_wheel(source, cache=cache, progress=events.append)
        == first
    )
    assert len(builds) == 1
    assert events[-1].cached


@pytest.mark.parametrize("change", ["edit", "add", "delete", "config", "hook"])
def test_changed_build_inputs_invalidate_the_wheel(source, tmp_path, builds, change):
    cache = tmp_path / "cache"
    first = packages.build_source_wheel(source, cache=cache)
    module = source / "src" / "toolang"
    if change == "edit":
        (module / "example.py").write_text("value = 2\n")
    elif change == "add":
        (module / "added.py").write_text("value = 3\n")
    elif change == "delete":
        (module / "example.py").unlink()
    elif change == "config":
        with (source / "pyproject.toml").open("a") as config:
            config.write('description = "changed"\n')
    else:
        (source / "hatch_build.py").write_text("# New build hook\n")
    second = packages.build_source_wheel(source, cache=cache)
    assert second != first
    assert len(builds) == 2


def test_bytecode_does_not_invalidate_a_source_wheel(source, tmp_path, builds):
    cache = tmp_path / "cache"
    first = packages.build_source_wheel(source, cache=cache)
    bytecode = source / "src" / "toolang" / "__pycache__"
    bytecode.mkdir()
    (bytecode / "example.cpython-313.pyc").write_bytes(b"bytecode")
    assert packages.build_source_wheel(source, cache=cache) == first
    assert len(builds) == 1


def test_changed_source_provenance_invalidates_the_wheel(
    source, tmp_path, monkeypatch, builds
):
    cache = tmp_path / "cache"
    first = packages.build_source_wheel(source, cache=cache)
    monkeypatch.setattr(packages, "repository_source_version", lambda _: "v0.4.0b1*")
    assert packages.build_source_wheel(source, cache=cache) != first
    assert len(builds) == 2


@pytest.mark.parametrize("failure", ["exit", "invalid", "changed"])
def test_failed_builds_do_not_publish_cached_wheels(
    source, tmp_path, monkeypatch, failure
):
    def build(command, **kwargs):
        output = Path(command[command.index("--out-dir") + 1])
        if failure == "invalid":
            (output / "toolang-invalid.whl").write_text("broken")
        else:
            _write_wheel(output)
        if failure == "changed":
            (source / "src" / "toolang" / "example.py").write_text("changed\n")
        return subprocess.CompletedProcess(
            command, 1 if failure == "exit" else 0, stderr="build backend failed"
        )

    monkeypatch.setattr(packages.shutil, "which", lambda _: "/tools/uv")
    monkeypatch.setattr(packages.subprocess, "run", build)
    message = {
        "exit": "build backend failed",
        "invalid": "valid wheel",
        "changed": "source changed",
    }[failure]
    cache = tmp_path / "cache"
    with pytest.raises(RuntimeError, match=message):
        packages.build_source_wheel(source, cache=cache)
    assert not list(cache.rglob("*.whl"))
    assert not list(cache.glob(".build-*"))


def test_corrupt_cached_wheel_is_rebuilt(source, tmp_path, builds):
    cache = tmp_path / "cache"
    wheel = packages.build_source_wheel(source, cache=cache)
    wheel.write_bytes(b"broken")
    assert packages.build_source_wheel(source, cache=cache) == wheel
    assert packages._valid_wheel(wheel)
    assert len(builds) == 2


def test_concurrent_launches_build_once(source, tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    calls = []
    monkeypatch.setattr(packages.shutil, "which", lambda _: "/tools/uv")

    def build(command, **kwargs):
        calls.append(command)
        _write_wheel(Path(command[command.index("--out-dir") + 1]))
        return subprocess.CompletedProcess(command, 0)

    def launch():
        barrier.wait(timeout=5)
        return packages.build_source_wheel(source, cache=tmp_path / "cache")

    monkeypatch.setattr(packages.subprocess, "run", build)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(launch) for _ in range(2)]
        assert futures[0].result() == futures[1].result()
    assert len(calls) == 1


def test_build_command_uses_pip_when_uv_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(packages.shutil, "which", lambda _: None)
    monkeypatch.setattr(packages, "find_spec", lambda _: object())
    command = packages._build_command(tmp_path / "source", tmp_path / "output")
    assert command[1:5] == ["-m", "pip", "wheel", "--no-deps"]
    assert command[-1] == str(tmp_path / "source")


def test_missing_build_tools_have_an_explicit_wheel_remedy(tmp_path, monkeypatch):
    monkeypatch.setattr(packages.shutil, "which", lambda _: None)
    monkeypatch.setattr(packages, "find_spec", lambda _: None)
    with pytest.raises(RuntimeError, match="requires uv or pip.*--dev PATH"):
        packages._build_command(tmp_path, tmp_path)

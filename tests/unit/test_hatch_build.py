from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from dulwich import porcelain

import hatch_build


def test_collect_build_info_captures_native_git_description(
    tmp_path: Path,
) -> None:
    porcelain.init(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("tagged\n", encoding="utf-8")
    porcelain.add(tmp_path, tracked.name)
    porcelain.commit(
        tmp_path,
        message=b"Tagged commit",
        author=b"Test Author <test@example.com>",
        committer=b"Test Author <test@example.com>",
    )
    porcelain.tag_create(tmp_path, "v0.2.7")
    tracked.write_text("committed after tag\n", encoding="utf-8")
    porcelain.add(tmp_path, tracked.name)
    revision = porcelain.commit(
        tmp_path,
        message=b"Commit after tag",
        author=b"Test Author <test@example.com>",
        committer=b"Test Author <test@example.com>",
    ).decode("ascii")
    tracked.write_text("dirty build input\n", encoding="utf-8")

    assert hatch_build.collect_build_info(tmp_path) == {
        "schema": 1,
        "source_version": f"v0.2.7-1-g{revision[:8]}*",
        "revision": revision,
        "dirty": True,
    }


def test_collect_build_info_reuses_inherited_sdist_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inherited = {
        "schema": 1,
        "source_version": "v0.2.7-87-g3b492a92*",
        "revision": "3b492a92f1ed6282fc5b57a02d091339059cabcf",
        "dirty": True,
    }
    path = tmp_path / "src" / "toolang" / "_build_info.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(inherited), encoding="utf-8")
    monkeypatch.setattr(
        hatch_build,
        "_repository_build_info",
        lambda *_args: pytest.fail("an sdist must not be redescribed"),
    )

    assert hatch_build.collect_build_info(tmp_path) == inherited


def test_collect_build_info_rejects_invalid_inherited_provenance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "src" / "toolang" / "_build_info.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid Toolang build info"):
        hatch_build.collect_build_info(tmp_path)


@pytest.mark.parametrize(
    ("target_name", "included_path"),
    (
        ("sdist", "src/toolang/_build_info.json"),
        ("wheel", "toolang/_build_info.json"),
    ),
)
def test_custom_hook_injects_and_cleans_build_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    included_path: str,
) -> None:
    build_info = {
        "schema": 1,
        "source_version": "v0.3.0",
        "revision": "3b492a92f1ed6282fc5b57a02d091339059cabcf",
        "dirty": False,
    }
    monkeypatch.setattr(hatch_build, "collect_build_info", lambda _root: build_info)
    hook = hatch_build.CustomBuildHook(
        str(tmp_path),
        {},
        cast(Any, None),
        cast(Any, None),
        "",
        target_name,
    )
    build_data: dict[str, Any] = {"force_include": {}}

    hook.initialize("standard", build_data)

    generated, target = next(iter(build_data["force_include"].items()))
    generated_path = Path(generated)
    assert target == included_path
    assert json.loads(generated_path.read_text(encoding="utf-8")) == build_info

    hook.finalize("standard", build_data, "artifact")

    assert not generated_path.exists()


def test_custom_hook_skips_editable_builds(tmp_path: Path) -> None:
    hook = hatch_build.CustomBuildHook(
        str(tmp_path),
        {},
        cast(Any, None),
        cast(Any, None),
        "",
        "wheel",
    )
    build_data: dict[str, Any] = {"force_include": {}}

    hook.initialize("editable", build_data)

    assert build_data["force_include"] == {}

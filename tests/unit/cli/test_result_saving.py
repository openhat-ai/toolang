from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

import toolang.cli.common.result_saving as result_saving
from toolang.base.types.message import ImagePart, TextPart
from toolang.cli.common.result_saving import save_result, serialize_result


def test_result_serialization_preserves_text_and_compacts_structured_parts() -> None:
    assert serialize_result((TextPart("first"), TextPart("\nsecond"))) == (
        "first\nsecond"
    )
    assert serialize_result(
        (TextPart("caption"), ImagePart(image_url="https://example.com/image.png"))
    ) == (
        '[{"type":"text","text":"caption"},'
        '{"type":"image","detail":"auto",'
        '"image_url":"https://example.com/image.png"}]'
    )


def test_result_saving_writes_exact_stdout_and_atomic_file_content(
    tmp_path: Path,
) -> None:
    stdout = StringIO()
    destination = tmp_path / "result.txt"
    destination.write_text("before", encoding="utf-8")
    destination.chmod(0o640)
    parts = (
        TextPart("caption"),
        ImagePart(image_url="https://example.com/image.png"),
    )
    expected = serialize_result(parts)

    save_result(parts, "-", stdout=stdout)
    save_result(parts, str(destination), stdout=stdout)

    assert stdout.getvalue() == expected
    assert destination.read_bytes() == expected.encode("utf-8")
    assert destination.stat().st_mode & 0o777 == 0o640


def test_result_saving_creates_an_empty_selected_file(tmp_path: Path) -> None:
    destination = tmp_path / "empty.txt"

    save_result((), str(destination), stdout=StringIO())

    assert destination.read_bytes() == b""


@pytest.mark.parametrize("kind", ("directory", "symlink", "missing-parent"))
def test_result_saving_rejects_invalid_file_destinations(
    tmp_path: Path,
    kind: str,
) -> None:
    if kind == "directory":
        destination = tmp_path
        message = "result destination is not a regular file"
    elif kind == "symlink":
        target = tmp_path / "target.txt"
        target.write_text("existing", encoding="utf-8")
        destination = tmp_path / "result.txt"
        destination.symlink_to(target)
        message = "result destination is not a regular file"
    else:
        destination = tmp_path / "missing" / "result.txt"
        message = "result destination parent does not exist"

    with pytest.raises(ValueError, match=message):
        save_result((TextPart("new"),), str(destination), stdout=StringIO())


def test_result_saving_reports_file_write_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "result.txt"

    def fail_write(_path: Path, _content: str) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(result_saving, "atomic_write_text", fail_write)

    with pytest.raises(OSError, match="could not save result.*write failed"):
        save_result((TextPart("result"),), str(destination), stdout=StringIO())

    assert tuple(tmp_path.iterdir()) == ()

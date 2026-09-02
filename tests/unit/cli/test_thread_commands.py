from __future__ import annotations

import pytest

from toolang.base.types.model import ModelOverride
from toolang.cli.toolang.commands import thread


def test_rerun_model_option_uses_the_shared_model_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert thread._rerun_model_override(
        model_body="effort=high",
        default_options=None,
    ) == ModelOverride(effort="high")
    assert capsys.readouterr().err == ""

    assert thread._rerun_model_override(
        model_body=None,
        default_options=["model=test/model effort=4096"],
    ) == ModelOverride(identity="test/model", effort=4096)
    assert capsys.readouterr().err.count("deprecated") == 1


def test_rerun_model_option_rejects_conflicts_and_model_removal() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        thread._rerun_model_override(
            model_body="test/model",
            default_options=["model=test/model"],
        )
    with pytest.raises(ValueError, match="does not accept unset"):
        thread._rerun_model_override(
            model_body="unset",
            default_options=None,
        )
    with pytest.raises(ValueError, match="was removed"):
        thread._rerun_model_override(
            model_body="none",
            default_options=None,
        )

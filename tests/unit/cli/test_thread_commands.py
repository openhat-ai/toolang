from __future__ import annotations

import pytest

from toolang.base.types.model import ModelOverride
from toolang.cli.toolang.commands import thread


def test_rerun_model_option_uses_the_shared_model_body() -> None:
    assert thread._rerun_model_override(
        model_body="test/model effort=4096",
    ) == ModelOverride(identity="test/model", effort=4096)
    assert thread._rerun_model_override(model_body="effort=high") == ModelOverride(
        effort="high"
    )
    assert thread._rerun_model_override(model_body=None) is None


def test_rerun_model_option_rejects_model_removal() -> None:
    with pytest.raises(ValueError, match="does not accept unset"):
        thread._rerun_model_override(model_body="unset")
    with pytest.raises(ValueError, match="was removed"):
        thread._rerun_model_override(model_body="none")

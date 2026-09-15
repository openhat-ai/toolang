"""A terminal model step must produce visible output."""

from __future__ import annotations

import pytest

from toolang.base.types.message import Message
from toolang.base.types.run import ModelCallResult, ModelUsage
from toolang.execution.errors import EmptyModelOutput
from toolang.execution.executor.runs.agic import _require_visible_output


def test_visible_message_is_accepted() -> None:
    _require_visible_output(ModelCallResult(message=Message.assistant("done")))


def test_reasoning_exhaustion_is_reported() -> None:
    result = ModelCallResult(
        message=None,
        usage=ModelUsage(
            input_tokens=100,
            output_tokens=4096,
            output_visible_tokens=0,
            output_reasoning_tokens=4096,
        ),
    )

    with pytest.raises(EmptyModelOutput, match="reasoning consumed"):
        _require_visible_output(result)


def test_empty_response_is_rejected() -> None:
    with pytest.raises(EmptyModelOutput, match="no visible output"):
        _require_visible_output(ModelCallResult(message=None))

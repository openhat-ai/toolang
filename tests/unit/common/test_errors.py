from toolang.base.errors import ToolangError as BaseToolangError
from toolang.common.errors import ToolangError as CommonToolangError


def test_common_errors_reexport_base_error() -> None:
    assert CommonToolangError is BaseToolangError

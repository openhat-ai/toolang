from toolang.base.error import ToolangError as BaseToolangError
from toolang.common.error import ToolangError as CommonToolangError


def test_common_error_reexports_base_error() -> None:
    assert CommonToolangError is BaseToolangError

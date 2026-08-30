import pytest

from toolang.cli.common.policy import (
    resolve_binding_overrides,
    resolve_ceiling_overrides,
    resolve_limit_overrides,
)


def test_policy_options_overlay_environment_by_field() -> None:
    environ = {
        "TOOLANG_ALLOW_MODELS": "gateway/*",
        "TOOLANG_DEFAULT_MODEL": "gateway/chat",
        "TOOLANG_LIMIT_TOKENS": "100",
    }

    assert resolve_ceiling_overrides(
        environ,
        ("models=local/*", "models=test/*", "tools=none"),
    ) == {
        "models": ("local/*", "test/*"),
        "tools": (),
    }
    assert resolve_binding_overrides(environ, ("model=none",)) == {"model": None}
    assert resolve_limit_overrides(
        environ,
        ("tokens=500", "agic_tool_calls=none", "time=60"),
    ) == {
        "tokens": 500,
        "agic_tool_calls": None,
        "time": 60,
    }


def test_policy_overrides_preserve_absent_empty_and_unrestricted() -> None:
    assert resolve_ceiling_overrides({}, ()) == {}
    assert resolve_ceiling_overrides({}, ("tools=none",)) == {"tools": ()}
    assert resolve_ceiling_overrides({}, ("tools=all",)) == {"tools": None}
    assert resolve_binding_overrides({}, ("model=none",)) == {"model": None}
    assert resolve_limit_overrides({}, ("time=none",)) == {"time": None}


@pytest.mark.parametrize(
    ("resolver", "value", "message"),
    [
        (resolve_ceiling_overrides, "channels=web", "unknown allow field"),
        (resolve_ceiling_overrides, "models=none", "cannot combine"),
        (resolve_ceiling_overrides, "models=all,openai/*", "cannot mix"),
        (resolve_binding_overrides, "model=a", "duplicate default field"),
        (resolve_limit_overrides, "tokens=1", "duplicate run limit"),
        (resolve_limit_overrides, "unknown=1", "unknown run limit"),
        (resolve_limit_overrides, "tokens=-1", "non-negative integer"),
        (resolve_limit_overrides, "cost=NaN", "non-negative decimal"),
        (resolve_limit_overrides, "time", "field=value"),
    ],
)
def test_policy_options_reject_invalid_values(
    resolver,
    value: str,
    message: str,
) -> None:
    values = (
        (value, "models=test/*")
        if resolver is resolve_ceiling_overrides and value == "models=none"
        else (value, value)
        if "duplicate" in message
        else (value,)
    )
    with pytest.raises(ValueError, match=message):
        resolver({}, values)

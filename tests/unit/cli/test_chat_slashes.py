from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest
from prompt_toolkit.utils import get_cwidth

from toolang.base.types.message import TextPart
from toolang.base.types.model import ModelParameters, ModelRequest, ReasoningParameters
from toolang.base.types.policy import AgentCeiling, RunPolicy
from toolang.common.errors import ToolangError
from toolang.execution.policy import apply_session_setting
from toolang.execution.schemas import RunRequest, RunnableRequest
from toolang.execution.types import (
    AllowOverride,
    ModelOverride,
    RunOverride,
    SessionSetting,
)
from toolang.lang.input import RunnableInputRaw
from toolang.cli.toolang.commands.chat import shortcuts, slashes
from toolang.cli.toolang.commands.chat.base import ChatResult, QueuedCall
from toolang.cli.toolang.commands.chat.input import QuickCommand
from toolang.cli.toolang.commands.chat.policy import (
    reconcile_session_model,
    run_override_help_lines,
    session_model_reconciliation_required,
    setting_slash_usage,
)
from toolang.cli.toolang.commands.chat.presenter import ChatRunPresenter


class _Client:
    def __init__(self) -> None:
        self.models: tuple[dict[str, Any], ...] = (
            {
                "ref": "openai/gpt-5",
                "name": "GPT-5",
                "provider": "openai",
                "parameters": {
                    "reasoning": {
                        "effort": ["low", "high"],
                        "applicable": True,
                    }
                },
                "price": {"input": "1.25", "output": "10.00"},
            },
            {
                "ref": "openrouter/openai/o3",
                "name": "OpenRouter o3",
                "provider": "openrouter",
                "parameters": {
                    "reasoning": {
                        "effort": ["low", "medium", "high"],
                        "applicable": True,
                    }
                },
                "price": {"input": "0.30", "output": "0.88"},
            },
        )
        self.tools: tuple[dict[str, Any], ...] = (
            {
                "ref": "shell/run",
                "toolset": "shell",
                "plugin": "shell",
                "description": "Run a shell command.",
            },
            {
                "ref": "filesystem/read",
                "toolset": "filesystem",
                "plugin": "filesystem",
                "description": "Read a file.",
            },
        )
        self.caps: tuple[dict[str, Any], ...] = (
            {
                "identity": "skill/reviewer",
                "kind": "skill",
                "scope": "home",
                "form": "authored",
                "description": "Review code.",
                "summary": "Review code.",
            },
            {
                "identity": "prompt/summary",
                "kind": "prompt",
                "scope": "root",
                "form": "configured",
                "description": "Summarize input.",
                "summary": "Summarize input.",
            },
        )
        self.runnables: dict[str, Mapping[str, Any]] = {
            "agic": {"default": "chat", "items": [{"name": "chat"}]},
            "flow": {"default": None, "items": [{"name": "review"}]},
            "runnable": {
                "default": "agic:chat",
                "items": [
                    {"kind": "agic", "name": "chat"},
                    {"kind": "flow", "name": "review"},
                ],
            },
        }
        self.error: Exception | None = None
        self.applied: list[RunOverride] = []
        self.resource_calls: list[tuple[str, str | None, Sequence[str] | None]] = []

    def list_models(
        self,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        self._raise_error()
        self.resource_calls.append(("models", None, queries))
        items = list(_select(self.models, queries))
        return {
            "default": (
                "openai/gpt-5"
                if any(item["ref"] == "openai/gpt-5" for item in items)
                else cast(str, items[0]["ref"])
                if items
                else None
            ),
            "items": items,
        }

    def list_tools(
        self,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        self._raise_error()
        self.resource_calls.append(("tools", None, queries))
        return {"items": list(_select(self.tools, queries))}

    def list_caps(
        self,
        kind: str | None = None,
        queries: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        self._raise_error()
        self.resource_calls.append(("caps", kind, queries))
        items = tuple(
            item for item in self.caps if kind is None or item["kind"] == kind
        )
        return {"items": list(_select(items, queries))}

    def list_runnables(self, kind: str) -> Mapping[str, Any]:
        self._raise_error()
        return self.runnables[kind]

    def apply_setting(
        self,
        setting: SessionSetting,
        update: RunOverride,
        *,
        allowed_model_refs: Sequence[str] | None = None,
        default_model_ref: str | None = None,
    ) -> SessionSetting:
        self._raise_error()
        self.applied.append(update)
        candidate = apply_session_setting(_surface(), setting, update)
        if not session_model_reconciliation_required(update):
            return candidate
        if allowed_model_refs is not None:
            return reconcile_session_model(
                candidate,
                update,
                allowed_refs=allowed_model_refs,
                default_ref=default_model_ref,
            )
        payload = self.list_models(candidate.allow.models)
        return reconcile_session_model(
            candidate,
            update,
            allowed_refs=tuple(
                cast(str, item["ref"])
                for item in cast(
                    list[dict[str, object]],
                    payload["items"],
                )
            ),
            default_ref=cast(str | None, payload["default"]),
        )

    def get_result(
        self,
        run_id: str | None,
        *,
        thread_id: str | None,
    ) -> ChatResult:
        del thread_id
        return ChatResult(
            run_id=run_id or "run_saved",
            output=(TextPart("saved result"),),
        )

    def _raise_error(self) -> None:
        if self.error is not None:
            raise self.error


def _select(
    items: Sequence[dict[str, Any]],
    queries: Sequence[str] | None,
) -> tuple[dict[str, Any], ...]:
    if queries is None:
        return tuple(items)
    if not queries:
        return ()
    query = " ".join(queries)
    prefix = query.split("*", 1)[0]
    return tuple(item for item in items if _item_identity(item).startswith(prefix))


def _item_identity(item: Mapping[str, Any]) -> str:
    return cast(str, item.get("ref") or item.get("identity") or "")


def _surface() -> SessionSetting:
    return SessionSetting(
        model=ModelRequest("openai/gpt-5"),
        runnable="agic:chat",
    )


def _request(source: str) -> RunRequest:
    return RunRequest(
        thread_id="thread_1",
        request_id=f"request_{source}",
        runnable=RunnableRequest("agic:chat", RunnableInputRaw(_=source)),
        model=ModelRequest("openai/gpt-5"),
        policy=RunPolicy(),
    )


class _App:
    def __init__(self) -> None:
        self.client = _Client()
        self.setting = _surface()
        self.queue: list[QueuedCall] = []
        self.active_run: str | None = None
        self.replaced = ""
        self.steers: list[str] = []
        self.exited = False
        self.status_refreshes = 0
        self.live_blocks: list[Any] = []
        self.presenter = ChatRunPresenter()

    def get_client(self) -> Any:
        return self.client

    def get_setting(self) -> SessionSetting:
        return self.setting

    def set_setting(self, setting: SessionSetting) -> None:
        self.setting = setting

    def get_queue(self) -> list[QueuedCall]:
        return self.queue

    def get_active_run(self) -> str | None:
        return self.active_run

    def get_thread_id(self) -> str | None:
        return "thread_1"

    def set_active_run(self, run_id: str | None) -> None:
        self.active_run = run_id

    def get_live_blocks(self) -> Any:
        return self.live_blocks

    def get_presenter(self) -> ChatRunPresenter:
        return self.presenter

    def ensure_thread_id(self) -> str:
        return "thread_1"

    def refresh_status(self) -> None:
        self.status_refreshes += 1

    def replace_input(self, text: str) -> None:
        self.replaced = text

    def request_steer(self, message: str) -> None:
        self.steers.append(message)

    def request_exit(self) -> None:
        self.exited = True

    def finalize_block(self, block: Any) -> None:
        self.live_blocks.remove(block)

    def finish_run(self) -> None:
        self.active_run = None


def _outcome(value: slashes.SlashOutcome | None) -> slashes.SlashOutcome:
    assert value is not None
    return value


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "Enter a command after / · See /? for help"),
        ("missing", "Unknown command /missing · See /? for help"),
    ],
)
def test_unrecognized_slash_names_return_status_guidance(
    name: str,
    message: str,
) -> None:
    assert not slashes.is_registered(name)
    assert slashes.unrecognized_diagnostic(name) == message


def test_command_with_unexpected_body_returns_error() -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand("help", "unexpected")))

    assert result.kind == "error"
    assert slashes.outcome_lines(result) == (
        "Error: /help does not accept an argument",
    )


def test_quick_help_and_exit_are_declarative_commands() -> None:
    app = _App()

    help_result = _outcome(slashes.handle(app, QuickCommand("?")))
    exit_result = slashes.handle(app, QuickCommand("quit"))

    help_lines = slashes.outcome_lines(help_result)
    assert help_result.kind == "result"
    assert help_lines[:8] == (
        "Slash commands act immediately.",
        "Setting commands change defaults for future runs in this Chat session.",
        "effort=auto inherits model or provider reasoning defaults.",
        "",
        "Submit one slash command by itself; it cannot be combined with run input.",
        "See :? to change settings for one run only.",
        "",
        "Available commands:",
    )
    assert help_lines[8].startswith("/model [MODEL] [effort=VALUE]")
    assert "Chat commands" not in help_lines
    assert any(line.startswith("/models [QUERY]") for line in help_lines)
    assert any(line.startswith("/tools [QUERY]") for line in help_lines)
    assert any(line.startswith("/caps [QUERY]") for line in help_lines)
    assert any(line.startswith("/keys") for line in help_lines)
    assert exit_result is None
    assert app.exited is True


def test_run_override_help_explains_lifetime_and_uses_shared_forms() -> None:
    lines = slashes.outcome_lines(slashes.run_override_help())
    expected_forms = (
        ":model MODEL",
        ":model unset",
        ":model effort=VALUE",
        ":agic AGIC",
        ":flow FLOW",
        ":runnable RUNNABLE",
        ":allow FIELD=QUERY...",
        ":limit FIELD=VALUE...",
    )

    assert lines[:8] == (
        "Run overrides change settings for this run only.",
        "Session defaults stay unchanged.",
        "effort=auto inherits model or provider reasoning defaults.",
        "",
        "Put one or more override lines first.",
        "Include the run input in the same submission.",
        "",
        "Available overrides:",
    )
    assert lines[8:] == run_override_help_lines() == expected_forms
    assert "Run overrides" not in lines


def test_keys_help_uses_the_binding_metadata_without_a_title() -> None:
    result = _outcome(slashes.handle(_App(), QuickCommand("keys")))
    lines = slashes.outcome_lines(result)

    assert result.kind == "result"
    assert lines[:4] == (
        "These shortcuts control interactive Chat.",
        "Standard cursor and text-editing keys are not listed.",
        "",
        "Available shortcuts:",
    )
    assert lines[4:] == shortcuts.help_lines()
    assert any(line.startswith("Esc  ") for line in lines)
    assert any(line.startswith("Esc Esc  ") for line in lines)
    assert "Chat shortcuts" not in lines


def test_shared_setting_usage_supplies_registered_slash_commands() -> None:
    help_lines = slashes.outcome_lines(
        _outcome(slashes.handle(_App(), QuickCommand("?")))
    )

    for name in ("model", "agic", "flow", "runnable", "allow", "limit"):
        assert any(line.startswith(setting_slash_usage(name)) for line in help_lines)


@pytest.mark.parametrize(
    ("name", "usage"),
    [
        ("model", "/model [MODEL] [effort=VALUE]"),
        ("agic", "/agic AGIC"),
        ("flow", "/flow FLOW"),
        ("runnable", "/runnable RUNNABLE"),
        ("allow", "/allow FIELD=QUERY..."),
        ("limit", "/limit FIELD=VALUE..."),
        ("steer", "/steer MESSAGE"),
    ],
)
def test_required_command_without_body_returns_usage(name: str, usage: str) -> None:
    app = _App()
    app.client.error = AssertionError("client must not be called")

    result = _outcome(slashes.handle(app, QuickCommand(name)))

    assert result.kind == "usage"
    assert slashes.outcome_lines(result) == (f"Usage: {usage}",)
    assert app.setting == _surface()
    assert app.status_refreshes == 0


def test_model_identity_and_effort_update_independently() -> None:
    app = _App()

    selected = _outcome(
        slashes.handle(app, QuickCommand("model", "openai/gpt-5 effort=high"))
    )
    assert app.setting.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(effort="high")),
    )
    assert slashes.outcome_lines(selected) == ("Model set to openai/gpt-5 · high",)

    slashes.handle(app, QuickCommand("model", "effort=low"))
    assert app.setting.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(effort="low")),
    )

    slashes.handle(app, QuickCommand("model", "effort=4096"))
    assert app.setting.model == ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(budget_tokens=4096)),
    )

    automatic = _outcome(slashes.handle(app, QuickCommand("model", "effort=auto")))
    assert app.setting.model == ModelRequest("openai/gpt-5")
    assert slashes.outcome_lines(automatic) == ("Model set to openai/gpt-5 · auto",)
    assert app.status_refreshes == 4


def test_model_rejects_known_unsupported_effort_without_changing_session() -> None:
    app = _App()
    previous = app.setting

    result = _outcome(slashes.handle(app, QuickCommand("model", "effort=medium")))

    assert result.kind == "error"
    assert slashes.outcome_lines(result) == (
        "Error: model openai/gpt-5 does not advertise reasoning effort "
        "'medium' (allowed: low, high)",
    )
    assert app.setting == previous
    assert app.status_refreshes == 0


def test_model_defers_effort_validation_when_metadata_is_unavailable() -> None:
    app = _App()
    app.client.models = (
        {
            "ref": "openai/gpt-5",
            "name": "GPT-5",
            "provider": "openai",
            "price": {"input": "1.25", "output": "10.00"},
        },
    )

    result = _outcome(slashes.handle(app, QuickCommand("model", "effort=medium")))

    assert result.kind == "success"
    assert slashes.outcome_lines(result) == ("Model set to openai/gpt-5 · medium",)


def test_model_none_is_rejected_without_loading_resources() -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand("model", "none")))

    assert result.kind == "error"
    assert slashes.outcome_lines(result) == (
        "Error: model identity 'none' was removed; use :model unset for a "
        "model-free run",
    )
    assert app.setting.model == ModelRequest("openai/gpt-5")
    assert app.client.resource_calls == []
    assert app.status_refreshes == 0


def test_model_unset_is_rejected_as_a_session_command() -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand("model", "unset")))

    assert result.kind == "error"
    assert slashes.outcome_lines(result) == (
        "Error: /model unset is not a session setting; use :model unset for one run",
    )
    assert app.setting.model == ModelRequest("openai/gpt-5")
    assert app.status_refreshes == 0


def test_runnable_argument_updates_setting_and_refreshes_status() -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand("flow", "review")))

    assert result.kind == "success"
    assert slashes.outcome_lines(result) == ("Runnable set to flow:review",)
    assert app.setting.runnable == "flow:review"
    assert app.status_refreshes == 1


def test_allow_and_limit_report_effective_updates_and_refresh_status() -> None:
    app = _App()

    allowed = _outcome(
        slashes.handle(
            app,
            QuickCommand("allow", "models=openai/* tools=shell/*"),
        )
    )
    limited = _outcome(
        slashes.handle(app, QuickCommand("limit", "tokens=2000 time=none"))
    )

    assert slashes.outcome_lines(allowed) == ("Allowed 1 model, 1 tool",)
    assert slashes.outcome_lines(limited) == ("Limits set to tokens=2000, time=none",)
    assert app.setting.allow.models == ("openai/*",)
    assert app.setting.allow.tools == ("shell/*",)
    assert app.setting.limits.tokens == 2000
    assert app.setting.limits.time is None
    assert app.status_refreshes == 2
    assert app.client.resource_calls == [
        ("models", None, ("openai/*",)),
        ("tools", None, ("shell/*",)),
    ]


def test_allow_selects_a_fallback_for_an_excluded_model_and_reports_it() -> None:
    app = _App()
    app.setting = SessionSetting(
        model=ModelRequest(
            "openai/gpt-5",
            ModelParameters(ReasoningParameters(effort="high")),
        ),
        runnable="agic:chat",
    )

    result = _outcome(slashes.handle(app, QuickCommand("allow", "models=openrouter/*")))

    assert app.setting.model == ModelRequest("openrouter/openai/o3")
    assert app.setting.allow.models == ("openrouter/*",)
    assert slashes.outcome_lines(result) == (
        "Allowed 1 model",
        "Model changed: openai/gpt-5 -> openrouter/openai/o3 because "
        "openai/gpt-5 is outside allow.models",
    )
    assert app.status_refreshes == 1
    assert app.client.resource_calls == [
        ("models", None, ("openrouter/*",)),
    ]


def test_allow_preserves_parameters_for_an_allowed_model() -> None:
    app = _App()
    selected = ModelRequest(
        "openai/gpt-5",
        ModelParameters(ReasoningParameters(effort="high")),
    )
    app.setting = SessionSetting(model=selected, runnable="agic:chat")

    result = _outcome(slashes.handle(app, QuickCommand("allow", "models=openai/*")))

    assert app.setting.model == selected
    assert slashes.outcome_lines(result) == ("Allowed 1 model",)
    assert app.status_refreshes == 1


def test_model_reconciliation_prefers_the_available_configured_default() -> None:
    update = RunOverride(allow=(AllowOverride("models", ("provider/*",)),))
    setting = SessionSetting(
        model=ModelRequest(
            "excluded/model",
            ModelParameters(ReasoningParameters(effort="high")),
        ),
        runnable="agic:chat",
    )

    preferred = reconcile_session_model(
        setting,
        update,
        allowed_refs=("provider/first", "provider/default"),
        default_ref="provider/default",
    )
    first = reconcile_session_model(
        setting,
        update,
        allowed_refs=("provider/first", "provider/second"),
        default_ref="excluded/default",
    )
    cleared_preference = reconcile_session_model(
        setting,
        RunOverride(model=ModelOverride(identity="unset")),
        allowed_refs=("provider/first", "provider/default"),
        default_ref="provider/default",
    )
    query_relative_default = reconcile_session_model(
        setting,
        RunOverride(model=ModelOverride(identity="default")),
        allowed_refs=("provider/first", "provider/default"),
        default_ref="provider/default",
    )

    assert preferred.model == ModelRequest("provider/default")
    assert first.model == ModelRequest("provider/first")
    assert cleared_preference.model == ModelRequest("provider/first")
    assert query_relative_default.model == ModelRequest("provider/default")


def test_allow_none_clears_the_model_and_reports_an_empty_collection() -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand("allow", "models=none")))

    assert app.setting.model is None
    assert app.setting.allow.models == ()
    assert slashes.outcome_lines(result) == (
        "Allowed 0 models",
        "Model cleared: openai/gpt-5 is outside allow.models; no models available",
    )
    assert app.client.resource_calls == [("models", None, ())]
    assert app.status_refreshes == 1


def test_explicit_model_outside_allow_is_atomic() -> None:
    app = _App()
    app.setting = SessionSetting(
        model=None,
        runnable="agic:chat",
        allow=AgentCeiling(models=("openrouter/*",)),
    )
    previous = app.setting

    result = _outcome(slashes.handle(app, QuickCommand("model", "openai/gpt-5")))

    assert result.kind == "error"
    assert slashes.outcome_lines(result) == (
        "Error: model is outside session allow.models: openai/gpt-5",
    )
    assert app.setting == previous
    assert app.status_refreshes == 0


def test_default_model_uses_the_query_relative_fallback() -> None:
    app = _App()
    app.setting = SessionSetting(
        model=None,
        runnable="agic:chat",
        allow=AgentCeiling(models=("openrouter/*",)),
    )
    result = _outcome(slashes.handle(app, QuickCommand("model", "default")))

    assert result.kind == "success"
    assert slashes.outcome_lines(result) == (
        "Model set to openrouter/openai/o3 · auto",
    )
    assert app.setting.model == ModelRequest("openrouter/openai/o3")
    assert app.status_refreshes == 1


@pytest.mark.parametrize(
    ("command", "query", "summary", "header"),
    [
        ("models", "openrouter/*", "Found 1 model", "MODEL"),
        ("tools", "filesystem/*", "Found 1 tool", "TOOL"),
        (
            "caps",
            "skill/*[scope=home;description~=code review]",
            "Found 1 cap",
            "CAP",
        ),
    ],
)
def test_resource_commands_forward_the_whole_query_and_return_tables(
    command: str,
    query: str,
    summary: str,
    header: str,
) -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand(command, query)))

    assert result.kind == "result"
    assert isinstance(result.content, slashes.SlashTable)
    assert result.content.summary == summary
    assert result.content.headers[0] == header
    assert app.client.resource_calls[-1] == (command, None, (query,))


def test_models_table_formats_prices_efforts_and_default_marker() -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand("models")))

    assert slashes.outcome_lines(result) == (
        "Found 2 models",
        "  MODEL                 PRICE ($/1M)     EFFORT",
        "  ────────────────────  ───────────────  ─────────────────",
        "  openai/gpt-5*         $ 1.25 / $10.00  low, high",
        "  openrouter/openai/o3  $ 0.30 / $ 0.88  low, medium, high",
    )


def test_models_table_distinguishes_zero_and_missing_prices() -> None:
    app = _App()
    app.client.models = (
        {
            "ref": "openai/gpt-5",
            "name": "GPT-5",
            "provider": "openai",
            "parameters": {"reasoning": {"effort": [], "applicable": False}},
            "price": {"input": 0, "output": None},
        },
    )

    result = _outcome(slashes.handle(app, QuickCommand("models")))

    assert isinstance(result.content, slashes.SlashTable)
    assert result.content.rows == (("openai/gpt-5*", "$ 0.00 /      -", "-"),)


def test_resource_tables_fit_unicode_cells_without_wrapping() -> None:
    outcome = slashes.SlashOutcome(
        "result",
        slashes.SlashTable(
            "Found 1 model",
            ("MODEL", "PRICE ($/1M)", "EFFORT"),
            (("提供者/非常长的模型*", "$ 1.25 / $10.00", "low, medium, high"),),
            shrink_order=(2, 0, 1),
            protected_suffixes=("*", None, None),
        ),
    )

    lines = slashes.outcome_lines(outcome, width=38)

    assert all("\n" not in line and get_cwidth(line) <= 38 for line in lines[1:])
    assert lines[1].strip().endswith("EFFORT")
    assert lines[-1].split("  ", 2)[1].endswith("*")
    assert "…" in lines[-1]


def test_tools_table_hides_private_toolsets_after_query() -> None:
    app = _App()
    app.client.tools += (
        {
            "ref": "_internal/read",
            "toolset": "_internal",
            "plugin": "internal",
            "description": "Read internal state.",
        },
        {
            "ref": "filesystem/_private",
            "toolset": "filesystem",
            "plugin": "filesystem",
            "description": "A public tool with a private-looking name.",
        },
    )

    result = _outcome(slashes.handle(app, QuickCommand("tools")))

    assert isinstance(result.content, slashes.SlashTable)
    assert result.content.summary == "Found 3 tools"
    assert all(not row[0].startswith("_internal/") for row in result.content.rows)
    assert any(row[0] == "filesystem/_private" for row in result.content.rows)

    hidden_only = _outcome(slashes.handle(app, QuickCommand("tools", "_internal/*")))
    assert slashes.outcome_lines(hidden_only) == ("No tools found",)


def test_caps_table_uses_form_and_display_summary() -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand("caps", "skill/*")))

    assert isinstance(result.content, slashes.SlashTable)
    assert result.content.headers == ("CAP", "SCOPE", "FORM", "DESCRIPTION")
    assert result.content.rows == (
        ("skill/reviewer", "home", "authored", "Review code."),
    )


def test_resource_command_without_matches_is_a_result() -> None:
    app = _App()

    result = _outcome(slashes.handle(app, QuickCommand("models", "missing/*")))

    assert result.kind == "result"
    assert slashes.outcome_lines(result) == ("No models found",)


def test_quick_show_loads_an_explicit_or_latest_durable_result() -> None:
    app = _App()

    explicit = _outcome(slashes.handle(app, QuickCommand("show", "run_saved")))
    latest = _outcome(slashes.handle(app, QuickCommand("show")))

    assert isinstance(explicit.content, slashes.SlashRunResult)
    assert isinstance(latest.content, slashes.SlashRunResult)
    assert explicit.content.result == ChatResult(
        run_id="run_saved",
        output=(TextPart("saved result"),),
    )
    assert latest.content.result == explicit.content.result


def test_quick_queue_edits_and_steers_numbered_items() -> None:
    app = _App()
    app.queue[:] = [
        QueuedCall("first", _request("first")),
        QueuedCall("second", _request("second")),
    ]

    edited = _outcome(slashes.handle(app, QuickCommand("queue", "edit 2")))
    app.active_run = "run_abc"
    steered = _outcome(slashes.handle(app, QuickCommand("queue", "steer 1")))

    assert slashes.outcome_lines(edited) == ("Moved queue item 2 to input",)
    assert slashes.outcome_lines(steered) == ("Accepted queue item 1 as steer",)
    assert app.replaced == "second"
    assert app.steers == ["first"]
    assert app.queue == []


def test_quick_steer_reports_acceptance_or_missing_active_run() -> None:
    app = _App()

    missing = _outcome(slashes.handle(app, QuickCommand("steer", "revise")))
    app.active_run = "run_abc"
    accepted = _outcome(slashes.handle(app, QuickCommand("steer", "revise")))

    assert missing.kind == "error"
    assert slashes.outcome_lines(missing) == ("Error: No active run to steer",)
    assert slashes.outcome_lines(accepted) == ("Steer accepted",)
    assert app.steers == ["revise"]


def test_quick_client_errors_return_scrollback_errors() -> None:
    app = _App()
    app.client.error = ToolangError("unavailable")

    result = _outcome(slashes.handle(app, QuickCommand("model", "openai/gpt-5")))

    assert result.kind == "error"
    assert slashes.outcome_lines(result) == ("Error: unavailable",)
    assert app.status_refreshes == 0

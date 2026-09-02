"""Resident remote Chat session composition and recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
import json

import httpx
import pytest
from pydantic import TypeAdapter

from toolang.base.types.message import TextPart
from toolang.base.types.model import (
    ModelParameters,
    ModelRequest,
    ReasoningParameters,
)
from toolang.cli.toolang.commands.chat import remote
from toolang.cli.toolang.commands.chat.base import (
    ChatExecutorMetadata,
    RunAccepted,
    RunBlocked,
    RunDisconnected,
    RunRecovered,
)
from toolang.execution.events import RunBegin, RunEnd, RunEvent, run_event_to_data
from toolang.execution.schemas import (
    RunControlRefData,
    RunDetail,
    ThreadControlRefData,
    ThreadInfo,
    ThreadPeerInfo,
)
from toolang.execution.types import (
    AllowOverride,
    ControlRef,
    Local,
    ModelOverride,
    LimitOverride,
    RunOverride,
)
from toolang.lang.input import RunnableInputRaw


_CONTAINER_ID = "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"
_HOST_DESCRIPTION = "macOS 27.0 arm64"


def _run_defaults() -> dict[str, object]:
    return {
        "model": {"ref": "test/model", "parameters": {}},
        "runnable": "agic:chat",
        "policy": {"allow": [], "limits": {}},
    }


def _models() -> dict[str, object]:
    return {
        "default": "test/model",
        "items": [
            {
                "ref": "test/model",
                "name": "test",
                "provider": "test",
                "parameters": {"reasoning": {"effort": []}},
            }
        ],
    }


def test_remote_run_defaults_allow_an_absent_runnable() -> None:
    setting = remote._session_setting(
        {
            "model": None,
            "runnable": None,
            "policy": {"allow": [], "limits": {}},
        }
    )

    assert setting.model is None
    assert setting.runnable is None


def test_remote_chat_initializes_a_fallback_when_run_defaults_have_no_model() -> None:
    model_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal model_requests
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(
                200,
                json={
                    "model": None,
                    "runnable": "agic:chat",
                    "policy": {"allow": [], "limits": {}},
                },
            )
        if request.url.path == "/api/v1/models":
            model_requests += 1
            return httpx.Response(200, json=_models())
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    try:
        model = session.initial_setting().model
        assert model is not None
        assert model.ref == "test/model"
        assert model_requests == 1
    finally:
        session.close()


@pytest.mark.parametrize(
    "payload",
    (
        {"default": None, "items": _models()["items"]},
        {"default": "missing/model", "items": _models()["items"]},
        {"default": "test/model", "items": []},
    ),
)
def test_remote_chat_rejects_a_model_default_outside_items(
    payload: dict[str, object],
) -> None:
    with pytest.raises(remote.RemoteChatError, match="models returned invalid default"):
        remote._catalog_payload(payload, operation="models", item_kind="model")


def test_remote_run_defaults_preserve_typed_model_parameters() -> None:
    setting = remote._session_setting(
        {
            "model": {
                "ref": "test/model",
                "parameters": {"reasoning": {"effort": "high"}},
            },
            "runnable": "agic:chat",
            "policy": {"allow": [], "limits": {}},
        }
    )

    assert setting.model == ModelRequest(
        "test/model",
        ModelParameters(reasoning=ReasoningParameters(effort="high")),
    )


class _Bytes(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def _profile(
    *,
    driver: str = "host",
    selector: str | None = None,
    instance: str | None = None,
) -> dict[str, object]:
    return {
        "runtime": {
            "version": "v0.3.9",
            "sandbox": {
                "driver": driver,
                "selector": selector or driver,
                "instance": instance,
                "description": (None if driver == "docker" else _HOST_DESCRIPTION),
            },
        }
    }


def _profile_without_description(
    *,
    driver: str = "host",
    selector: str | None = None,
    instance: str | None = None,
) -> dict[str, object]:
    return {
        "runtime": {
            "version": "v0.3.9",
            "sandbox": {
                "driver": driver,
                "selector": selector or driver,
                "instance": instance,
            },
        }
    }


def _thread(thread_id: str = "term_remote") -> ThreadInfo:
    return ThreadInfo(
        id=thread_id,
        title="chat",
        created_at="2026-08-25T00:00:00Z",
        updated_at="2026-08-25T00:00:00Z",
        origin="chat",
        channel="terminal",
        status="idle",
        peer=ThreadPeerInfo(),
        created_by=ThreadControlRefData(thread=thread_id, index=0),
        head=ThreadControlRefData(thread=thread_id, index=0),
        run_count=0,
        latest_run=None,
        active_run=None,
    )


def _detail(
    run_id: str = "run_remote",
    *,
    status: str = "succeeded",
    output: Local | None = None,
) -> RunDetail:
    terminal = status not in {"pending", "running"}
    return RunDetail(
        id=run_id,
        parent=None,
        thread_id="term_remote",
        root_run_id=run_id,
        runnable_kind="agic",
        runnable_name="chat",
        call_kind="top",
        state=RunControlRefData(run=run_id, index=0),
        occurrence=None,
        input_text="hello",
        summary="remote answer",
        status=status,  # type: ignore[arg-type]
        error=None,
        ejected=None,
        created_at="2026-08-25T00:00:00Z",
        started_at="2026-08-25T00:00:00Z",
        finished_at="2026-08-25T00:00:01Z" if terminal else None,
        updated_at=("2026-08-25T00:00:01Z" if terminal else "2026-08-25T00:00:00Z"),
        control=RunControlRefData(run=run_id, index=0),
        output=output,
        controls=[],
        steps=[],
    )


def _begin(run_id: str = "run_remote") -> RunBegin:
    return RunBegin(
        run=run_id,
        control=ControlRef(run_id, 0),
        runnable="agic:chat",
        started_at="2026-08-25T00:00:00Z",
    )


def _end(run_id: str = "run_remote") -> RunEnd:
    return RunEnd(
        run=run_id,
        status="succeeded",
        finished_at="2026-08-25T00:00:01Z",
    )


def _stream(*events: RunEvent, run_id: str = "run_remote") -> httpx.Response:
    chunks = tuple(
        (
            f"event: {event.type}\ndata: {json.dumps(run_event_to_data(event))}\n\n"
        ).encode()
        for event in events
    )
    return httpx.Response(
        200,
        headers={
            "content-type": "text/event-stream",
            "X-Toolang-Run-ID": run_id,
        },
        stream=_Bytes(*chunks),
    )


def _json(value: object) -> object:
    return TypeAdapter(type(value)).dump_python(value, mode="json")


def test_remote_chat_non_run_operations_and_executor_metadata() -> None:
    requests: list[tuple[str, str, object | None]] = []
    result = _detail(
        output=Local.typed("Part[]", (TextPart("remote answer"),), "_"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(
                200,
                json=_profile(
                    driver="docker",
                    selector="docker:python:3.13-slim",
                    instance=_CONTAINER_ID[:12],
                ),
            )
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(200, json=_run_defaults())
        if request.url.path == "/api/v1/models":
            return httpx.Response(
                200,
                json={
                    "default": "test/model",
                    "items": [
                        {
                            "ref": "test/model",
                            "name": "test",
                            "provider": "test",
                            "parameters": {"reasoning": {"effort": []}},
                        }
                    ],
                },
            )
        if request.url.path == "/api/v1/agics":
            return httpx.Response(
                200,
                json={"default": "chat", "items": [{"name": "chat"}]},
            )
        if request.url.path == "/api/v1/flows":
            return httpx.Response(200, json={"default": None, "items": []})
        if request.url.path == "/api/v1/prompt-completions":
            assert request.url.params.get("runnable") == "agic:chat"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "name": "review",
                            "params": [{"name": "focus", "optional": False}],
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/threads" and request.method == "POST":
            return httpx.Response(201, json={"thread": _json(_thread())})
        if request.url.path in {
            "/api/v1/runs/run_remote",
            "/api/v1/threads/term_remote/result",
        }:
            return httpx.Response(200, json=_json(result))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "HTTP://runtime.test:7001/",
        expected_sandbox="docker:python:3.13-slim",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert session.executor_metadata == ChatExecutorMetadata(
            sandbox_selector="docker:python:3.13-slim",
            sandbox_detail="176191c1528b",
            endpoint="http://runtime.test:7001",
            version="v0.3.9",
        )
        assert session.run_client is not None
        assert session.run_client.endpoint == "http://runtime.test:7001"
        assert session.list_models()["default"] == "test/model"
        assert session.list_runnables("agic")["default"] == "chat"
        assert session.list_runnables("runnable") == {
            "default": "agic:chat",
            "items": [{"kind": "agic", "name": "chat"}],
        }
        assert session.list_prompts("agic:chat") == {
            "items": [
                {
                    "name": "review",
                    "params": [{"name": "focus", "optional": False}],
                }
            ]
        }
        assert session.create_thread() == "term_remote"
        setting = session.apply_setting(
            session.initial_setting(),
            RunOverride(
                model=ModelOverride(identity="test/model"),
                allow=(AllowOverride("models", ("test/*",)),),
                limits=(LimitOverride("cost", Decimal("2.50")),),
            ),
        )
        assert session.get_result("run_remote", thread_id=None).output == (
            TextPart("remote answer"),
        )
        assert session.get_result(None, thread_id="term_remote").run_id == (
            "run_remote"
        )
        assert setting.model is not None and setting.model.ref == "test/model"
        assert setting.allow.models == ("test/*",)
        assert setting.limits.cost == Decimal("2.50")
    finally:
        session.close()

    assert not any(path.endswith("/validate") for _method, path, _body in requests)


def test_remote_chat_resource_queries_and_model_reconciliation() -> None:
    queries: list[tuple[str, tuple[str, ...]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = tuple(request.url.params.get_list("query"))
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if path == "/api/v1/runs/defaults":
            return httpx.Response(200, json=_run_defaults())
        if path == "/api/v1/models":
            queries.append(("models", query))
            items = (
                [
                    {
                        "ref": "openrouter/openai/o3",
                        "name": "o3",
                        "provider": "openrouter",
                        "parameters": {"reasoning": {"effort": ["low", "high"]}},
                    }
                ]
                if query == ("openrouter/*",)
                else _models()["items"]
            )
            return httpx.Response(
                200,
                json={
                    "default": (
                        "openrouter/openai/o3"
                        if query == ("openrouter/*",)
                        else "test/model"
                    ),
                    "items": items,
                },
            )
        if path == "/api/v1/tools":
            queries.append(("tools", query))
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "ref": "filesystem/read",
                            "plugin": "filesystem",
                            "description": "Read a file.",
                        }
                    ]
                },
            )
        if path == "/api/v1/caps":
            queries.append(("caps", query))
            return httpx.Response(
                200,
                json={
                    "agent": "alice",
                    "psyches": [],
                    "skills": [
                        {
                            "kind": "skill",
                            "name": "reviewer",
                            "scope": "home",
                            "description": "Review code.",
                        }
                    ],
                    "services": [],
                    "prompts": [],
                    "counts": {
                        "psyches": 0,
                        "skills": 1,
                        "services": 0,
                        "prompts": 0,
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert [
            item["ref"] for item in session.list_models(("openrouter/*",))["items"]
        ] == ["openrouter/openai/o3"]
        assert (
            session.list_models(("openrouter/*",))["default"] == "openrouter/openai/o3"
        )
        assert session.list_models(())["default"] is None
        assert session.list_models(())["items"] == []
        assert session.list_tools(("filesystem/*",))["items"][0]["ref"] == (
            "filesystem/read"
        )
        assert session.list_tools(())["items"] == []
        assert session.list_caps(None, ("skill/*",))["items"] == [
            {
                "identity": "skill/reviewer",
                "kind": "skill",
                "scope": "home",
                "form": None,
                "description": "Review code.",
                "summary": "Review code.",
            }
        ]

        narrowed = session.apply_setting(
            session.initial_setting(),
            RunOverride(allow=(AllowOverride("models", ("openrouter/*",)),)),
        )
        assert narrowed.model is not None
        assert narrowed.model.ref == "openrouter/openai/o3"
        assert narrowed.allow.models == ("openrouter/*",)

        disabled = session.apply_setting(
            session.initial_setting(),
            RunOverride(allow=(AllowOverride("models", ()),)),
        )
        assert disabled.model is None
        assert disabled.allow.models == ()

        with pytest.raises(
            ValueError,
            match="model is outside session allow.models: test/model",
        ):
            session.apply_setting(
                narrowed,
                RunOverride(model=ModelOverride(identity="test/model")),
            )
        assert narrowed.model is not None
        assert narrowed.model.ref == "openrouter/openai/o3"
    finally:
        session.close()

    assert queries == [
        ("models", ("openrouter/*",)),
        ("models", ("openrouter/*",)),
        ("tools", ("filesystem/*",)),
        ("caps", ("skill/*",)),
        ("models", ("openrouter/*",)),
        ("models", ("openrouter/*",)),
    ]


@pytest.mark.parametrize(
    ("profile_payload", "expected_sandbox", "message"),
    (
        (
            {
                "runtime": {
                    "version": "v0.3.9",
                    "sandbox": {"driver": "host", "instance": None},
                }
            },
            "host",
            "invalid sandbox identity",
        ),
        (
            _profile(driver="host", instance="a1b2c3d4e5f6"),
            "host",
            "non-docker sandbox returned an instance ID",
        ),
        (
            _profile(
                driver="docker",
                selector="docker:python:3.13-slim",
                instance="",
            ),
            "docker:python:3.13-slim",
            "sandbox instance is invalid",
        ),
        (
            _profile(driver="host", selector="docker:python:3.13-slim"),
            "host",
            "sandbox selector does not match its driver",
        ),
        (
            _profile(
                driver="docker",
                selector="docker:python:3.12-slim",
                instance="a1b2c3d4e5f6",
            ),
            "docker:python:3.13-slim",
            "sandbox does not match its runtime status",
        ),
    ),
)
def test_remote_chat_rejects_invalid_runtime_identity(
    profile_payload: dict[str, object],
    expected_sandbox: str,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=profile_payload)
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(200, json=_run_defaults())
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with pytest.raises(remote.RemoteChatError, match=message):
        remote.RemoteChatSession(
            "http://runtime.test:7001",
            expected_sandbox=expected_sandbox,
            transport=httpx.MockTransport(handler),
        )


def test_remote_chat_runtime_identity_allows_additive_profile_fields() -> None:
    identity = remote._runtime_identity(
        {
            "runtime": {
                "version": "v0.3.9",
                "future": True,
                "sandbox": {
                    "driver": "docker",
                    "selector": "docker:python:3.13-slim",
                    "instance": _CONTAINER_ID[:12],
                    "description": None,
                    "future": True,
                },
            }
        }
    )

    assert identity.instance == _CONTAINER_ID[:12]
    assert identity.description is None


@pytest.mark.parametrize(
    ("profile_payload", "expected_description"),
    (
        (_profile_without_description(), None),
        (
            {
                "runtime": {
                    "version": "v0.3.9",
                    "sandbox": {
                        "driver": "host",
                        "selector": "host",
                        "instance": None,
                        "description": None,
                    },
                }
            },
            None,
        ),
        (
            _profile_without_description(
                driver="docker",
                selector="docker:python:3.13-slim",
                instance=_CONTAINER_ID[:12],
            ),
            None,
        ),
    ),
)
def test_remote_chat_runtime_identity_allows_optional_description(
    profile_payload: dict[str, object],
    expected_description: str | None,
) -> None:
    identity = remote._runtime_identity(profile_payload)

    assert identity.description == expected_description


@pytest.mark.parametrize(
    "profile_payload",
    (
        _profile_without_description(),
        {
            "runtime": {
                "version": "v0.4.0-12-g12345678",
                "sandbox": {
                    "driver": "host",
                    "selector": "host",
                    "instance": None,
                    "description": None,
                    "future": True,
                },
                "future": True,
            }
        },
    ),
)
def test_remote_chat_uses_local_host_description_when_profile_does_not_supply_it(
    profile_payload: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        remote,
        "host_sandbox_description",
        lambda: "Test OS 1.0 arm64",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=profile_payload)
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(200, json=_run_defaults())
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert session.executor_metadata == ChatExecutorMetadata(
            sandbox_selector="host",
            sandbox_detail="Test OS 1.0 arm64",
            endpoint="http://runtime.test:7001",
            version=remote._runtime_identity(profile_payload).version,
        )
    finally:
        session.close()


def test_remote_chat_repeated_concrete_runs_do_not_list_models() -> None:
    detail = _detail()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(200, json=_run_defaults())
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json=_models())
        if request.url.path == "/api/v1/runs/authored/stream":
            return _stream(_begin(), _end())
        if request.url.path == "/api/v1/runs/run_remote":
            return httpx.Response(200, json=_json(detail))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    events: list[RunEvent] = []
    states: list[object] = []
    errors: list[str] = []
    try:
        for override in (
            RunOverride(),
            RunOverride(model=ModelOverride(identity="test/model(variant)")),
        ):
            request = session.build_request(
                "term_remote",
                override,
                RunnableInputRaw(_="hello"),
                session.initial_setting(),
            )
            session.run(
                request,
                events.append,
                errors.append,
                states.append,
            )
    finally:
        session.close()

    assert session.executor_metadata == ChatExecutorMetadata(
        sandbox_selector="host",
        sandbox_detail=_HOST_DESCRIPTION,
        endpoint="http://runtime.test:7001",
        version="v0.3.9",
    )
    assert [type(item) for item in events] == [RunBegin, RunEnd, RunBegin, RunEnd]
    assert states == [RunAccepted("run_remote"), RunAccepted("run_remote")]
    assert errors == []
    assert requests.count("/api/v1/models") == 0
    assert requests.count("/api/v1/agics") == 0
    assert requests.count("/api/v1/flows") == 0
    assert requests.count("/api/v1/runs/authored/stream") == 2


def test_remote_chat_recovers_without_replaying_or_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote, "_RECOVERY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(remote, "_RECOVERY_INTERVAL", 0.0)
    terminal = _detail(
        output=Local.typed("Part[]", (TextPart("durable"),), "_"),
    )
    details = iter(
        (
            replace(terminal, status="running", finished_at=None),
            terminal,
        )
    )
    submissions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(200, json=_run_defaults())
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json=_models())
        if request.url.path == "/api/v1/runs/authored/stream":
            submissions += 1
            return _stream(_begin())
        if request.url.path == "/api/v1/runs/run_remote":
            return httpx.Response(200, json=_json(next(details)))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    events: list[RunEvent] = []
    states: list[object] = []
    errors: list[str] = []
    try:
        request = session.build_request(
            "term_remote",
            RunOverride(),
            RunnableInputRaw(_="hello"),
            session.initial_setting(),
        )
        session.run(
            request,
            events.append,
            errors.append,
            states.append,
        )
    finally:
        session.close()

    assert submissions == 1
    assert [type(item) for item in events] == [RunBegin]
    assert [type(item) for item in states] == [
        RunAccepted,
        RunDisconnected,
        RunRecovered,
    ]
    assert isinstance(states[-1], RunRecovered)
    assert states[-1].detail == terminal
    assert errors == []


def test_remote_chat_blocks_ambiguous_pre_acceptance_failure() -> None:
    submissions = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submissions
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/profile":
            return httpx.Response(200, json=_profile())
        if request.url.path == "/api/v1/runs/defaults":
            return httpx.Response(200, json=_run_defaults())
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json=_models())
        if request.url.path == "/api/v1/runs/authored/stream":
            submissions += 1
            raise httpx.ReadError("private transport detail", request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    session = remote.RemoteChatSession(
        "http://runtime.test:7001",
        expected_sandbox="host",
        transport=httpx.MockTransport(handler),
    )
    states: list[object] = []
    errors: list[str] = []
    try:
        for _ in range(2):
            request = session.build_request(
                "term_remote",
                RunOverride(),
                RunnableInputRaw(_="hello"),
                session.initial_setting(),
            )
            session.run(
                request,
                lambda _event: None,
                errors.append,
                states.append,
            )
    finally:
        session.close()

    assert submissions == 1
    assert len(states) == 2
    assert all(isinstance(item, RunBlocked) for item in states)
    assert errors == []
    assert "private transport detail" not in states[0].message

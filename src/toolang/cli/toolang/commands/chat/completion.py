"""Namespace-aware completion for terminal Chat authored input."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    PathCompleter,
)
from prompt_toolkit.document import Document

from toolang.execution.types import (
    ALLOW_FIELDS,
    LIMIT_FIELDS,
)


@dataclass(frozen=True, slots=True)
class _Candidate:
    text: str
    display: str
    meta: str


def _override_candidates() -> tuple[_Candidate, ...]:
    candidates = [
        *(
            _Candidate(
                f":allow {field}=",
                f":allow {field}=QUERY",
                "Restrict one resource collection.",
            )
            for field in sorted(ALLOW_FIELDS)
        ),
        *(
            _Candidate(
                f":limit {field}=",
                f":limit {field}=VALUE",
                "Set one execution limit.",
            )
            for field in sorted(LIMIT_FIELDS)
        ),
    ]
    candidates.extend(
        (
            _Candidate(
                ":model ",
                ":model MODEL? effort=VALUE",
                "Override the run model.",
            ),
            _Candidate(":agic ", ":agic AGIC", "Select an Agic runnable."),
            _Candidate(":flow ", ":flow FLOW", "Select a Flow runnable."),
            _Candidate(
                ":runnable ",
                ":runnable RUNNABLE",
                "Select a runnable.",
            ),
        )
    )
    return tuple(candidates)


_OVERRIDE_CANDIDATES = _override_candidates()


class ChatInputCompleter(Completer):
    """Complete one column-zero namespace without crossing into another."""

    def __init__(
        self,
        *,
        resource_paths: Callable[[], list[str]] | None = None,
    ) -> None:
        self._prompts: tuple[_Candidate, ...] = ()
        self._paths = PathCompleter(
            get_paths=resource_paths,
            expanduser=True,
        )

    def set_prompts(self, payload: Mapping[str, object]) -> None:
        """Replace prompt candidates from one current runnable catalog."""

        raw_items = payload.get("items", ())
        if not isinstance(raw_items, Sequence) or isinstance(
            raw_items, (str, bytes, bytearray)
        ):
            raise ValueError("prompt completion catalog items must be an array")
        candidates: list[_Candidate] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping):
                raise ValueError("prompt completion catalog contains an invalid item")
            item = cast(Mapping[str, object], raw_item)
            name = item.get("name")
            raw_params = item.get("params", ())
            if not isinstance(name, str) or not name:
                raise ValueError("prompt completion requires a name")
            if not isinstance(raw_params, Sequence) or isinstance(
                raw_params, (str, bytes, bytearray)
            ):
                raise ValueError("prompt completion params must be an array")
            params: list[tuple[str, bool]] = []
            for raw_param in raw_params:
                if not isinstance(raw_param, Mapping):
                    raise ValueError("prompt completion contains an invalid parameter")
                param = cast(Mapping[str, object], raw_param)
                param_name = param.get("name")
                optional = param.get("optional", False)
                if not isinstance(param_name, str) or not isinstance(optional, bool):
                    raise ValueError("prompt completion parameter is invalid")
                params.append((param_name, optional))
            arguments = "".join(f" {param_name}=" for param_name, _ in params)
            signature = "".join(
                f" {param_name}{'?' if optional else ''}="
                for param_name, optional in params
            )
            candidates.append(
                _Candidate(
                    text=f"${name}{arguments}",
                    display=f"${name}{signature}",
                    meta="Reusable prompt.",
                )
            )
        self._prompts = tuple(sorted(candidates, key=lambda item: item.text))

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        line = document.current_line_before_cursor
        if not line or line[0] not in "/$:@":
            return
        marker = line[0]
        if marker == "/":
            return
        if marker == "$":
            yield from _candidate_completions(line, self._prompts)
            return
        if marker == ":":
            yield from _candidate_completions(line, _OVERRIDE_CANDIDATES)
            return
        path_document = Document(
            text=line[1:],
            cursor_position=max(0, len(line) - 1),
        )
        yield from self._paths.get_completions(path_document, complete_event)


def _candidate_completions(
    prefix: str,
    candidates: Sequence[_Candidate],
) -> Iterable[Completion]:
    for candidate in candidates:
        if candidate.text == prefix or not candidate.text.startswith(prefix):
            continue
        yield Completion(
            candidate.text,
            start_position=-len(prefix),
            display=candidate.display,
            display_meta=candidate.meta,
        )

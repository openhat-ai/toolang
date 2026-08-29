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
    ALLOW_POLICY_FIELDS,
    DEFAULT_POLICY_FIELDS,
    LIMIT_POLICY_FIELDS,
)

from .slashes import SLASHES


@dataclass(frozen=True, slots=True)
class _Candidate:
    text: str
    display: str
    meta: str


def _slash_candidates() -> tuple[_Candidate, ...]:
    candidates: list[_Candidate] = []
    for slash in SLASHES:
        usage_tail = (
            slash.display_usage.split(maxsplit=1)
            if "," not in slash.display_usage
            else [slash.display_usage]
        )
        placeholder = usage_tail[1] if len(usage_tail) == 2 else ""
        placeholder = placeholder.replace("[", "").replace("]", "")
        for name in slash.names:
            text = f"/{name}{f' {placeholder}' if placeholder else ''}"
            candidates.append(
                _Candidate(
                    text=text,
                    display=f"/{name}{f' [{placeholder}]' if placeholder else ''}",
                    meta=slash.summary,
                )
            )
    return tuple(candidates)


def _policy_candidates() -> tuple[_Candidate, ...]:
    candidates = [
        *(
            _Candidate(
                f":allow {field}=",
                f":allow {field}=SELECTORS",
                "Restrict one resource domain.",
            )
            for field in sorted(ALLOW_POLICY_FIELDS)
        ),
        *(
            _Candidate(
                f":default {field}=",
                f":default {field}=VALUE",
                "Set one execution default.",
            )
            for field in sorted(DEFAULT_POLICY_FIELDS)
        ),
        *(
            _Candidate(
                f":limit {field}=",
                f":limit {field}=VALUE",
                "Set one execution limit.",
            )
            for field in sorted(LIMIT_POLICY_FIELDS)
        ),
    ]
    candidates.extend(
        _Candidate(f":{field} ", f":{field} SELECTORS", "Restrict resources.")
        for field in sorted(ALLOW_POLICY_FIELDS)
    )
    candidates.extend(
        (
            _Candidate(":model ", ":model MODEL", "Select the run model."),
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


_SLASH_CANDIDATES = _slash_candidates()
_POLICY_CANDIDATES = _policy_candidates()


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
            if "\n" in document.text_before_cursor:
                return
            yield from _candidate_completions(line, _SLASH_CANDIDATES)
            return
        if marker == "$":
            yield from _candidate_completions(line, self._prompts)
            return
        if marker == ":":
            yield from _candidate_completions(line, _POLICY_CANDIDATES)
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

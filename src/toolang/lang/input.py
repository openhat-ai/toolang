"""Runnable input parsing, part resolution, and typed boundary coercion."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
import re
import shlex
from types import MappingProxyType
from typing import TypeAlias, cast

from toolang.base.errors import ToolangError
from toolang.base.types.message import (
    Message,
    Part,
    TextPart,
    message_text,
)
from toolang.common.template import render_text_template

from .ast import AgicDecl, FlowDecl, Parameter, Program, StructDecl
from .errors import ToolangOutputError
from .types import Array, Struct, Value

IncludeResolver = Callable[[str], Part]
NamedInputSources: TypeAlias = tuple[tuple[str, str], ...]
_ARGUMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")
_SLOT_RE = re.compile(r"\ue000(\d+)\ue001")
_MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_JSON_OUTPUT_FENCE_RE = re.compile(
    r"```[ \t]*json[ \t]*\r?\n(?P<value>.*?)\r?\n?```",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class RunnableInputRaw:
    """Structured primary and named source text awaiting input resolution."""

    primary: str | None = None
    named: NamedInputSources = ()


@dataclass(frozen=True, slots=True)
class RunnableInput:
    """Resolved primary and named inputs adopted by one run."""

    primary: Value | None = None
    named: Mapping[str, Value] = field(default_factory=dict)

    def __post_init__(self) -> None:
        primary = (
            _require_input_value(self.primary) if self.primary is not None else None
        )
        if not isinstance(self.named, Mapping):
            raise TypeError("run named inputs must be a mapping")
        named: dict[str, Value] = {}
        for name, value in sorted(self.named.items()):
            if not isinstance(name, str) or not _ARGUMENT_NAME_RE.fullmatch(name):
                raise ValueError("run input value requires a canonical name")
            named[name] = _require_input_value(value)
        object.__setattr__(self, "primary", primary)
        object.__setattr__(self, "named", MappingProxyType(named))


def resolve_runnable_input(
    runnable: AgicDecl | FlowDecl,
    *,
    primary: object | None = None,
    named: Mapping[str, object] | None = None,
    structs: Mapping[str, StructDecl] | None = None,
) -> RunnableInput:
    """Resolve caller values once against one runnable signature."""

    parameters = {parameter.name: parameter for parameter in runnable.params}
    arguments = dict(named or {})
    unknown = sorted(set(arguments) - set(parameters))
    if unknown:
        raise ValueError(
            f"unknown named inputs for {runnable.name}: {', '.join(unknown)}"
        )
    missing = sorted(
        name
        for name, parameter in parameters.items()
        if not parameter.optional and name not in arguments
    )
    if missing:
        raise ValueError(
            f"missing named inputs for {runnable.name}: {', '.join(missing)}"
        )
    if runnable.input is None and primary is not None:
        raise ValueError(f"{runnable.name} does not accept primary input")
    if runnable.input is not None and not runnable.input.optional and primary is None:
        raise ValueError(f"{runnable.name} requires primary input")

    declared_structs = structs or {}
    resolved_primary = (
        coerce_input(
            primary,
            runnable.input.type_name or "Part[]",
            structs=declared_structs,
        )
        if runnable.input is not None and primary is not None
        else None
    )
    resolved_named = {
        name: coerce_input(
            value,
            parameters[name].type_name or "Part[]",
            structs=declared_structs,
        )
        for name, value in arguments.items()
    }
    return RunnableInput(primary=resolved_primary, named=resolved_named)


def parse_input(
    source: str | None,
    *,
    named: NamedInputSources = (),
) -> RunnableInputRaw:
    """Parse runnable input without resolving includes or declared types."""

    primary = source if source and source.strip() else None
    if primary is not None:
        first = primary.splitlines()[0]
        if first.startswith(":") and not first.startswith("::"):
            raise ValueError("primary input must escape a leading colon as ::")

    parsed_named: list[tuple[str, str]] = []
    names: set[str] = set()
    for item in named:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("named input sources must be name/source pairs")
        name, value = item
        if not isinstance(name, str) or not _ARGUMENT_NAME_RE.fullmatch(name):
            raise ValueError("named input must use a canonical name")
        if not isinstance(value, str):
            raise TypeError("named input source must be a string")
        if name in names:
            raise ValueError(f"duplicate named input: {name}")
        names.add(name)
        parsed_named.append((name, value))
    return RunnableInputRaw(primary=primary, named=tuple(parsed_named))


def resolve_input_parts(
    source: str | Sequence[Part],
    *,
    program: Program | None = None,
    values: Mapping[str, object] | None = None,
    types: Mapping[str, str] | None = None,
    include: IncludeResolver | None = None,
) -> tuple[Part, ...]:
    """Resolve one textual or structured input into ordered canonical parts."""

    if not isinstance(source, str):
        return _require_parts(source)
    return _resolve_parts_body(
        source,
        program=program,
        values=values or {},
        types=types or {},
        include=include,
        prompt_stack=(),
        depth=0,
    )


def coerce_input(
    value: object,
    type_name: str,
    *,
    structs: Mapping[str, StructDecl] | None = None,
) -> Value:
    """Coerce one caller value to a declared runnable input type."""

    return _coerce_value(
        value,
        type_name,
        structs=structs or {},
        boundary="input",
    )


def coerce_output(
    value: object,
    type_name: str | None,
    *,
    structs: Mapping[str, StructDecl] | None = None,
) -> Value:
    """Coerce one runnable result to its declared output type."""

    if type_name is None:
        if isinstance(value, Message):
            return _canonical_value(_message_parts(value), "Part[]", structs or {})
        return _require_input_value(value)
    try:
        return _coerce_value(
            value,
            type_name,
            structs=structs or {},
            boundary="output",
        )
    except ToolangError as exc:
        raise ToolangOutputError(str(exc)) from exc


def validate_value(
    value: object,
    type_name: str,
    *,
    structs: Mapping[str, StructDecl],
    path: str = "value",
) -> None:
    """Validate one Toolang runtime value against a declared type."""

    if type_name.endswith("[]"):
        if isinstance(value, Array) and value.type != type_name:
            raise ToolangError(f"{path} is {value.type}, not {type_name}")
        if not isinstance(value, Array | list | tuple):
            raise ToolangError(f"{path} is not {type_name}")
        item_type = type_name[:-2]
        for index, item in enumerate(value):
            validate_value(
                item,
                item_type,
                structs=structs,
                path=f"{path}[{index}]",
            )
        return

    if type_name == "Text":
        valid = isinstance(value, str)
    elif type_name == "Number":
        valid = (
            not isinstance(value, bool)
            and isinstance(value, int | float)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    elif type_name == "Boolean":
        valid = isinstance(value, bool)
    elif type_name == "Json":
        valid = _is_json_value(value)
    elif type_name == "Part":
        valid = _is_part(value)
    elif struct := structs.get(type_name):
        if isinstance(value, Struct) and value.type != type_name:
            raise ToolangError(f"{path} is {value.type}, not {type_name}")
        if not isinstance(value, Mapping):
            valid = False
        else:
            fields = {field.name: field for field in struct.fields}
            unknown = set(value) - set(fields)
            missing = {
                name
                for name, field in fields.items()
                if not field.optional and name not in value
            }
            if unknown:
                names = ", ".join(sorted(str(name) for name in unknown))
                raise ToolangError(f"{path} has unknown {type_name} fields: {names}")
            if missing:
                names = ", ".join(sorted(missing))
                raise ToolangError(f"{path} is missing {type_name} fields: {names}")
            for name, item in value.items():
                validate_value(
                    item,
                    fields[str(name)].type_name,
                    structs=structs,
                    path=f"{path}.{name}",
                )
            return
    else:
        raise ToolangError(f"unknown Toolang type: {type_name}")

    if not valid:
        raise ToolangError(f"{path} is not {type_name}")


def _resolve_parts_body(
    body: str,
    *,
    program: Program | None,
    values: Mapping[str, object],
    types: Mapping[str, str],
    include: IncludeResolver | None,
    prompt_stack: tuple[str, ...],
    depth: int,
) -> tuple[Part, ...]:
    if depth > 16:
        raise ToolangError("Prompt composition exceeded the maximum depth of 16.")
    rendered, slots = _render_body(body, values=values, types=types)
    lines = rendered.splitlines(keepends=True)
    output: list[Part] = []
    markdown_fence: tuple[str, int] | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        text, line_break = _split_line(line)
        if markdown_fence is not None:
            _extend_parts(output, _text_parts(line, slots))
            if _closes_markdown_fence(text, markdown_fence):
                markdown_fence = None
            index += 1
            continue

        opened_fence = _opens_markdown_fence(text)
        if opened_fence is not None:
            _extend_parts(output, _text_parts(line, slots))
            markdown_fence = opened_fence
            index += 1
            continue

        if text.startswith(("::", "//", "@@")):
            literal = text[1:] + line_break
            _extend_parts(output, _text_parts(literal, slots))
            index += 1
            continue
        if text.startswith("@"):
            reference = _include_reference(text)
            if include is None:
                raise ToolangError(f"Include resolver is required for: {reference}")
            _append_part(output, _require_part(include(reference)))
            if line_break:
                _append_part(output, TextPart(line_break))
            index += 1
            continue

        call = _prompt_call(text)
        if call is not None:
            prompt_name = call.name
            attached_body = ""
            consumed = 1
            following_break = line_break
            if call.scope == "tail":
                if not line_break:
                    raise ToolangError(
                        f"Tail prompt /{prompt_name} requires a following line."
                    )
                attached_body = "".join(lines[index + 1 :])
                consumed = len(lines) - index
                following_break = ""
            elif call.scope == "fenced":
                if not line_break:
                    raise ToolangError(
                        f"Fenced prompt /{prompt_name} requires a following line."
                    )
                assert call.fence is not None
                closing_index = _find_prompt_fence(
                    lines,
                    start=index + 1,
                    fence=call.fence,
                )
                if closing_index is None:
                    raise ToolangError(f"Unclosed prompt fence for /{prompt_name}.")
                attached_body = "".join(lines[index + 1 : closing_index])
                _, following_break = _split_line(lines[closing_index])
                consumed = closing_index - index + 1
            prompt_input = _resolve_parts_body(
                attached_body,
                program=program,
                values=values,
                types=types,
                include=include,
                prompt_stack=prompt_stack,
                depth=depth + 1,
            )
            _extend_parts(
                output,
                _resolve_prompt_parts(
                    program,
                    prompt_name=prompt_name,
                    raw_args=call.raw_args,
                    input=prompt_input,
                    include=include,
                    prompt_stack=prompt_stack,
                    depth=depth + 1,
                ),
            )
            if following_break:
                _append_part(output, TextPart(following_break))
            index += consumed
            continue

        _extend_parts(output, _text_parts(line, slots))
        index += 1
    return tuple(output)


def _render_body(
    body: str,
    *,
    values: Mapping[str, object],
    types: Mapping[str, str],
) -> tuple[str, tuple[Part, ...]]:
    if "\ue000" in body or "\ue001" in body:
        raise ToolangError("ContentBody contains a reserved marker.")
    if not values:
        return body, ()
    template = body
    context: dict[str, object] = {}
    slots: list[Part] = []
    for name, value in values.items():
        type_name = types.get(name)
        if type_name == "Part":
            part = _require_part(value)
            marker = _slot_marker(slots, part)
            template = _replace_direct_value(template, name, marker)
            context[name] = marker
            continue
        if type_name == "Part[]":
            parts = _require_parts(value)
            markers = [_slot_marker(slots, part) for part in parts]
            template = _replace_direct_value(template, name, "".join(markers))
            context[name] = markers
            continue
        context[name] = _template_value(value, type_name=type_name)
    return render_text_template(template, context), tuple(slots)


def _resolve_prompt_parts(
    program: Program | None,
    *,
    prompt_name: str,
    raw_args: str,
    input: tuple[Part, ...],
    include: IncludeResolver | None,
    prompt_stack: tuple[str, ...],
    depth: int,
) -> tuple[Part, ...]:
    if program is None:
        raise ToolangError(f"Prompt not found: {prompt_name}")
    if prompt_name in prompt_stack:
        chain = " -> ".join((*prompt_stack, prompt_name))
        raise ToolangError(f"Prompt cycle: {chain}")
    prompt = next(
        (
            item
            for item in program.caps
            if item.kind == "prompt" and item.name == prompt_name
        ),
        None,
    )
    if prompt is None:
        raise ToolangError(f"Prompt not found: {prompt_name}")
    bindings = _parse_prompt_args(
        raw_args,
        params=prompt.params,
        prompt_name=prompt_name,
    )
    return _strip_text_boundaries(
        _resolve_parts_body(
            prompt.body,
            program=program,
            values={"_": input, **bindings},
            types={
                "_": "Part[]",
                **{parameter.name: "Text" for parameter in prompt.params},
            },
            include=include,
            prompt_stack=(*prompt_stack, prompt_name),
            depth=depth,
        )
    )


@dataclass(frozen=True, slots=True)
class _PromptCall:
    name: str
    raw_args: str
    scope: str
    fence: str | None = None


def _prompt_call(line: str) -> _PromptCall | None:
    match = _PROMPT_CALL_RE.fullmatch(line)
    if match is None:
        return None
    prompt_name = match.group(1)
    raw = match.group(2) or ""
    terminal = _unquoted_terminal_token(raw)
    if terminal is None:
        return _PromptCall(prompt_name, raw.strip(), "none")
    start, token = terminal
    if token == "-":
        return _PromptCall(prompt_name, raw[:start].rstrip(), "tail")
    if len(token) >= 3 and set(token) == {"`"}:
        return _PromptCall(
            prompt_name,
            raw[:start].rstrip(),
            "fenced",
            fence=token,
        )
    return _PromptCall(prompt_name, raw.strip(), "none")


def _unquoted_terminal_token(value: str) -> tuple[int, str] | None:
    quote: str | None = None
    escaped = False
    token_start: int | None = None
    token_is_plain = True
    last: tuple[int, str] | None = None
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            token_is_plain = False
            continue
        if character == "\\":
            if token_start is None:
                token_start = index
            escaped = True
            continue
        if character in {'"', "'"}:
            if token_start is None:
                token_start = index
            token_is_plain = False
            quote = (
                None if quote == character else character if quote is None else quote
            )
            continue
        if quote is None and character in " \t":
            if token_start is not None:
                if token_is_plain:
                    last = (token_start, value[token_start:index])
                else:
                    last = None
                token_start = None
                token_is_plain = True
            continue
        if token_start is None:
            token_start = index
    if token_start is not None:
        return (token_start, value[token_start:]) if token_is_plain else None
    return last


def _split_line(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def _opens_markdown_fence(line: str) -> tuple[str, int] | None:
    match = _MARKDOWN_FENCE_RE.fullmatch(line)
    if match is None:
        return None
    fence, info = match.groups()
    if fence[0] == "`" and "`" in info:
        return None
    return fence[0], len(fence)


def _closes_markdown_fence(
    line: str,
    fence: tuple[str, int],
) -> bool:
    marker, minimum = fence
    match = re.fullmatch(r" {0,3}([`~]+)[ \t]*", line)
    return bool(
        match
        and match.group(1)[0] == marker
        and set(match.group(1)) == {marker}
        and len(match.group(1)) >= minimum
    )


def _find_prompt_fence(
    lines: Sequence[str],
    *,
    start: int,
    fence: str,
) -> int | None:
    for index in range(start, len(lines)):
        text, _ = _split_line(lines[index])
        if text == fence:
            return index
    return None


def _include_reference(line: str) -> str:
    try:
        tokens = shlex.split(line[1:].strip())
    except ValueError as exc:
        raise ToolangError(f"Invalid include reference: {exc}") from exc
    if len(tokens) != 1:
        raise ToolangError(f"Invalid include reference: {line}")
    return tokens[0]


def _text_parts(
    value: str,
    slots: Sequence[Part],
) -> list[Part]:
    parts: list[Part] = []
    cursor = 0
    for match in _SLOT_RE.finditer(value):
        if match.start() > cursor:
            parts.append(TextPart(value[cursor : match.start()]))
        index = int(match.group(1))
        if index >= len(slots):
            raise ToolangError("ContentBody contains an invalid part marker.")
        parts.append(slots[index])
        cursor = match.end()
    if cursor < len(value):
        parts.append(TextPart(value[cursor:]))
    return parts


def _extend_parts(
    output: list[Part],
    parts: Sequence[Part],
) -> None:
    for part in parts:
        _append_part(output, part)


def _append_part(output: list[Part], part: Part) -> None:
    if isinstance(part, TextPart) and not part.text:
        return
    if isinstance(part, TextPart) and output and isinstance(output[-1], TextPart):
        output[-1] = TextPart(output[-1].text + part.text)
        return
    output.append(part)


def _strip_text_boundaries(parts: tuple[Part, ...]) -> tuple[Part, ...]:
    result = list(parts)
    if result and isinstance(result[0], TextPart):
        result[0] = TextPart(result[0].text.lstrip())
    if result and isinstance(result[-1], TextPart):
        result[-1] = TextPart(result[-1].text.rstrip())
    return tuple(part for part in result if not isinstance(part, TextPart) or part.text)


def _replace_direct_value(template: str, name: str, value: str) -> str:
    pattern = re.compile(r"{{\s*" + re.escape(name) + r"\s*}}")
    return pattern.sub(lambda _match: value, template)


def _slot_marker(slots: list[Part], part: Part) -> str:
    marker = f"\ue000{len(slots)}\ue001"
    slots.append(part)
    return marker


def _template_value(value: object, *, type_name: str | None) -> object:
    if value is None:
        return ""
    if type_name == "Boolean" or isinstance(value, bool):
        return "true" if bool(value) else "false"
    if type_name == "Number":
        return str(value)
    if type_name == "Json" or (
        type_name is not None
        and (type_name.endswith("[]") or type_name not in {"Text", "Number", "Boolean"})
    ):
        return json.dumps(
            _plain_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    if isinstance(value, Mapping | list | tuple):
        return json.dumps(
            _plain_value(value),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    return value


def _plain_value(value: object) -> object:
    if isinstance(value, Array):
        return [_plain_value(item) for item in value]
    if isinstance(value, Struct | Mapping):
        return {str(name): _plain_value(item) for name, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain_value(item) for item in value]
    return value


def _coerce_value(
    value: object,
    type_name: str,
    *,
    structs: Mapping[str, StructDecl],
    boundary: str,
) -> Value:
    if type_name == "Part[]":
        result = _as_parts(value)
    elif type_name == "Part":
        parts = _as_parts(value)
        if len(parts) != 1:
            raise ToolangError(
                f"{boundary} is not Part: expected 1 part, got {len(parts)}"
            )
        result = parts[0]
    elif type_name == "Text":
        result = _text_value(value, boundary=boundary)
    elif type_name == "Number":
        result = _number_value(value, boundary=boundary)
    elif type_name == "Boolean":
        result = _boolean_value(value, boundary=boundary)
    else:
        result = _structured_value(value, type_name=type_name, boundary=boundary)
    validate_value(result, type_name, structs=structs, path=boundary)
    return _canonical_value(result, type_name, structs)


def _canonical_value(
    value: object,
    type_name: str,
    structs: Mapping[str, StructDecl],
) -> Value:
    if type_name.endswith("[]"):
        items = (
            value.value
            if isinstance(value, Array)
            else tuple(cast(Sequence[object], value))
        )
        item_type = type_name[:-2]
        return Array(
            type_name,
            tuple(_canonical_value(item, item_type, structs) for item in items),
        )
    if struct := structs.get(type_name):
        fields = {field.name: field for field in struct.fields}
        return Struct(
            type_name,
            {
                name: _canonical_value(item, fields[name].type_name, structs)
                for name, item in cast(Mapping[str, object], value).items()
            },
        )
    if type_name == "Json":
        return cast(Value, _canonical_json(value))
    return _require_input_value(value)


def _canonical_json(value: object) -> object:
    if isinstance(value, Array):
        return Array(value.type, tuple(_canonical_json(item) for item in value))
    if isinstance(value, Struct):
        return Struct(
            value.type,
            {name: _canonical_json(item) for name, item in value.items()},
        )
    if isinstance(value, Mapping):
        return {str(name): _canonical_json(item) for name, item in value.items()}
    if isinstance(value, tuple | list):
        return tuple(_canonical_json(item) for item in value)
    return value


def _as_parts(value: object) -> tuple[Part, ...]:
    if isinstance(value, Message):
        return _message_parts(value)
    if _is_part(value):
        return (cast(Part, value),)
    return _require_parts(value)


def _message_parts(message: Message) -> tuple[Part, ...]:
    return message.parts


def _text_value(value: object, *, boundary: str) -> str:
    if isinstance(value, str):
        return value
    parts = _as_parts(value)
    if not all(isinstance(part, TextPart) for part in parts):
        raise ToolangError(f"{boundary} is not Text: non-text parts are present")
    return message_text(parts)


def _number_value(value: object, *, boundary: str) -> int | float:
    if not isinstance(value, (str, Message, Array, tuple, list)):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ToolangError(f"{boundary} is not Number")
        return value
    parsed = _parse_text_json(value, type_name="Number", boundary=boundary)
    if isinstance(parsed, bool) or not isinstance(parsed, int | float):
        raise ToolangError(f"{boundary} is not Number")
    return parsed


def _boolean_value(value: object, *, boundary: str) -> bool:
    if isinstance(value, bool):
        return value
    text = _text_value(value, boundary=boundary).strip()
    if text == "true":
        return True
    if text == "false":
        return False
    raise ToolangError(f"{boundary} is not Boolean")


def _structured_value(
    value: object,
    *,
    type_name: str,
    boundary: str,
) -> object:
    if isinstance(value, (str, Message)) or (
        isinstance(value, Array | tuple | list)
        and all(_is_part(part) for part in value)
    ):
        return _parse_text_json(
            value,
            type_name=type_name,
            boundary=boundary,
        )
    return value


def _parse_text_json(
    value: object,
    *,
    type_name: str,
    boundary: str,
) -> object:
    text = _text_value(value, boundary=boundary)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        error = exc
        if boundary == "output":
            blocks = tuple(
                match.group("value") for match in _JSON_OUTPUT_FENCE_RE.finditer(text)
            )
            if len(blocks) == 1:
                try:
                    return json.loads(blocks[0])
                except json.JSONDecodeError as fenced_exc:
                    error = fenced_exc
        raise ToolangError(
            f"{boundary} is not valid {type_name}: {error.msg}"
        ) from error


def _validate_input_value(value: object) -> None:
    if _is_part(value):
        return
    if isinstance(value, Array | tuple | list):
        for item in value:
            _validate_input_value(item)
        return
    if isinstance(value, Struct | Mapping):
        for item in value.values():
            _validate_input_value(item)
        return
    if value is None or isinstance(value, str | bool | int | float):
        if isinstance(value, float) and not _is_json_value(value):
            raise TypeError("run input value must be finite")
        return
    raise TypeError(f"unsupported run input value: {type(value).__name__}")


def _require_input_value(value: object) -> Value:
    candidate = cast(Value, value)
    _validate_input_value(candidate)
    return candidate


def _require_parts(value: object) -> tuple[Part, ...]:
    if not isinstance(value, Array | tuple | list):
        raise ToolangError("Part[] requires an ordered part sequence")
    if isinstance(value, Array) and value.type != "Part[]":
        raise ToolangError(f"Part[] cannot use {value.type}")
    parts = tuple(value)
    if not all(_is_part(part) for part in parts):
        raise ToolangError("Part[] can only contain Part values")
    return cast(tuple[Part, ...], parts)


def _require_part(value: object) -> Part:
    if not _is_part(value):
        raise ToolangError("value is not Part")
    return cast(Part, value)


def _is_part(value: object) -> bool:
    return isinstance(value, Part)


def _is_json_value(value: object) -> bool:
    if _is_part(value):
        return True
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Array | tuple | list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, Struct | Mapping):
        return all(
            isinstance(name, str) and _is_json_value(item)
            for name, item in value.items()
        )
    return False


def _parse_prompt_args(
    raw_args: str,
    *,
    params: tuple[Parameter, ...],
    prompt_name: str,
) -> dict[str, str]:
    if not raw_args.strip():
        tokens: list[str] = []
    else:
        try:
            tokens = shlex.split(raw_args)
        except ValueError as exc:
            raise ToolangError(f"Invalid prompt argument syntax: {exc}") from exc

    bindings: dict[str, str] = {}
    known = {param.name for param in params}
    for token in tokens:
        candidate, separator, value = token.partition("=")
        if not separator:
            raise ToolangError(
                f"Prompt argument must use name=value syntax for /{prompt_name}."
            )
        if candidate not in known:
            raise ToolangError(
                f"Unknown prompt argument {candidate!r} for /{prompt_name}."
            )
        if candidate in bindings:
            raise ToolangError(
                f"Duplicate prompt argument {candidate!r} for /{prompt_name}."
            )
        bindings[candidate] = value

    for param in params:
        if param.name in bindings:
            continue
        if param.optional:
            bindings[param.name] = ""
            continue
        raise ToolangError(
            f"Missing required prompt argument {param.name!r} for /{prompt_name}."
        )

    return bindings

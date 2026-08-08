"""Input perceiving and executable boundary coercion."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import re
import shlex
from typing import TypeAlias, cast

from toolang.base.errors import ToolangError
from toolang.base.types.message import (
    AudioPart,
    DocumentPart,
    ImagePart,
    Message,
    Percept,
    PerceptPart,
    TextPart,
    message_text,
)
from toolang.common.template import render_text_template

from .ast import Parameter, Program, StructDecl

IncludeResolver = Callable[[str], PerceptPart]
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
class RunnableInput:
    """Syntax-valid primary and named sources for one runnable invocation."""

    primary: str | None = None
    named: NamedInputSources = ()


def parse_input(
    source: str | None,
    *,
    named: NamedInputSources = (),
) -> RunnableInput:
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
    return RunnableInput(primary=primary, named=tuple(parsed_named))


def perceive_input(
    source: str | Percept,
    *,
    program: Program | None = None,
    values: Mapping[str, object] | None = None,
    types: Mapping[str, str] | None = None,
    include: IncludeResolver | None = None,
) -> Percept:
    """Interpret one supported input as an ordered canonical percept."""

    if not isinstance(source, str):
        return _require_percept(source)
    return _perceive_body(
        source,
        program=program,
        values=values or {},
        types=types or {},
        include=include,
        prompt_stack=(),
        depth=0,
    )


def coerce_input(
    percept: Percept,
    type_name: str,
    *,
    structs: Mapping[str, StructDecl] | None = None,
) -> object:
    """Coerce one canonical percept to a runnable primary input type."""

    return _coerce_value(
        _require_percept(percept),
        type_name,
        structs=structs or {},
        boundary="input",
    )


def coerce_output(
    value: object,
    type_name: str | None,
    *,
    structs: Mapping[str, StructDecl] | None = None,
) -> object:
    """Coerce one runnable result to its declared output type."""

    if type_name is None:
        if isinstance(value, Message):
            return _message_percept(value)
        return value
    return _coerce_value(
        value,
        type_name,
        structs=structs or {},
        boundary="output",
    )


def validate_value(
    value: object,
    type_name: str,
    *,
    structs: Mapping[str, StructDecl],
    path: str = "value",
) -> None:
    """Validate one Toolang runtime value against a declared type."""

    if type_name.endswith("[]"):
        if type_name == "Part[]":
            _require_percept(value)
            return
        if not isinstance(value, list | tuple):
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
        valid = not isinstance(value, bool) and isinstance(value, int | float)
    elif type_name == "Boolean":
        valid = isinstance(value, bool)
    elif type_name == "Json":
        valid = _is_json_value(value)
    elif type_name == "Part":
        valid = _is_percept_part(value)
    elif struct := structs.get(type_name):
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
                raise ToolangError(
                    f"{path} has unknown {type_name} fields: {names}"
                )
            if missing:
                names = ", ".join(sorted(missing))
                raise ToolangError(
                    f"{path} is missing {type_name} fields: {names}"
                )
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


def _perceive_body(
    body: str,
    *,
    program: Program | None,
    values: Mapping[str, object],
    types: Mapping[str, str],
    include: IncludeResolver | None,
    prompt_stack: tuple[str, ...],
    depth: int,
) -> Percept:
    if depth > 16:
        raise ToolangError("Prompt composition exceeded the maximum depth of 16.")
    rendered, slots = _render_body(body, values=values, types=types)
    lines = rendered.splitlines(keepends=True)
    output: list[PerceptPart] = []
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
                raise ToolangError(
                    f"Include resolver is required for: {reference}"
                )
            _append_part(output, _require_percept_part(include(reference)))
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
                    raise ToolangError(
                        f"Unclosed prompt fence for /{prompt_name}."
                    )
                attached_body = "".join(lines[index + 1 : closing_index])
                _, following_break = _split_line(lines[closing_index])
                consumed = closing_index - index + 1
            prompt_input = _perceive_body(
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
                _perceive_prompt(
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
) -> tuple[str, tuple[PerceptPart, ...]]:
    if "\ue000" in body or "\ue001" in body:
        raise ToolangError("ContentBody contains a reserved marker.")
    if not values:
        return body, ()
    template = body
    context: dict[str, object] = {}
    slots: list[PerceptPart] = []
    for name, value in values.items():
        type_name = types.get(name)
        if type_name == "Part":
            part = _require_percept_part(value)
            marker = _slot_marker(slots, part)
            template = _replace_direct_value(template, name, marker)
            context[name] = marker
            continue
        if type_name == "Part[]":
            percept = _require_percept(value)
            markers = [_slot_marker(slots, part) for part in percept]
            template = _replace_direct_value(template, name, "".join(markers))
            context[name] = markers
            continue
        context[name] = _template_value(value, type_name=type_name)
    return render_text_template(template, context), tuple(slots)


def _perceive_prompt(
    program: Program | None,
    *,
    prompt_name: str,
    raw_args: str,
    input: Percept,
    include: IncludeResolver | None,
    prompt_stack: tuple[str, ...],
    depth: int,
) -> Percept:
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
        _perceive_body(
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
            quote = None if quote == character else character if quote is None else quote
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
    slots: Sequence[PerceptPart],
) -> list[PerceptPart]:
    parts: list[PerceptPart] = []
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
    output: list[PerceptPart],
    parts: Sequence[PerceptPart],
) -> None:
    for part in parts:
        _append_part(output, part)


def _append_part(output: list[PerceptPart], part: PerceptPart) -> None:
    if isinstance(part, TextPart) and not part.text:
        return
    if (
        isinstance(part, TextPart)
        and output
        and isinstance(output[-1], TextPart)
    ):
        output[-1] = TextPart(output[-1].text + part.text)
        return
    output.append(part)


def _strip_text_boundaries(parts: Percept) -> Percept:
    result = list(parts)
    if result and isinstance(result[0], TextPart):
        result[0] = TextPart(result[0].text.lstrip())
    if result and isinstance(result[-1], TextPart):
        result[-1] = TextPart(result[-1].text.rstrip())
    return tuple(
        part
        for part in result
        if not isinstance(part, TextPart) or part.text
    )


def _replace_direct_value(template: str, name: str, value: str) -> str:
    pattern = re.compile(r"{{\s*" + re.escape(name) + r"\s*}}")
    return pattern.sub(lambda _match: value, template)


def _slot_marker(slots: list[PerceptPart], part: PerceptPart) -> str:
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
        and (
            type_name.endswith("[]")
            or type_name not in {"Text", "Number", "Boolean"}
        )
    ):
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    if isinstance(value, Mapping | list | tuple):
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    return value


def _coerce_value(
    value: object,
    type_name: str,
    *,
    structs: Mapping[str, StructDecl],
    boundary: str,
) -> object:
    if type_name == "Part[]":
        result = _as_percept(value)
    elif type_name == "Part":
        percept = _as_percept(value)
        if len(percept) != 1:
            raise ToolangError(
                f"{boundary} is not Part: expected 1 part, got {len(percept)}"
            )
        result = percept[0]
    elif type_name == "Text":
        result = _text_value(value, boundary=boundary)
    elif type_name == "Number":
        result = _number_value(value, boundary=boundary)
    elif type_name == "Boolean":
        result = _boolean_value(value, boundary=boundary)
    else:
        result = _structured_value(value, type_name=type_name, boundary=boundary)
    validate_value(result, type_name, structs=structs, path=boundary)
    return result


def _as_percept(value: object) -> Percept:
    if isinstance(value, Message):
        return _message_percept(value)
    if _is_percept_part(value):
        return (cast(PerceptPart, value),)
    return _require_percept(value)


def _message_percept(message: Message) -> Percept:
    try:
        return message.percept
    except ValueError as exc:
        raise ToolangError(str(exc)) from exc


def _text_value(value: object, *, boundary: str) -> str:
    if isinstance(value, str):
        return value
    percept = _as_percept(value)
    if not all(isinstance(part, TextPart) for part in percept):
        raise ToolangError(f"{boundary} is not Text: non-text parts are present")
    return message_text(percept)


def _number_value(value: object, *, boundary: str) -> int | float:
    if not isinstance(value, (str, Message, tuple, list)):
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
        isinstance(value, tuple)
        and all(_is_percept_part(part) for part in value)
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
                match.group("value")
                for match in _JSON_OUTPUT_FENCE_RE.finditer(text)
            )
            if len(blocks) == 1:
                try:
                    return json.loads(blocks[0])
                except json.JSONDecodeError as fenced_exc:
                    error = fenced_exc
        raise ToolangError(
            f"{boundary} is not valid {type_name}: {error.msg}"
        ) from error


def _require_percept(value: object) -> Percept:
    if not isinstance(value, tuple | list):
        raise ToolangError("Percept must be an ordered part sequence")
    parts = tuple(value)
    if not all(_is_percept_part(part) for part in parts):
        raise ToolangError(
            "Percept can only contain text, image, audio, or document parts"
        )
    return cast(Percept, parts)


def _require_percept_part(value: object) -> PerceptPart:
    if not _is_percept_part(value):
        raise ToolangError(
            "Part must be text, image, audio, or document"
        )
    return cast(PerceptPart, value)


def _is_percept_part(value: object) -> bool:
    return isinstance(value, (TextPart, ImagePart, AudioPart, DocumentPart))


def _is_json_value(value: object) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


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
            f"Missing required prompt argument {param.name!r} "
            f"for /{prompt_name}."
        )

    return bindings

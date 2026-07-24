"""Input perceiving and executable boundary coercion."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import re
import shlex
import textwrap
from typing import cast

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

_PROMPT_CALL_RE = re.compile(r"^/([A-Za-z_][\w-]*)(?:\s+(.*))?$")
_SLOT_RE = re.compile(r"\ue000(\d+)\ue001")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


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
    fence: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        fence_match = _FENCE_RE.match(line)
        if fence_match is not None:
            marker = fence_match.group(1)[0]
            fence = None if fence == marker else marker if fence is None else fence
            _extend_parts(output, _text_parts(line, slots))
            index += 1
            continue
        if fence is not None:
            _extend_parts(output, _text_parts(line, slots))
            index += 1
            continue

        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("@@"):
            prefix = line.index(stripped)
            literal = line[:prefix] + stripped[1:]
            _extend_parts(output, _text_parts(literal, slots))
            index += 1
            continue
        if stripped.startswith("@"):
            reference = _include_reference(stripped)
            if include is None:
                raise ToolangError(
                    f"Include resolver is required for: {reference}"
                )
            _append_part(output, _require_percept_part(include(reference)))
            if line.endswith(("\n", "\r")):
                _append_part(output, TextPart("\n"))
            index += 1
            continue

        call = _prompt_call(stripped)
        if call is not None:
            prompt_name, raw_args, inline_body, accepts_indented = call
            attached_body = inline_body
            consumed = 1
            if accepts_indented and not inline_body:
                attached_body, following = _indented_body(
                    lines[index + 1 :],
                    base_indent=_indent_width(line),
                )
                consumed += following
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
                    raw_args=raw_args,
                    input=prompt_input,
                    include=include,
                    prompt_stack=prompt_stack,
                    depth=depth + 1,
                ),
            )
            if line.endswith(("\n", "\r")):
                _append_part(output, TextPart("\n"))
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


def _prompt_call(
    line: str,
) -> tuple[str, str, str, bool] | None:
    match = _PROMPT_CALL_RE.fullmatch(line)
    if match is None:
        return None
    prompt_name = match.group(1)
    raw = match.group(2) or ""
    raw_args, attached, has_colon = _split_unquoted_colon(raw)
    return prompt_name, raw_args.strip(), attached.strip(), has_colon


def _split_unquoted_colon(value: str) -> tuple[str, str, bool]:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character in {'"', "'"}:
            quote = None if quote == character else character if quote is None else quote
            continue
        if character == ":" and quote is None:
            return value[:index], value[index + 1 :], True
    return value, "", False


def _indented_body(
    lines: Sequence[str],
    *,
    base_indent: int,
) -> tuple[str, int]:
    selected: list[str] = []
    for line in lines:
        if line.strip() and _indent_width(line) <= base_indent:
            break
        selected.append(line)
    return textwrap.dedent("".join(selected)).strip("\n"), len(selected)


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
        raise ToolangError(
            f"{boundary} is not valid {type_name}: {exc.msg}"
        ) from exc


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


def _indent_width(value: str) -> int:
    return len(value) - len(value.lstrip(" "))


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
    positionals: list[str] = []
    known = {param.name for param in params}
    for token in tokens:
        if "=" in token:
            candidate, value = token.split("=", 1)
            if candidate in known:
                if candidate in bindings:
                    raise ToolangError(
                        f"Duplicate prompt argument {candidate!r} "
                        f"for /{prompt_name}."
                    )
                bindings[candidate] = value
                continue
        positionals.append(token)

    positional_index = 0
    for param in params:
        if param.name in bindings:
            continue
        if positional_index < len(positionals):
            bindings[param.name] = positionals[positional_index]
            positional_index += 1
            continue
        if param.optional:
            bindings[param.name] = ""
            continue
        raise ToolangError(
            f"Missing required prompt argument {param.name!r} "
            f"for /{prompt_name}."
        )

    if positional_index < len(positionals):
        raise ToolangError(f"Too many prompt arguments for /{prompt_name}.")
    return bindings

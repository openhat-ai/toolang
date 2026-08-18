"""Convert public HTTP schemas into core runtime values."""

from fastapi import HTTPException

from toolang.base.types.message import Message, Part
from .schemas import InputMessagePayload, InputPart


def parse_user_message(payload: InputMessagePayload) -> Message:
    """Parse and validate one user-authored API message."""

    try:
        message = Message.from_data(
            payload.model_dump(mode="python", exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if message.role != "user":
        raise HTTPException(status_code=422, detail="input message role must be user")
    return message


def parse_parts(parts: list[InputPart]) -> tuple[Part, ...]:
    """Parse and validate canonical HTTP input parts."""

    try:
        return Message.from_data(
            {
                "role": "user",
                "parts": [
                    part.model_dump(mode="python", exclude_none=True) for part in parts
                ],
            }
        ).parts
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

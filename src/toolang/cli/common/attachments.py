"""Capture client-side file attachments for authored remote requests."""

from dataclasses import replace
from pathlib import Path

import httpx

from toolang.execution.schemas import RunRequest
from toolang.common.errors import ToolangError
from toolang.lang.includes import resolve_file_include


async def capture_attachments(
    http: httpx.AsyncClient, endpoint: str, request: RunRequest, *, procdir: Path
) -> RunRequest:
    # Plain Content cannot introduce a file include without @ or a prompt call.
    if not any(
        "@" in source or "$" in source for source in request.runnable.input.values()
    ):
        return replace(request, attachments={})
    response = await http.post(
        f"{endpoint.rstrip('/')}/api/v1/runs/input-references",
        json={"runnable": request.runnable.ref, "input": dict(request.runnable.input)},
    )
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ToolangError(f"could not prepare remote inputs: {detail}")
    payload = response.json()
    references = payload.get("references")
    revision = payload.get("state")
    if (
        not isinstance(revision, str)
        or not isinstance(references, list)
        or not all(isinstance(item, str) for item in references)
    ):
        raise ValueError("invalid remote input references")
    return replace(
        request,
        source_revision=revision,
        attachments={
            reference: resolve_file_include(reference, base=procdir)
            for reference in references
        },
    )

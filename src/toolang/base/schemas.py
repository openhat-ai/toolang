"""Wire shapes shared by runtime tools and their consumers."""

from typing import TypedDict


class RecallControlSummary(TypedDict):
    ref: str
    target: dict[str, str]
    revision: str


class ReloadControlSummary(TypedDict):
    ref: str
    state: str


class CompactControlSummary(TypedDict):
    ref: str
    horizon: str


ControlSummary = RecallControlSummary | ReloadControlSummary | CompactControlSummary

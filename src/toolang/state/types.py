"""Shared capability-state vocabulary and scalar types."""

from typing import Literal


CapScope = Literal["root", "home", "here"]
EntryKind = Literal["psyche", "skill", "service", "prompt"]
EntryShape = Literal["file", "dir"]
SourceOrigin = Literal["local", "remote"]
CapForm = Literal["authored", "inline", "configured", "referenced"]
ProgramKind = Literal["agent", "flow"]
RunnableKind = Literal["agic", "flow"]

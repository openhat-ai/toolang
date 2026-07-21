"""Shared capability-state vocabulary and scalar types."""

from typing import Literal


PreparedVisibility = Literal["shared", "private"]
EntryKind = Literal["psyche", "skill", "service", "prompt"]
EntryShape = Literal["file", "dir"]
SourceOrigin = Literal["local", "remote"]
SourceForm = Literal["inline", "ref", "wired", "file"]
Visibility = PreparedVisibility
EntryOrigin = SourceOrigin
EntryForm = SourceForm
EntryScope = Literal["root", "home", "here"]

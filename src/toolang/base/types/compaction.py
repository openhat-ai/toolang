"""Concrete compaction results shared by tools and execution."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """A summary covering the half-open root range [begin, end)."""

    thread: str
    begin: str
    end: str
    summary: str

    def __post_init__(self) -> None:
        for name in ("thread", "begin", "end", "summary"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"compact {name} must contain nonempty text")
        if self.begin == self.end:
            raise ValueError("compact range must be nonempty")

    def to_data(self) -> dict[str, str]:
        return asdict(self)

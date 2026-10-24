"""Parse explicit home selectors and resolve their verified ownership."""

from dataclasses import dataclass
from pathlib import Path

from toolang.common.place_codes import root_code, decode_place_code
from toolang.common import layout as paths
from toolang.common.layout import AgentLayout, derive_agent_name, validate_agent_name
from .allocation import select_roaming
from .identity import layout_from_control, read_home_identity
from .instance import read_instance
from .private_paths import verify_root_namespace, verify_home_paths


@dataclass(frozen=True, slots=True)
class AgentAddress:
    name: str
    place_code: str
    source: Path | None = None

    def __post_init__(self) -> None:
        validate_agent_name(self.name)
        _, timestamp = decode_place_code(self.place_code)
        if self.source is not None and (
            not self.source.is_absolute()
            or self.source.suffix != ".too"
            or timestamp is None
        ):
            raise ValueError(
                "a source-path instance requires an absolute .too path and a roaming code"
            )

    @property
    def placement(self) -> str:
        return (
            "visiting" if decode_place_code(self.place_code)[1] is None else "roaming"
        )


def parse_address(text: str) -> AgentAddress | None:
    """Remote schemes keep their own revisions and URL @ characters."""
    explicit_name = text.startswith("agent:")
    if explicit_name:
        text = text.removeprefix("agent:")
    elif ":" in text.split("/", 1)[0]:
        return None
    if "@" not in text or text.endswith(".too"):
        return None
    label, _, token = text.rpartition("@")
    _, timestamp = decode_place_code(token)
    source = None
    if not explicit_name and (label.endswith(".too") or "/" in label):
        source = Path(label).expanduser().resolve()
        if source.suffix != ".too" or timestamp is None:
            raise ValueError(
                "a source-path instance requires a .too path and a roaming code"
            )
        name = derive_agent_name(source.stem)
    else:
        name = validate_agent_name(label)
    return AgentAddress(name, token, source)


def resolve_address(root: Path, address: AgentAddress) -> tuple[AgentLayout, str]:
    root = root.expanduser().resolve()
    if address.source is not None:
        layout = select_roaming(address.source, address.place_code, toolang_root=root)
    elif address.placement == "roaming":
        from .discovery import active_layout

        layout, launch_id = active_layout(root, f"{address.name}@{address.place_code}")
        return layout, launch_id
    else:
        namespace = paths.VISITING_BASE / root_code(str(root))
        verify_root_namespace(namespace, root)
        home = namespace / address.place_code / "agents" / address.name
        verify_home_paths(namespace / address.place_code, address.name)
        entry = read_instance(home / ".runtime" / "status.json")
        if entry is None:
            raise FileNotFoundError(
                f"instance not found: {address.name}@{address.place_code}"
            )
        layout = layout_from_control(entry["control"])
        if layout.home != home or layout.root != root:
            raise ValueError("instance does not match the selected agent")
        return layout, entry["launch_id"]
    entry = read_home_identity(layout, toolang_root=root)
    assert entry is not None
    return layout, entry["launch_id"]

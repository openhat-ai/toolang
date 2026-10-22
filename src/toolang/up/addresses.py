"""Parse explicit home selectors and resolve their verified ownership."""

from dataclasses import dataclass
from pathlib import Path

from toolang.common.instance_ids import area_id, decode_instance_id
from toolang.common import layout as paths
from toolang.common.layout import AgentLayout, derive_agent_name, validate_agent_name
from .identity import layout_from_control, read_home_identity, select_roaming
from .instance import read_instance
from .private_paths import verify_area, verify_home_paths


@dataclass(frozen=True, slots=True)
class AgentAddress:
    name: str
    instance_id: str
    source: Path | None = None

    def __post_init__(self) -> None:
        validate_agent_name(self.name)
        decode_instance_id(self.instance_id)
        if self.source is not None and (
            not self.source.is_absolute()
            or self.source.suffix != ".too"
            or not self.instance_id.startswith("s")
        ):
            raise ValueError(
                "a source-path instance requires an absolute .too path and an s ID"
            )

    @property
    def placement(self) -> str:
        return "visiting" if self.instance_id.startswith("h") else "roaming"


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
    decode_instance_id(token)
    source = None
    if not explicit_name and (label.endswith(".too") or "/" in label):
        source = Path(label).expanduser().resolve()
        if source.suffix != ".too" or not token.startswith("s"):
            raise ValueError("a source-path instance requires a .too path and an s ID")
        name = derive_agent_name(source.stem)
    else:
        name = validate_agent_name(label)
    return AgentAddress(name, token, source)


def resolve_address(root: Path, address: AgentAddress) -> tuple[AgentLayout, str]:
    root = root.expanduser().resolve()
    if address.source is not None:
        layout = select_roaming(address.source, address.instance_id, toolang_root=root)
    elif address.instance_id.startswith("s"):
        from .observation import active_layout

        layout, launch_id = active_layout(root, f"{address.name}@{address.instance_id}")
        return layout, launch_id
    else:
        area = paths.VISITING_ROOT / area_id(str(root))
        verify_area(area, root)
        home = area / address.instance_id / "agents" / address.name
        verify_home_paths(area / address.instance_id, address.name)
        entry = read_instance(home / ".runtime" / "status.json")
        if entry is None:
            raise FileNotFoundError(
                f"instance not found: {address.name}@{address.instance_id}"
            )
        layout = layout_from_control(entry["control"])
        if layout.home != home or layout.toolang_root != root:
            raise ValueError("instance does not match the selected agent")
        return layout, entry["launch_id"]
    entry = read_home_identity(layout, toolang_root=root)
    assert entry is not None
    return layout, entry["launch_id"]

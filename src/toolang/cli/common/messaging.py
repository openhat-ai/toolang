"""Hub discovery and stable context identities for human messaging clients."""

from hashlib import sha256
from pathlib import Path

from toolang.teaming.schemas import HubConnection
from toolang.up.hub import HubProcess


def talk_identity(root: Path, connection: str, human: str) -> str:
    """Stable dataset/viewer key for Talk drafts, independent of the endpoint."""
    return sha256(f"{root.resolve()}\0{connection}\0{human}".encode()).hexdigest()[:20]


def talk_context(root: Path, connection: str, human: str, endpoint: str) -> str:
    """Endpoint-specific pane context shared by Talk and team Top."""
    identity = talk_identity(root, connection, human)
    return sha256(f"{identity}\0{endpoint}".encode()).hexdigest()[:20]


def settings(root: Path) -> tuple[HubConnection, str]:
    config = HubProcess(root).connection()
    return config, config.human

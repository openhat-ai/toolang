"""Create root-owned configuration fixtures at their root."""

from pathlib import Path


def root_file(root: Path, name: str) -> Path:
    directory = root
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def capture_guest_source(host, guest, *, state_revision=None):
    """Model the ordinary read-only guest source.json launch input."""
    import json
    from toolang.up.mounts import prepare_guest_source

    data = (
        json.loads(host.root_source_receipt.read_bytes())
        if host.root_source_receipt.is_file()
        else {}
    )
    data["launch"] = prepare_guest_source(host, state_revision=state_revision)
    path = guest.root_source_receipt
    if path.is_symlink():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))

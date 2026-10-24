"""Stage the fixed Toolang bootstrap used by Docker guests."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
import shutil
import json

from toolang.base.types.sandbox import SandboxRequest
from toolang.common.files import atomic_write_text
from toolang.common.environment import is_environment_name


_GUEST_FILES = {"guest.sh": 0o755}


def stage_guest_files(directory: Path) -> None:
    """Copy the fixed guest bootstrap files into one immutable launch stage."""

    package = files("toolang.plugin.sandboxes.docker")
    for name, mode in _GUEST_FILES.items():
        path = directory / name
        with package.joinpath(name).open("rb") as reader, path.open("wb") as writer:
            shutil.copyfileobj(reader, writer)
        path.chmod(mode)


def write_guest_env(
    path: Path,
    *,
    dotenv_envs: Mapping[str, str],
    process_envs: Mapping[str, str],
) -> None:
    """Write the restricted generated dotenv consumed by the guest core."""

    content = _dotenv_section(
        "Explicit sandbox bindings",
        {
            name: value
            for name, value in dotenv_envs.items()
            if name not in process_envs
        },
    )
    content += "\n"
    content += _dotenv_section("Resolved workload values", process_envs)
    atomic_write_text(path, content)
    path.chmod(0o600)


def validate_guest_environment(environ: Mapping[str, str]) -> None:
    for name, value in environ.items():
        _dotenv_name(name)
        _dotenv_value(value)


def prepare_stage_directory(stage_dir: Path) -> None:
    stage_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir()


def prepare_diagnostic(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "")
    path.chmod(0o600)


def remove_stage_directory(
    stage_dir: str | Path,
    *,
    ignore_errors: bool = False,
    launch_id: str | None = None,
) -> None:
    path = Path(stage_dir)
    if launch_id is not None and path.name == "docker":
        try:
            info_path = path / "active/info.json"
            if not info_path.exists():
                info_path = path / "info.json"
            info = json.loads(info_path.read_text())
        except (OSError, ValueError):
            # A preparation failure may leave only an exclusively created empty dir.
            try:
                path.rmdir()
            except OSError:
                pass
            return
        if not isinstance(info, dict) or info.get("launch") != launch_id:
            return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=ignore_errors)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if not ignore_errors:
                raise


def prepare_background_log(request: SandboxRequest) -> Path | None:
    """Validate and create the durable log used by background workloads."""

    if request.output == "inherit":
        if request.log_path is not None:
            raise ValueError("inherited docker output does not accept a log path")
        return None
    if request.output != "file":
        raise ValueError(f"unsupported docker output mode: {request.output}")
    log_path = request.log_path
    if log_path is None:
        raise ValueError("docker file output requires a log path")
    resolved_home = request.local_home.resolve()
    resolved_log = log_path.resolve()
    try:
        resolved_log.relative_to(resolved_home)
    except ValueError as exc:
        raise ValueError("docker background log must be inside the agent home") from exc
    resolved_log.parent.mkdir(parents=True, exist_ok=True)
    resolved_log.touch(mode=0o600, exist_ok=True)
    resolved_log.chmod(0o600)
    relative_log = resolved_log.relative_to(resolved_home)
    return request.hosted_home / relative_log


def _dotenv_section(title: str, environ: Mapping[str, str]) -> str:
    return f"# {title}\n" + "".join(
        f'{_dotenv_name(name)}="{_dotenv_value(value)}"\n'
        for name, value in sorted(environ.items())
    )


def _dotenv_name(name: str) -> str:
    if not is_environment_name(name):
        raise ValueError(f"invalid guest environment variable name: {name!r}")
    return name


def _dotenv_value(value: str) -> str:
    if "\x00" in value:
        raise ValueError("guest environment variable values must not contain NUL")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r")

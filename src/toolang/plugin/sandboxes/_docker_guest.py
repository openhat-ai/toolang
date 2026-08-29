"""Stage files used by the Docker guest core."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
import shutil

from toolang.base.types.sandbox import SandboxRequest
from toolang.common.files import atomic_write_text


def write_guest_script(path: Path) -> None:
    """Copy the packaged Linux guest core into one launch stage."""

    source = files("toolang.plugin.sandboxes").joinpath("docker_guest.sh")
    atomic_write_text(path, source.read_text(encoding="utf-8"))
    path.chmod(0o755)


def write_guest_env(
    path: Path,
    *,
    dotenv_envs: Mapping[str, str],
    process_envs: Mapping[str, str],
) -> None:
    content = _dotenv_section("Root and agent dotenv values", dotenv_envs)
    content += "\n"
    content += _dotenv_section("Filtered host process values", process_envs)
    atomic_write_text(path, content)
    path.chmod(0o600)


def validate_guest_environment(environ: Mapping[str, str]) -> None:
    for name, value in environ.items():
        _dotenv_name(name)
        _dotenv_value(value)


def prepare_stage_directory(stage_dir: Path) -> None:
    stage_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir()


def prepare_sandbox_instance(path: Path) -> None:
    atomic_write_text(path, "")
    path.chmod(0o600)


def write_sandbox_instance(path: str | Path, instance: str) -> None:
    value = instance.strip()
    if not value or value != instance:
        raise ValueError("docker sandbox instance must be a nonempty token")
    target = Path(path)
    atomic_write_text(target, value + "\n")
    target.chmod(0o600)


def prepare_diagnostic_log(
    request: SandboxRequest,
    launch_id: str,
) -> tuple[Path, Path]:
    relative = Path(".runtime") / f"sandbox-bootstrap-{launch_id}.log"
    local_path = request.local_home / relative
    local_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(local_path, "")
    local_path.chmod(0o600)
    return local_path, request.hosted_home / relative


def remove_diagnostic_log(path: str | Path, *, ignore_errors: bool = False) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        if not ignore_errors:
            raise


def remove_stage_directory(
    stage_dir: str | Path,
    *,
    ignore_errors: bool = False,
) -> None:
    path = Path(stage_dir)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=ignore_errors)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            if not ignore_errors:
                raise


def prepare_background_log(request: SandboxRequest) -> Path | None:
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
        relative = resolved_log.relative_to(resolved_home)
    except ValueError as exc:
        raise ValueError("docker background log must be inside the agent home") from exc
    resolved_log.parent.mkdir(parents=True, exist_ok=True)
    resolved_log.touch(mode=0o600, exist_ok=True)
    resolved_log.chmod(0o600)
    return request.hosted_home / relative


def _dotenv_section(title: str, environ: Mapping[str, str]) -> str:
    return f"# {title}\n" + "".join(
        f'{_dotenv_name(name)}="{_dotenv_value(value)}"\n'
        for name, value in sorted(environ.items())
    )


def _dotenv_name(name: str) -> str:
    if not name or any(char.isspace() or char in "=#\x00" for char in name):
        raise ValueError(f"invalid guest environment variable name: {name!r}")
    return name


def _dotenv_value(value: str) -> str:
    if "\x00" in value:
        raise ValueError("guest environment variable values must not contain NUL")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r")

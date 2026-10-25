"""Host and inspect a local Hub independently of agent processes."""

from __future__ import annotations

from collections.abc import Sequence
import fcntl
import os
from pathlib import Path
import socket
import subprocess
import time

import httpx
import psutil
import uvicorn

from toolang.common.version import toolang_version
from .config import HubServerConfig
from toolang.teaming.errors import TeamingError
from .records import HubRecord


class HubProcess:
    def __init__(self, root: Path) -> None:
        self.path = root / "hub" / "hub.json"
        self.lock = root / "hub" / "hub.lock"
        self.log = root / "hub" / "hub.log"

    @staticmethod
    def process(record: HubRecord) -> psutil.Process | None:
        try:
            process = psutil.Process(record.pid)
            if (
                process.create_time() == record.created
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                return process
        except psutil.NoSuchProcess:
            pass
        return None

    def current(self) -> HubRecord | None:
        record = HubRecord.load(self.path)
        return (
            record if record is not None and self.process(record) is not None else None
        )

    @staticmethod
    def ready(record: HubRecord) -> bool:
        if record.status == "starting":
            return False
        try:
            with httpx.Client(trust_env=False, timeout=1) as client:
                response = client.get(
                    record.connection.endpoint + "/healthz",
                )
            return response.status_code == 200 and response.json() == {"ok": True}
        except (httpx.HTTPError, ValueError):
            return False

    def _clear(self, expected: HubRecord, *, locked: bool = False) -> None:
        def remove_owned() -> None:
            current = HubRecord.load(self.path)
            if current is not None and (current.pid, current.created) == (
                expected.pid,
                expected.created,
            ):
                self.path.unlink(missing_ok=True)

        if locked:
            remove_owned()
            return
        current = HubRecord.load(self.path)
        if current is None or (current.pid, current.created) != (
            expected.pid,
            expected.created,
        ):
            return
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TeamingError(
                    "Hub process exited but its record could not be cleaned"
                ) from exc
            remove_owned()

    def start(self, command: Sequence[str], *, timeout: float = 20) -> HubRecord:
        if self.current() is not None:
            raise TeamingError("Hub is already running; stop it before restarting")
        stale = HubRecord.load(self.path)
        if stale is not None:
            self._clear(stale)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("ab") as log:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        try:
            created = psutil.Process(child.pid).create_time()
        except psutil.NoSuchProcess:
            created = None
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise TeamingError(f"Hub startup failed; see {self.log}")
                record = self.current()
                if (
                    record is not None
                    and (record.pid, record.created) == (child.pid, created)
                    and self.ready(record)
                ):
                    return record
                time.sleep(0.05)
            raise TeamingError(f"Hub startup timed out; see {self.log}")
        except BaseException:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            failed = HubRecord.load(self.path)
            if failed is not None and (failed.pid, failed.created) == (
                child.pid,
                created,
            ):
                self._clear(failed)
            raise

    def stop(self, *, force: bool = False) -> bool:
        record = HubRecord.load(self.path)
        process = self.process(record) if record is not None else None
        if process is None:
            if record is not None:
                self._clear(record)
            return False
        try:
            process.terminate()
            process.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired as exc:
            if not force:
                raise TeamingError(
                    "Hub did not stop; use 'too hub stop --force'"
                ) from exc
            try:
                process.kill()
                process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        assert record is not None
        self._clear(record)
        return True


def serve(root: Path, config: HubServerConfig, *, port: int) -> int:
    """Serve one root with a lifetime lock and an explicitly bound socket."""
    from toolang.teaming.api import create_app

    root = root.expanduser().resolve(strict=False)
    if config.backend.root != str(root):
        raise TeamingError("Hub backend configuration belongs to another Toolang root")
    hub = HubProcess(root)
    hub.lock.parent.mkdir(parents=True, exist_ok=True)
    with hub.lock.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TeamingError("Hub is already running or starting") from exc
        if hub.current() is not None:
            raise TeamingError("Hub is already running")
        with socket.socket(
            socket.AF_INET6 if ":" in config.host else socket.AF_INET,
            socket.SOCK_STREAM,
        ) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind((config.host, port))
                # Bind alone allows a competing listener during startup on Linux.
                listener.listen()
            except OSError as exc:
                raise TeamingError(f"Cannot bind Hub port {port}: {exc}") from exc
            record = HubRecord(
                pid=os.getpid(),
                created=psutil.Process().create_time(),
                port=port,
                host=config.host,
                status="starting",
            )

            def publish_ready() -> None:
                nonlocal record
                ready_record = record.model_copy(update={"status": "running"})
                ready_record.save(hub.path)
                record = ready_record

            try:
                record.save(hub.path)
                app = create_app(
                    config.backend,
                    version=toolang_version(),
                    on_ready=publish_ready,
                    on_stop=lambda: hub._clear(record, locked=True),
                )
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host=config.host,
                        port=port,
                        access_log=False,
                        timeout_graceful_shutdown=5,
                    )
                )
                try:
                    server.run(sockets=[listener])
                except SystemExit as exc:
                    return int(exc.code) if isinstance(exc.code, int) else 1
                return 0 if server.started else 3
            finally:
                hub._clear(record, locked=True)

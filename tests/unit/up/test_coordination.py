"""Independent processes coordinate shared runtime status and discovery files."""

from contextlib import contextmanager, ExitStack
import json
import select
import subprocess
import sys

from toolang.common.layout import AgentLayout
from toolang.up.allocation import select_roaming
from toolang.up.instance import patch_workload, read_instance, update_instance
from toolang.up.registry import RuntimeRegistration, registration_lock
from toolang.up.workload import ForegroundWorkload


@contextmanager
def worker(code, *args):
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, *map(str, args)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream is not None
            stream.close()


def message(process):
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 5)[0], "worker did not respond"
    value = process.stdout.readline().decode().strip()
    assert value, process.communicate(timeout=5)[1].decode()
    return value


def resume(process):
    assert process.stdin is not None
    process.stdin.write(b"\n")


def finish(process):
    output, error = process.communicate(timeout=5)
    assert process.returncode == 0, error.decode()
    return output.decode().strip()


def test_processes_merge_status_fields_without_lost_updates(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    update_instance(
        layout.runtime_status, lambda entry: entry.update(launch_id="shared")
    )
    code = """
import sys
from pathlib import Path
from toolang.up.instance import update_instance
path, key = Path(sys.argv[1]), sys.argv[2]
print('ready', flush=True)
sys.stdin.readline()
for number in range(40):
    assert update_instance(
        path, lambda entry: entry['workload'].update({key: number}), launch_id='shared'
    )
"""
    with ExitStack() as stack:
        writers = [
            stack.enter_context(worker(code, layout.runtime_status, f"writer{index}"))
            for index in range(4)
        ]
        assert [message(process) for process in writers] == ["ready"] * 4
        for process in writers:
            resume(process)
        for process in writers:
            finish(process)
    entry = read_instance(layout.runtime_status)
    assert entry is not None
    assert entry["workload"] == {f"writer{index}": 39 for index in range(4)}


def test_process_management_ownership_serializes_without_blocking_status_updates(
    tmp_path,
):
    code = """
import sys
from pathlib import Path
from toolang.common.layout import AgentLayout
from toolang.up.workload import management_operation
reported = False
def waiting():
    global reported
    if not reported:
        reported = True
        print('waiting', flush=True)
print('ready', flush=True)
with management_operation(
    AgentLayout.resident(Path(sys.argv[1]), 'alice'), stage='start', on_wait=waiting
):
    print('entered', flush=True)
    sys.stdin.readline()
"""
    layout = AgentLayout.resident(tmp_path, "alice")
    with worker(code, tmp_path) as first:
        assert message(first) == "ready"
        assert message(first) == "entered"
        entry = read_instance(layout.runtime_status)
        assert entry is not None
        assert patch_workload(layout.runtime_status, entry["launch_id"], {"pid": 123})
        with worker(code, tmp_path) as second:
            assert message(second) == "ready"
            assert message(second) == "waiting"
            resume(first)
            finish(first)
            assert message(second) == "entered"
            resume(second)
            finish(second)
    entry = read_instance(layout.runtime_status)
    assert entry is not None and "operation" not in entry["control"]
    assert entry["workload"]["pid"] == 123


def test_old_process_cleanup_preserves_a_replacement_registration(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    code = """
import sys
from pathlib import Path
from toolang.common.layout import AgentLayout
from toolang.up.registry import RuntimeRegistration
registration = RuntimeRegistration(
    AgentLayout.resident(Path(sys.argv[1]), 'alice'),
    toolang_root=Path(sys.argv[1]), launch_id=sys.argv[2]
)
print('ready', flush=True)
registration.close()
"""
    with ForegroundWorkload(layout, toolang_root=tmp_path, drivers=frozenset()) as old:
        with ExitStack() as stack:
            with registration_lock(tmp_path, layout):
                cleanup = stack.enter_context(worker(code, tmp_path, old.token))
                assert message(cleanup) == "ready"
                update_instance(
                    layout.runtime_status,
                    lambda entry: entry.update(launch_id="replacement"),
                )
                replacement = RuntimeRegistration(
                    layout, toolang_root=tmp_path, launch_id="replacement"
                ).publish()
            finish(cleanup)
        assert json.loads(old.path.read_text())["launch_id"] == "replacement"
    assert replacement.path.exists()
    replacement.close()


def test_process_allocations_with_the_same_clock_have_distinct_retained_homes(tmp_path):
    source = tmp_path / "aid.too"
    root = tmp_path / "root"
    code = """
import sys
from pathlib import Path
from toolang.up import allocation
allocation.time.time_ns = lambda: 100_000_000
print('ready', flush=True)
sys.stdin.readline()
layout = allocation.allocate_roaming(Path(sys.argv[1]), toolang_root=Path(sys.argv[2]))
print(layout.place_code, flush=True)
"""
    with ExitStack() as stack:
        allocators = [stack.enter_context(worker(code, source, root)) for _ in range(4)]
        assert [message(process) for process in allocators] == ["ready"] * 4
        for process in allocators:
            resume(process)
        codes = [finish(process) for process in allocators]
    assert len(set(codes)) == 4
    for code in codes:
        layout = select_roaming(source, code, toolang_root=root)
        assert layout.runtime_status.is_file()
        assert (
            root / ".runtime" / "active" / layout.address / f"{layout.address}.json"
        ).is_file()
    assert select_roaming(source, toolang_root=root).place_code == max(codes)

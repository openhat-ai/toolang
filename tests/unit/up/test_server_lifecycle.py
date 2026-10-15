"""Retention is independent of transport readers, drivers, and durable history."""

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from toolang.up.lifecycle import LifecycleConflict, ServerLifecycle


def test_final_release_consumes_startup_hold_and_elects_only_once():
    lifecycle = ServerLifecycle("launch", "automatic", clock=lambda: 1.0)
    lifecycle.ready()
    lifecycle.lease("launch", "first")
    lifecycle.lease("launch", "second")
    lifecycle.release("launch", "first")
    assert not lifecycle.elect_exit(lambda: False)
    lifecycle.release("launch", "second")
    lifecycle.release("launch", "second")
    assert lifecycle.elect_exit(lambda: False)
    assert not lifecycle.elect_exit(lambda: False)
    with pytest.raises(LifecycleConflict, match="closing"):
        lifecycle.lease("launch", "new")
    with pytest.raises(LifecycleConflict, match="closing"):
        with lifecycle.admit():
            pytest.fail("closing servers cannot acknowledge new work")


def test_killed_client_expires_but_pending_and_finishing_work_retains_owner():
    now = [0.0]
    lifecycle = ServerLifecycle("launch", "automatic", clock=lambda: now[0])
    lifecycle.ready()
    lifecycle.lease("launch", "killed")
    now[0] = 16.0
    assert not lifecycle.elect_exit(lambda: True)
    with lifecycle.admit():
        assert not lifecycle.elect_exit(lambda: False)
    assert lifecycle.elect_exit(lambda: False)


def test_startup_failure_without_registration_eventually_reclaims_server():
    now = [0.0]
    lifecycle = ServerLifecycle("launch", "automatic", clock=lambda: now[0])
    assert not lifecycle.elect_exit(lambda: False)
    lifecycle.ready()
    now[0] = 29.0
    assert not lifecycle.elect_exit(lambda: False)
    now[0] = 30.0
    assert lifecycle.elect_exit(lambda: False)


def test_old_generation_cannot_release_promote_or_admit_on_replacement():
    lifecycle = ServerLifecycle("new", "automatic")
    lifecycle.ready()
    lifecycle.lease("new", "same-client")
    lifecycle.release("old", "same-client")
    assert not lifecycle.elect_exit(lambda: False)
    for action in (
        lambda: lifecycle.lease("old", "same-client"),
        lambda: lifecycle.promote("old"),
    ):
        with pytest.raises(LifecycleConflict, match="changed"):
            action()
    with pytest.raises(LifecycleConflict, match="changed"):
        with lifecycle.admit("old"):
            pytest.fail("stale mutation reached the replacement")


def test_promotion_wins_before_exit_and_is_idempotent():
    mirrored = []
    lifecycle = ServerLifecycle(
        "launch", "automatic", on_promote=lambda: mirrored.append(True)
    )
    lifecycle.ready()
    lifecycle.lease("launch", "client")
    lifecycle.promote("launch")
    lifecycle.promote("launch")
    lifecycle.release("launch", "client")
    assert mirrored == [True, True]
    assert lifecycle.lifetime == "persistent"
    assert not lifecycle.elect_exit(lambda: False)


def test_foreground_is_never_detached_or_reclaimed():
    lifecycle = ServerLifecycle("launch", "foreground")
    lifecycle.ready()
    assert not lifecycle.elect_exit(lambda: False)
    with pytest.raises(LifecycleConflict, match="foreground"):
        lifecycle.promote("launch")


@pytest.mark.parametrize("operation", ["lease", "promote"])
def test_retention_racing_exit_has_one_winner(operation):
    lifecycle = ServerLifecycle("launch", "automatic")
    lifecycle.ready()
    lifecycle.lease("launch", "bootstrap")
    lifecycle.release("launch", "bootstrap")
    barrier = Barrier(2)

    def retain():
        barrier.wait()
        try:
            if operation == "lease":
                lifecycle.lease("launch", "new")
            else:
                lifecycle.promote("launch")
            return True
        except LifecycleConflict:
            return False

    def exit_idle():
        barrier.wait()
        return lifecycle.elect_exit(lambda: False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        retained = pool.submit(retain)
        exited = pool.submit(exit_idle)
        assert retained.result() != exited.result()

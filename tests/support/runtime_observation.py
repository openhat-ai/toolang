"""Build source bindings and read results for runtime observation tests."""

import json

from toolang.common.layout import AgentLayout
from toolang.stats.schemas import CompletedRoots, CompletionCounts, RecentUsage
from toolang.stats.types import Metrics, timestamp
from toolang.up.observation import RuntimeSource
from toolang.up.instance import AGENT_INSTANCE_FORMAT
from toolang.up.types import RuntimeInstance

NOW = 1792326877


def completed(at=NOW, recent=300):
    return CompletedRoots(
        observed_at=timestamp(at),
        start=timestamp(at - recent),
        end=timestamp(at),
        counts=CompletionCounts(),
        roots=(),
    )


def usage(period=3600):
    return RecentUsage(
        observed_at=timestamp(NOW),
        start=timestamp(NOW - period),
        end=timestamp(NOW),
        metrics=Metrics(model_calls=76),
    )


def binding(tmp_path, *, registered=False):
    registration = tmp_path / ".runtime" / "instances" / "alice.json"
    if registered:
        registration.parent.mkdir(parents=True, exist_ok=True)
        registration.write_text(
            json.dumps(
                {
                    "version": AGENT_INSTANCE_FORMAT,
                    "status": str(
                        AgentLayout.resident(tmp_path, "alice").runtime_status
                    ),
                    "launch_id": "fixture",
                }
            )
        )
    return RuntimeSource(
        RuntimeInstance("alice", "resident", None, "source", "running", ()),
        AgentLayout.resident(tmp_path, "alice"),
        "http://localhost:7001",
        registration,
        "fixture",
    )

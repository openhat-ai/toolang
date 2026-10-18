"""Project durable invocation facts using the execution package's codecs."""

import json
from collections.abc import Mapping
from typing import Any

from toolang.base.money import cost_units
from toolang.execution.accounting import selected_cost_is_approximate, selected_usd_cost
from toolang.execution.records import (
    StoredModelStepSpec,
    step_report_from_data,
    stored_step_spec_from_data,
)
from toolang.execution.types import ModelStepReport, ToolStepReport

from .types import Contribution, Metrics, seconds


def project_root(row: Mapping[str, Any]) -> list[Contribution]:
    if row["started_at"] is None:
        return []
    return [
        Contribution(
            seconds(row["started_at"]),
            row["id"],
            row["thread"],
            "",
            Metrics(root_runs=1),
        )
    ]


def project_step(row: Mapping[str, Any], root: str, thread: str) -> list[Contribution]:
    kind = row["kind"]
    if kind not in ("model", "tool") or row["finished_at"] is None:
        return []
    report = step_report_from_data(kind, json.loads(row["report"]))
    if not isinstance(report, (ModelStepReport, ToolStepReport)) or not report.invoked:
        return []
    if isinstance(report, ToolStepReport):
        return [
            Contribution(
                seconds(row["started_at"]), root, thread, "", Metrics(tool_calls=1)
            )
        ]
    spec = stored_step_spec_from_data("model", json.loads(row["spec"]))
    assert isinstance(spec, StoredModelStepSpec)
    usage, cost = report.usage, selected_usd_cost(report.cost)
    return [
        Contribution(
            seconds(row["started_at"]),
            root,
            thread,
            spec.model,
            Metrics(model_calls=1),
        ),
        Contribution(
            seconds(row["finished_at"]),
            root,
            thread,
            spec.model,
            Metrics(
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None,
                usage.cache_read_tokens if usage else None,
                cost_units(cost) if cost is not None else None,
                spend_approximate=selected_cost_is_approximate(report.cost),
            ),
        ),
    ]

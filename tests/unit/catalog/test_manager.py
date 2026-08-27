from pathlib import Path

from toolang.catalog import CapsManager, JobsManager
from toolang.common.layout import AgentLayout


def test_catalog_managers_bind_one_agent_layout(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")

    caps = CapsManager(layout)
    jobs = JobsManager(layout)

    assert caps.home_authoring.directory == layout.home
    assert caps.home_configured.config_path == layout.config
    assert caps.root_authoring.directory == layout.root
    assert caps.root_configured.config_path == layout.root_config
    assert jobs.home_authoring.directory == layout.home

"""CRUD over resident agent directories."""

from __future__ import annotations

from pathlib import Path
import shutil

from toolang.agent import local as agents


class AgentCatalog:
    """CRUD over resident agent directories under one Toolang root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list(self) -> tuple[agents.AgentLayout, ...]:
        directory = self.root / "agents"
        if not directory.is_dir():
            return ()
        return tuple(
            self._layout(path.name)
            for path in sorted(directory.iterdir())
            if path.is_dir() and (path / "agent.too").is_file()
        )

    def get(self, name: str) -> agents.AgentLayout | None:
        layout = self._layout(name)
        return layout if layout.program.is_file() else None

    def create(self, name: str, *, template: str = "default") -> agents.AgentLayout:
        home = agents.agent_home(self.root, name)
        if home.exists():
            raise FileExistsError(f"agent already exists: {home}")
        home.mkdir(parents=True, exist_ok=False)
        agents.agent_program_path(self.root, name).write_text(
            agents._default_program_source(name, template_name=template),
            encoding="utf-8",
        )
        return self._layout(name)

    def clone(self, source: str, name: str | None = None) -> agents.AgentLayout:
        selector = agents.parse_agent_selector(source)
        if selector.form == "name":
            if name is None:
                raise ValueError("target name is required when cloning one local agent")
            program = agents._clone_local_agent(self.root, selector.name or "", name)
        else:
            ref = agents.resolve_agent_selector_ref(selector)
            target = name or selector.default_name()
            program = agents.write_agent_program(
                self.root, target, agents.fetch_agent_ref(ref)
            )
        return self._layout(program.parent.name)

    def remove(self, name: str) -> agents.AgentLayout:
        layout = self.get(name)
        if layout is None:
            raise FileNotFoundError(
                f"agent not found: {agents.agent_home(self.root, name)}"
            )
        process = agents.AgentProcess(self.root, name)
        status = process.status(ui_base_url="")
        if status is not None and status.status in {"running", "preparing", "starting"}:
            raise ValueError(f"agent is still active: {name}")
        runtime_pids = process.pids()
        if runtime_pids:
            pids = ", ".join(str(pid) for pid in runtime_pids)
            raise ValueError(f"agent is still active: {name} (pid {pids})")
        shutil.rmtree(layout.home)
        sandbox_stage_dir = agents._sandbox_stage_dir(self.root, name)
        if sandbox_stage_dir.exists():
            shutil.rmtree(sandbox_stage_dir)
        return layout

    def _layout(self, name: str) -> agents.AgentLayout:
        return agents.AgentLayout(
            root=self.root,
            name=name,
            home=agents.agent_home(self.root, name),
            program=agents.agent_program_path(self.root, name),
            room=agents.agent_room(self.root, name),
        )

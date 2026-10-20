"""A short-lived Dulwich repository view for source version inspection."""

from functools import cached_property
from os import PathLike

from dulwich.config import ConfigFile, StackedConfig
from dulwich.objects import ObjectID
from dulwich.refs import Ref
from dulwich.repo import Repo


class VersionRepo(Repo):
    """Read-only view with configuration cached for one source version query."""

    def __init__(self, root: str | bytes | PathLike[str]) -> None:
        # Repo reads configuration before selecting its ref backend.
        self._config_ready = False
        super().__init__(root)
        self._config_ready = True

    def get_refs(self) -> dict[Ref, ObjectID]:
        """Give describe tag refs without reading unrelated ref objects."""
        prefix = Ref(b"refs/tags")
        return {
            Ref(prefix + b"/" + name): oid
            for name, oid in self.refs.as_dict(prefix).items()
        }

    @cached_property
    def _version_config(self) -> ConfigFile:
        return super().get_config()

    def get_config(self) -> ConfigFile:
        if not self._config_ready:
            return super().get_config()
        return self._version_config

    @cached_property
    def _version_config_stack(self) -> StackedConfig:
        return super().get_config_stack()

    def get_config_stack(self) -> StackedConfig:
        if not self._config_ready:
            return super().get_config_stack()
        return self._version_config_stack

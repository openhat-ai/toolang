"""A short-lived Dulwich repository view for source version inspection."""

from functools import cached_property

from dulwich.config import ConfigFile, StackedConfig
from dulwich.objects import ObjectID
from dulwich.refs import Ref
from dulwich.repo import Repo


class VersionRepo(Repo):
    """Scope describe to tags and reuse configuration within one snapshot."""

    def get_refs(self) -> dict[Ref, ObjectID]:
        """Give describe tag refs without reading unrelated ref objects."""
        prefix = Ref(b"refs/tags/")
        return {
            Ref(prefix + name): oid for name, oid in self.refs.as_dict(prefix).items()
        }

    @cached_property
    def _version_config(self) -> ConfigFile:
        return super().get_config()

    def get_config(self) -> ConfigFile:
        return self._version_config

    @cached_property
    def _version_config_stack(self) -> StackedConfig:
        return super().get_config_stack()

    def get_config_stack(self) -> StackedConfig:
        return self._version_config_stack

"""Bridge fakeredis's Lua subcommand dispatch to its normal command handler."""

from fakeredis.commands_mixins.scripting_mixin import ScriptingCommandsMixin


def install_subcommands(monkeypatch):
    original = ScriptingCommandsMixin._lua_redis_call

    def call(self, runtime, globals_, command, *args):
        # fakeredis handles XINFO STREAM through the normal wire parser, but
        # its Lua dispatcher currently treats XINFO as a complete command.
        if command.lower() == b"xinfo" and args:
            command, args = command + b" " + args[0], args[1:]
        return original(self, runtime, globals_, command, *args)

    monkeypatch.setattr(ScriptingCommandsMixin, "_lua_redis_call", call)

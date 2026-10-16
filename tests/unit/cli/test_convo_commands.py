"""Human conversation management and explicit, byte-preserving transfers."""

import asyncio

from typer.testing import CliRunner
import pytest

from tests.unit.cli.test_talk import messaging_cli as messaging_cli
from toolang.cli.toolang import main as cli
from toolang.cli.toolang.commands import convo
from toolang.cli.toolang.commands.talk import directory


@pytest.fixture
def conversations(tmp_path, monkeypatch, messaging_cli):
    # The shared directory fixture already installs an in-process Hub transport.
    monkeypatch.setattr(convo, "HubClient", directory.HubClient)
    monkeypatch.setattr(convo, "settings", directory.settings)

    async def existing():
        async with messaging_cli() as human:
            return (await human.contacts())[0]["conversation"]

    return tmp_path, messaging_cli, asyncio.run(existing())


def invoke(root, *args, input=None):
    return CliRunner().invoke(
        cli.app, ["--root", str(root), "convo", *args], input=input
    )


def test_missing_rules_and_byte_exact_upload_download(conversations):
    root, _, conversation = conversations
    result = invoke(root, "rules", conversation)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "No rules set." in result.stderr and "Revision: 0" in result.stderr
    content = b"# Members\r\n\nKeep literal {{value}} and caf\xc3\xa9.\n"
    source, destination = root / "source.md", root / "download.md"
    source.write_bytes(content)
    result = invoke(root, "rules", conversation, "-u", str(source), "--revision", "0")
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    revision = result.stderr.split("Revision: ")[1].strip()
    result = invoke(root, "rules", conversation, "-o", str(destination))
    assert result.exit_code == 0, result.output
    assert result.stdout == "" and destination.read_bytes() == content
    result = invoke(root, "rules", conversation, "-u", "--revision", revision, input="")
    assert result.exit_code == 0, result.output
    assert "Revision: 0" in result.stderr
    result = invoke(root, "rules", conversation, "-o", str(destination))
    assert result.exit_code == 0 and destination.read_bytes() == b""


def test_pipe_requires_update_and_conflict_preserves_rules_and_read_failure_file(
    conversations,
):
    root, _, conversation = conversations
    result = invoke(root, "rules", conversation, input="Do not upload implicitly.")
    assert result.exit_code == 0 and result.stdout == ""
    result = invoke(
        root,
        "rules",
        conversation,
        "-u",
        "-",
        "--revision",
        "0",
        input="Current rules.",
    )
    assert result.exit_code == 0, result.output
    result = invoke(
        root, "rules", conversation, "-u", "--revision", "0", input="Stale rules."
    )
    assert result.exit_code != 0 and "changed" in result.output
    result = invoke(root, "rules", conversation)
    assert result.stdout == "Current rules."
    destination = root / "keep.md"
    destination.write_bytes(b"keep")
    result = invoke(root, "rules", "gc_00000000", "-o", str(destination))
    assert result.exit_code != 0 and destination.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "flags", [["-u"], ["--revision", "0"], ["-o", "-", "-u", "--revision", "0"]]
)
def test_invalid_transfer_flags_are_usage_errors(conversations, flags):
    root, _, conversation = conversations
    assert invoke(root, "rules", conversation, *flags).exit_code == 2


def test_membership_create_and_shared_list(conversations):
    root, _, _ = conversations
    result = invoke(root, "create", "members")
    assert result.exit_code == 0, result.output
    conversation = result.stdout.strip()
    result = invoke(root, "list")
    assert result.exit_code == 0 and "JOINED" in result.output
    talk = CliRunner().invoke(cli.app, ["--root", str(root), "talk"])
    assert talk.exit_code == 0 and talk.output == result.output
    assert invoke(root, "leave", conversation).exit_code == 0
    assert invoke(root, "join", conversation).exit_code == 0
    result = invoke(root)
    assert "rules" in result.output and "create" in result.output

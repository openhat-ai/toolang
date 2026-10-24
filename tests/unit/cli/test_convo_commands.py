"""Human conversation management and explicit, byte-preserving transfers."""

import asyncio

from typer.testing import CliRunner
from typer._click.utils import strip_ansi
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


def test_convo_help_uses_consistent_short_descriptions():
    result = CliRunner().invoke(cli.app, ["convo", "--help"])
    output = strip_ansi(result.output)

    assert result.exit_code == 0, output
    assert output.startswith("Manage conversations and rules.\n")
    for command, description in (
        ("list", "List conversations"),
        ("create", "Create a group conversation"),
        ("join", "Join a group conversation"),
        ("leave", "Leave a group conversation"),
        ("rules", "Retrieve or update rules"),
    ):
        assert f"  {command:<7} {description}\n" in output
    result = CliRunner().invoke(cli.app, ["convo", "rules", "--help"])
    assert result.exit_code == 0, result.output
    output = " ".join(strip_ansi(result.output).split())
    assert "--output <FILE>" in output
    assert "Write rules to FILE; - for stdout" in output
    assert "--update [FILE]" in output
    assert "Update rules from FILE; omit FILE or use - to read stdin" in output


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


@pytest.mark.parametrize("revision", ["latest", "00", "A" * 64])
@pytest.mark.parametrize("force_color", [False, True])
def test_invalid_revision_fails_before_hub_discovery_or_input(
    conversations, monkeypatch, revision, force_color
):
    root, _, conversation = conversations

    def unexpected(*args):
        pytest.fail("Invalid revisions must not discover the Hub or read input")

    monkeypatch.setattr(convo, "settings", unexpected)
    monkeypatch.setattr(convo.typer, "get_binary_stream", unexpected)
    if force_color:
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
    result = invoke(root, "rules", conversation, "-u", "--revision", revision)
    assert result.exit_code == 2, result.output
    output = strip_ansi(result.output)
    assert "--revision" in output and "SHA-256" in output


@pytest.mark.parametrize(
    "content", [b"x" * 65537, "é".encode() * 32769], ids=["ascii", "multibyte"]
)
def test_oversized_upload_reports_byte_limit_without_changing_rules(
    conversations, content
):
    root, _, conversation = conversations
    source = root / "oversized.md"
    source.write_bytes(content)
    result = invoke(root, "rules", conversation, "-u", str(source), "--revision", "0")
    assert result.exit_code == 1, result.output
    assert "Conversation rules exceed 64 KiB" in result.output
    assert invoke(root, "rules", conversation).stdout == ""


@pytest.mark.parametrize("source", ["file", "stdin"])
def test_invalid_utf8_upload_has_a_concise_error_and_preserves_rules(
    conversations, source
):
    root, _, conversation = conversations
    path = root / "invalid.md"
    path.write_bytes(b"\xff")
    result = invoke(
        root,
        "rules",
        conversation,
        "-u",
        str(path) if source == "file" else "-",
        "--revision",
        "0",
        input=b"\xff" if source == "stdin" else None,
    )
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "Rules input must be UTF-8" in result.stderr
    assert invoke(root, "rules", conversation).stdout == ""


def test_membership_create_and_list(conversations):
    root, _, _ = conversations
    result = invoke(root, "create", "members")
    assert result.exit_code == 0, result.output
    conversation = result.stdout.strip()
    result = invoke(root, "list")
    assert result.exit_code == 0 and "JOINED" in result.output
    talk = CliRunner().invoke(cli.app, ["--root", str(root), "talk"])
    assert talk.exit_code == 0
    assert "Usage:" in talk.output
    assert "talk [OPTIONS] [CONVO] [MESSAGE...]" in talk.output
    assert invoke(root, "leave", conversation).exit_code == 0
    assert invoke(root, "join", conversation).exit_code == 0
    result = invoke(root)
    assert "rules" in result.output and "create" in result.output

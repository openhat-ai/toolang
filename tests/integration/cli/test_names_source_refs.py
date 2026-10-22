"""CLI Name validation and explicit Source Ref acquisition."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

import pytest
from typer.testing import CliRunner

from toolang.cli.toolang.main import app, main
from toolang.up import source as sources

runner = CliRunner()


@pytest.mark.parametrize("kind", ["skill", "prompt"])
def test_http_caps_accept_markdown_content_negotiation(tmp_path, capsys, kind):
    requests = []

    class ContentHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            requests.append(self.path)
            accepted = self.headers.get("Accept", "*/*")
            if "*/*" not in accepted and "text/markdown" not in accepted:
                self.send_error(406)
                return
            content = b"---\ndescription: Review\n---\nReview changes.\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ContentHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / "root"
    home = root / "agents/gus"
    home.mkdir(parents=True)
    (home / "agent.too").write_text("flow main():\n  pass\n")
    path = "/review/SKILL.md" if kind == "skill" else "/review.md"
    url = f"http://127.0.0.1:{server.server_port}{path}?version=2"
    try:
        result = main(["--root", str(root), "gus", kind, "add", url])
        output = capsys.readouterr()
        assert result == 0, output
        result = main(["--root", str(root), "gus", kind, "list", "--json"])
        output = capsys.readouterr()
        assert result == 0, output
        (record,) = json.loads(output.out)
        assert record["cap"] == f"{kind}/review"
        assert record["source"] == (
            url.replace("/SKILL.md", "") if kind == "skill" else url
        )
        assert requests == [f"{path}?version=2"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("name", ["Gus", "a.b", "gus-by3", "a" * 65])
def test_new_rejects_invalid_names_without_writing(tmp_path, name):
    root = tmp_path / "root"
    result = runner.invoke(app, ["--root", str(root), "new", name])
    assert result.exit_code == 2
    assert "invalid agent name" in result.output
    assert not root.exists()


def test_new_accepts_64_characters_and_lists_valid_peers(tmp_path):
    root = tmp_path / "root"
    name = "a" * 64
    assert runner.invoke(app, ["--root", str(root), "new", name]).exit_code == 0
    legacy = root / "agents" / "old.name"
    legacy.mkdir()
    (legacy / "agent.too").write_text("flow main():\n  pass\n")
    result = runner.invoke(app, ["--root", str(root), "list"])
    assert result.exit_code == 0, result.output
    assert name in result.output


@pytest.mark.parametrize(
    "ref", ["https://example.com/中文.too", "gh:owner/repo/" + "a" * 65 + ".too@main"]
)
def test_clone_explicit_name_can_replace_an_invalid_derived_default(
    tmp_path, monkeypatch, ref
):
    calls = []

    def fetch(source, **kwargs):
        calls.append(source.render())
        return "flow main():\n  pass\n"

    monkeypatch.setattr(sources, "fetch_agent_ref", fetch)
    root = tmp_path / "root"
    rejected = runner.invoke(app, ["--root", str(root), "clone", ref])
    assert rejected.exit_code == 1, rejected.output
    assert "invalid agent name" in rejected.output
    assert calls == [] and not root.exists()
    accepted = runner.invoke(app, ["--root", str(root), "clone", ref, "daily_report"])
    assert accepted.exit_code == 0, accepted.output
    assert len(calls) == 1
    assert (root / "agents" / "daily_report" / "agent.too").is_file()


@pytest.mark.parametrize("ref", ["owner/name", "oh:owner/name", "openhat:owner/name"])
def test_clone_rejects_deferred_sources_without_network_or_writes(
    tmp_path, monkeypatch, ref
):
    monkeypatch.setattr(
        sources, "urlopen", lambda *a, **k: pytest.fail("network access")
    )
    root = tmp_path / "root"
    result = runner.invoke(app, ["--root", str(root), "clone", ref, "gus"])
    assert result.exit_code == 1, result.output
    assert "unsupported Source Ref" in result.output
    assert not root.exists()


@pytest.mark.parametrize("filename", ["Daily-Report.v2.too", " Daily Report.v2.too"])
@pytest.mark.parametrize("mode", ["direct", "run", "shebang"])
def test_script_entry_points_keep_the_filename_and_share_the_normalized_home(
    tmp_path, mode, filename
):
    import subprocess
    import sys

    source = tmp_path / filename
    source.write_text("#!/usr/bin/env too\nflow main(_: Text) -> Text:\n  pass\n")
    source.chmod(0o755)
    cli = Path(sys.executable).with_name("too")
    command = (
        [str(source)]
        if mode == "shebang"
        else [str(cli), *(["run"] if mode == "run" else []), source.name]
    )
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{cli.parent}:/usr/bin:/bin",
        "TOOLANG_ROOT": str(tmp_path / "registry"),
    }
    result = subprocess.run(
        [*command, "main", "--quiet", "--out", "-", "--", "done"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "done"
    assert source.is_file()
    from toolang.up.identity import select_roaming

    layout = select_roaming(source)
    assert layout.name == "daily_report_v2"
    assert layout.program.resolve() == source.resolve()
    assert layout.root.name == layout.instance_name

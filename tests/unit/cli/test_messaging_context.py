"""Stable messaging identities for persisted drafts and pane metadata."""

from pathlib import Path

from toolang.cli.common.messaging import talk_context, talk_identity


def test_talk_identity_separates_root_connection_and_human(tmp_path):
    assert (
        len(
            {
                talk_identity(tmp_path, "one", "bryan"),
                talk_identity(tmp_path / "other", "one", "bryan"),
                talk_identity(tmp_path, "two", "bryan"),
                talk_identity(tmp_path, "one", "other"),
            }
        )
        == 4
    )


def test_existing_draft_and_pane_keys_remain_stable():
    root = Path("/toolang/root")
    assert talk_identity(root, "backend", "human:bryan") == "01f5debacc938b008478"
    assert (
        talk_context(root, "backend", "human:bryan", "http://127.0.0.1:9400")
        == "d51b69bf57730250466c"
    )


def test_pane_context_separates_endpoints(tmp_path):
    assert talk_context(
        tmp_path, "backend", "human:bryan", "http://first"
    ) != talk_context(tmp_path, "backend", "human:bryan", "http://second")

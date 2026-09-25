"""Adapter registration must not initialize clients needed only for model calls."""

import subprocess
import sys


def test_loading_adapters_does_not_import_openai_sdk() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from toolang.plugin.loading import load_model_adapters; "
            "import sys; "
            "adapters = load_model_adapters(); "
            "assert {'responses', 'chat_completions', 'messages', 'generate_content'} "
            "<= adapters.keys(); "
            "assert 'openai' not in sys.modules, 'setup imported the model-call SDK'",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

from __future__ import annotations

from toolang.state.config import canonical_state_config


def test_state_config_projection_excludes_setup_fields_and_is_canonical() -> None:
    first = canonical_state_config(
        b"""
[default]
model = "openai/one"

[allow]
models = ["openai/*"]
prompts = ["prompt/review"]

[prompts]
zeta = { ref = "acme/zeta" }
alpha = { ref = "acme/alpha" }
"""
    )
    reordered = canonical_state_config(
        b"""
[models.providers.openai]
adapter = "responses"

[prompts]
alpha = { ref = "acme/alpha" }
zeta = { ref = "acme/zeta" }

[allow]
prompts = ["prompt/review"]
tools = ["shell/*"]
"""
    )

    assert first == reordered
    text = first.decode()
    assert "default" not in text
    assert "models" not in text
    assert "tools" not in text
    assert text.index("alpha") < text.index("zeta")

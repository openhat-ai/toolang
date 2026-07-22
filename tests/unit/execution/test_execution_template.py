import pytest

from toolang.common.errors import ToolangError
from toolang.common.template import render_text_template


def test_render_text_template_supports_variables_sections_and_inverted_sections() -> None:
    rendered = render_text_template(
        """
{{greeting}}
{{#focus}}
Focus: {{focus}}
{{/focus}}
{{^audience}}
No audience.
{{/audience}}
{{#runtime.skills}}
- {{name}}
{{/runtime.skills}}
""".strip(),
        {
            "greeting": "Hello",
            "focus": "correctness",
            "audience": None,
            "runtime": {
                "skills": [
                    {"name": "review"},
                    {"name": "patch"},
                ]
            },
        },
    )

    assert rendered.rstrip("\n") == "Hello\nFocus: correctness\nNo audience.\n- review\n- patch"


def test_render_text_template_does_not_escape_plain_text_values() -> None:
    rendered = render_text_template("{{text}}", {"text": "<keep & raw>"})

    assert rendered == "<keep & raw>"


@pytest.mark.parametrize(
    "template, match",
    [
        ("{{> partial}}", "do not support tags"),
        ("{{! note}}", "do not support tags"),
        ("{{{raw}}}", "do not support unescaped tags"),
        ("{{& raw}}", "do not support tags"),
        ("{{= <% %> =}}", "do not support tags"),
        ("{{#items}}{{name}}", "unclosed Toolang template section"),
        ("{{/items}}", "unmatched Toolang template section close"),
    ],
)
def test_render_text_template_rejects_unsupported_mustache_features(template: str, match: str) -> None:
    with pytest.raises(ToolangError, match=match):
        render_text_template(template, {"runtime": {}, "items": []})


def test_render_text_template_rejects_callable_context_values() -> None:
    with pytest.raises(ToolangError, match="does not support callables"):
        render_text_template("{{value}}", {"value": lambda: "nope"})

from types import MappingProxyType

import pytest

from toolang.common.errors import ToolangError
from toolang.common.template import render_text_template, template_root_names


def test_render_text_template_supports_variables_sections_and_inverted_sections() -> (
    None
):
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

    assert (
        rendered.rstrip("\n")
        == "Hello\nFocus: correctness\nNo audience.\n- review\n- patch"
    )


def test_render_text_template_does_not_escape_plain_text_values() -> None:
    rendered = render_text_template("{{text}}", {"text": "<keep & raw>"})

    assert rendered == "<keep & raw>"


def test_render_text_template_supports_sequence_indexes() -> None:
    rendered = render_text_template(
        "{{family}} {{name}} {{args.0}} {{args.1}}",
        {
            "family": "fs",
            "name": "read",
            "args": ("README.md", "utf-8"),
        },
    )

    assert rendered == "fs read README.md utf-8"


def test_template_root_names_follow_supported_reference_rules() -> None:
    assert template_root_names(
        "{{user-name}} {{#items}}{{items.name}}{{/items}} {{records.0}} {{_}} {{.}}"
    ) == ("user-name", "items", "items", "records", "_")


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
def test_render_text_template_rejects_unsupported_mustache_features(
    template: str, match: str
) -> None:
    with pytest.raises(ToolangError, match=match):
        render_text_template(template, {"runtime": {}, "items": []})


def test_render_text_template_rejects_callable_context_values() -> None:
    with pytest.raises(ToolangError, match="does not support callables"):
        render_text_template("{{value}}", {"value": lambda: "nope"})


def test_template_lookup_reads_data_without_python_attributes() -> None:
    assert (
        render_text_template(
            "{{record.items}}|{{text.title}}|{{items.count}}|"
            "{{#record.items}}wrong{{/record.items}}{{^record.items}}empty{{/record.items}}",
            {"record": {}, "text": "hello world", "items": []},
        )
        == "|||empty"
    )
    assert (
        render_text_template(
            "{{record.items}}/{{record.keys}}/{{record.clear}}",
            {"record": {"items": "items", "keys": "keys", "clear": "clear"}},
        )
        == "items/keys/clear"
    )


def test_template_lookup_preserves_scope_shadowing_and_dotted_paths() -> None:
    assert (
        render_text_template(
            "{{#rows}}{{prefix}}:{{value}}/{{record.value}};{{/rows}}",
            {
                "prefix": "outer",
                "record": {"value": "outer"},
                "rows": [
                    {"value": 0, "record": {"value": False}},
                    {"value": None, "prefix": "inner"},
                ],
            },
        )
        == "outer:0/false;inner:/outer;"
    )


def test_template_serializes_immutable_containers_at_every_depth() -> None:
    value = MappingProxyType({"items": (MappingProxyType({"passed": False}),)})
    assert (
        render_text_template(
            "{{value}}|{{value.items}}|{{_1.value}}",
            {"value": value, "_1": {"value": value}},
        )
        == '{"items":[{"passed":false}]}|[{"passed":false}]|{"items":[{"passed":false}]}'
    )

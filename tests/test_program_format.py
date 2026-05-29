from toolang.program_format import format_source


def test_format_source_normalizes_too_spacing() -> None:
    source = """
use   skill   briceyan/pdf-processing

struct ReviewSummary:
    title:string
    summary?:   Text

thunk review( input:Message,path?:Path)->Json:
    model= gpt-5
    skills += review,patch
    user:   Review the target carefully.
""".strip()

    assert format_source(source) == (
        "use skill briceyan/pdf-processing\n"
        "\n"
        "struct ReviewSummary:\n"
        "  title: string\n"
        "  summary?: Text\n"
        "\n"
        "thunk review(input: Message, path?: Path) -> Json:\n"
        "  models = gpt-5\n"
        "  skills += review, patch\n"
        "\n"
        "  user: Review the target carefully.\n"
    )


def test_format_source_uses_configured_tab_size() -> None:
    source = """
struct ReviewSummary:
  title:string

thunk review(input:Message):
  user:
    Review it.
""".strip()

    assert format_source(source, tab_size=4) == (
        "struct ReviewSummary:\n"
        "    title: string\n"
        "\n"
        "thunk review(input: Message):\n"
        "    user:\n"
        "        Review it.\n"
    )


def test_format_source_uses_syntax_without_program_semantics() -> None:
    source = """
service search: ```md
This is syntactically valid even before service metadata is authored.
```
""".strip()

    assert format_source(source) == (
        "service search: ```md\n"
        "This is syntactically valid even before service metadata is authored.\n"
        "```\n"
    )


def test_format_source_accepts_unformatted_shebang_file() -> None:
    source = """
#!/usr/bin/env toolang

thunk followup:
    models = deepseek/*
    recall = none

    context: none

    user:
        My name is Ada.
""".lstrip()

    assert format_source(source) == (
        "#!/usr/bin/env toolang\n"
        "\n"
        "thunk followup:\n"
        "  models = deepseek/*\n"
        "  recall = none\n"
        "\n"
        "  context: none\n"
        "\n"
        "  user:\n"
        "    My name is Ada.\n"
    )


def test_format_source_normalizes_thunk_blank_lines_by_section() -> None:
    source = """
thunk review(input: Message):

    models = gpt-5


    tools = shell
    context: repo

    instruct: strict-json
    user:
        Review it.
    assistant: Ready.
    user:
        Continue.
""".strip()

    assert format_source(source) == (
        "thunk review(input: Message):\n"
        "  models = gpt-5\n"
        "  tools = shell\n"
        "\n"
        "  context: repo\n"
        "  instruct: strict-json\n"
        "\n"
        "  user:\n"
        "    Review it.\n"
        "\n"
        "  assistant: Ready.\n"
        "\n"
        "  user:\n"
        "    Continue.\n"
    )


def test_format_source_keeps_instruct_block_body_attached() -> None:
    source = """
thunk followup:
  models = deepseek/*
  recall = none

  context: none
  instruct:
      abc

  user:
    My name is Ada.
""".strip()

    assert format_source(source) == (
        "thunk followup:\n"
        "  models = deepseek/*\n"
        "  recall = none\n"
        "\n"
        "  context: none\n"
        "\n"
        "  instruct:\n"
        "    abc\n"
        "\n"
        "  user:\n"
        "    My name is Ada.\n"
    )


def test_format_source_keeps_inline_controls_together() -> None:
    source = """
thunk followup:
  instruct: strict

  context: none

  user: hello
""".strip()

    assert format_source(source) == (
        "thunk followup:\n"
        "  instruct: strict\n"
        "  context: none\n"
        "\n"
        "  user: hello\n"
    )


def test_format_source_orders_inline_controls_before_block_controls() -> None:
    source = """
thunk followup:
  context:
      repo context

  instruct: strict
  user: hello
""".strip()

    assert format_source(source) == (
        "thunk followup:\n"
        "  instruct: strict\n"
        "\n"
        "  context:\n"
        "    repo context\n"
        "\n"
        "  user: hello\n"
    )


def test_format_source_does_not_absorb_implicit_message_after_control_block() -> None:
    source = """
thunk slug(title) -> text:
    models = sss

    instruct: hello

    context:
        abcdef sdfss

    Convert the provided title into a concise lowercase slug.
    Use hyphens between words and return only the slug text.
""".strip()

    assert format_source(source, tab_size=4) == (
        "thunk slug(title) -> text:\n"
        "    models = sss\n"
        "\n"
        "    instruct: hello\n"
        "\n"
        "    context:\n"
        "        abcdef sdfss\n"
        "\n"
        "    Convert the provided title into a concise lowercase slug.\n"
        "    Use hyphens between words and return only the slug text.\n"
    )


def test_format_source_does_not_absorb_same_indent_implicit_message_after_control_block() -> None:
    source = """
thunk slug(title) -> text:
    context:
        abcdef sdfss
    Convert the provided title into a concise lowercase slug.
    Use hyphens between words and return only the slug text.
""".strip()

    assert format_source(source, tab_size=4) == (
        "thunk slug(title) -> text:\n"
        "    context:\n"
        "        abcdef sdfss\n"
        "\n"
        "    Convert the provided title into a concise lowercase slug.\n"
        "    Use hyphens between words and return only the slug text.\n"
    )


def test_format_source_preserves_implicit_message_paragraphs() -> None:
    source = """
# Alice is a small remote-ready assistant example.
# The goal is one file that can be published and used without local setup.

use skill briceyan/pdf-processing

thunk:

  Help the user directly.

  Use the selected skills when they apply.

  Otherwise answer directly.
""".strip()

    assert format_source(source) == (
        "# Alice is a small remote-ready assistant example.\n"
        "# The goal is one file that can be published and used without local setup.\n"
        "\n"
        "use skill briceyan/pdf-processing\n"
        "\n"
        "thunk:\n"
        "  Help the user directly.\n"
        "\n"
        "  Use the selected skills when they apply.\n"
        "\n"
        "  Otherwise answer directly.\n"
    )


def test_format_source_preserves_top_comment_spacing() -> None:
    source = """
#!/usr/bin/env toolang 

#First comment block.

# Second comment block.
#    Same block.
#

struct Summary:
  title: string
""".lstrip()

    assert format_source(source) == (
        "#!/usr/bin/env toolang\n"
        "\n"
        "# First comment block.\n"
        "\n"
        "# Second comment block.\n"
        "# Same block.\n"
        "#\n"
        "\n"
        "struct Summary:\n"
        "  title: string\n"
    )


def test_format_source_formats_program_comments_and_keeps_attached_comments() -> None:
    source = """
##!program comments
##!    second program comment
## attached comments
struct Summary:
    title: string
    summary: string

#normal comments
struct BulletList:
    items: string[]
""".strip()

    assert format_source(source, tab_size=4) == (
        "##! program comments\n"
        "##! second program comment\n"
        "\n"
        "## attached comments\n"
        "struct Summary:\n"
        "    title: string\n"
        "    summary: string\n"
        "\n"
        "# normal comments\n"
        "struct BulletList:\n"
        "    items: string[]\n"
    )


def test_format_source_moves_program_comments_after_shebang() -> None:
    source = """
#!/usr/bin/env toolang

# attached comments
struct Summary:
    title: string

##!later program comment
struct BulletList:
    items: string[]
""".lstrip()

    assert format_source(source, tab_size=4) == (
        "#!/usr/bin/env toolang\n"
        "\n"
        "##! later program comment\n"
        "\n"
        "# attached comments\n"
        "struct Summary:\n"
        "    title: string\n"
        "\n"
        "struct BulletList:\n"
        "    items: string[]\n"
    )

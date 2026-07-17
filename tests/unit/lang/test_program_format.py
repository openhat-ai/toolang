from toolang.lang.format import format_source


def test_format_source_normalizes_too_spacing() -> None:
    source = """
with skill   briceyan/pdf-processing

struct ReviewSummary:
    title:Text
    summary?:   Text

agic review( input:Message,path?:Path)->Json:
    model= gpt-5
    skills += review,patch
    user:   Review the target carefully.
""".strip()

    assert format_source(source) == (
        "with skill briceyan/pdf-processing\n"
        "\n"
        "struct ReviewSummary:\n"
        "  title: Text\n"
        "  summary?: Text\n"
        "\n"
        "agic review(in: Pack, path?: Path) -> Json:\n"
        "  models = gpt-5\n"
        "  skills += review, patch\n"
        "\n"
        "  user: Review the target carefully.\n"
    )


def test_format_source_uses_configured_tab_size() -> None:
    source = """
struct ReviewSummary:
  title:Text

agic review(input:Message):
  user:
    Review it.
""".strip()

    assert format_source(source, tab_size=4) == (
        "struct ReviewSummary:\n"
        "    title: Text\n"
        "\n"
        "agic review(in: Pack):\n"
        "    user:\n"
        "        Review it.\n"
    )


def test_format_source_uses_syntax_without_program_semantics() -> None:
    source = """
service search:
  This is syntactically valid even before service metadata is authored.
""".strip()

    assert format_source(source) == (
        "service search:\n"
        "  This is syntactically valid even before service metadata is authored.\n"
    )


def test_format_source_formats_inline_job_headers() -> None:
    source = """
task   review_api  :
  title = Review API changes

  Review the API changes.

chore stale_prs  :
  schedule = FREQ=HOURLY;INTERVAL=6
""".strip()

    assert format_source(source) == (
        "task review_api:\n"
        "  title = Review API changes\n"
        "  Review the API changes.\n"
        "\n"
        "chore stale_prs:\n"
        "  schedule = FREQ=HOURLY;INTERVAL=6\n"
    )


def test_format_source_accepts_unformatted_shebang_file() -> None:
    source = """
#!/usr/bin/env toolang

agic followup:
    models = deepseek/*
    recall = none

    context: none

    user:
        My name is Ada.
""".lstrip()

    assert format_source(source) == (
        "#!/usr/bin/env toolang\n"
        "\n"
        "agic followup:\n"
        "  models = deepseek/*\n"
        "  recall = none\n"
        "\n"
        "  context: none\n"
        "\n"
        "  user:\n"
        "    My name is Ada.\n"
    )


def test_format_source_normalizes_agic_blank_lines_by_section() -> None:
    source = """
agic review(input: Message):

    models = gpt-5


    tools = shell
    context: repo

    instruct: strict_json
    user:
        Review it.
    assistant: Ready.
    user:
        Continue.
""".strip()

    assert format_source(source) == (
        "agic review(in: Pack):\n"
        "  models = gpt-5\n"
        "  tools = shell\n"
        "\n"
        "  context: repo\n"
        "  instruct: strict_json\n"
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
agic followup:
  models = deepseek/*
  recall = none

  context: none
  instruct:
      abc

  user:
    My name is Ada.
""".strip()

    assert format_source(source) == (
        "agic followup:\n"
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
agic followup:
  instruct: strict

  context: none

  user: hello
""".strip()

    assert format_source(source) == (
        "agic followup:\n"
        "  instruct: strict\n"
        "  context: none\n"
        "\n"
        "  user: hello\n"
    )


def test_format_source_orders_inline_controls_before_block_controls() -> None:
    source = """
agic followup:
  context:
      repo context

  instruct: strict
  user: hello
""".strip()

    assert format_source(source) == (
        "agic followup:\n"
        "  instruct: strict\n"
        "\n"
        "  context:\n"
        "    repo context\n"
        "\n"
        "  user: hello\n"
    )


def test_format_source_does_not_absorb_implicit_message_after_control_block() -> None:
    source = """
agic slug(title) -> Text:
    models = sss

    instruct: hello

    context:
        abcdef sdfss

    Convert the provided title into a concise lowercase slug.
    Use hyphens between words and return only the slug text.
""".strip()

    assert format_source(source, tab_size=4) == (
        "agic slug(title) -> Text:\n"
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
agic slug(title) -> Text:
    context:
        abcdef sdfss
    Convert the provided title into a concise lowercase slug.
    Use hyphens between words and return only the slug text.
""".strip()

    assert format_source(source, tab_size=4) == (
        "agic slug(title) -> Text:\n"
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

with skill briceyan/pdf-processing

agic:

  Help the user directly.

  Use the selected skills when they apply.

  Otherwise answer directly.
""".strip()

    assert format_source(source) == (
        "# Alice is a small remote-ready assistant example.\n"
        "# The goal is one file that can be published and used without local setup.\n"
        "\n"
        "with skill briceyan/pdf-processing\n"
        "\n"
        "agic:\n"
        "  Help the user directly.\n"
        "\n"
        "  Use the selected skills when they apply.\n"
        "\n"
        "  Otherwise answer directly.\n"
    )


def test_format_source_keeps_explicit_roles_after_implicit_messages() -> None:
    source = """
agic is_relevant(in: Part[]):
    Evidence bundle:
    {{ _ }}

    Decide whether this evidence bundle contains concrete information about agent workflow implementations.

    # comments
    user:
        abc

    assistant:
        def
""".strip()

    assert format_source(source) == (
        "agic is_relevant(in: Part[]):\n"
        "  Evidence bundle:\n"
        "  {{ _ }}\n"
        "\n"
        "  Decide whether this evidence bundle contains concrete information about agent workflow implementations.\n"
        "\n"
        "  # comments\n"
        "\n"
        "  user:\n"
        "    abc\n"
        "\n"
        "  assistant:\n"
        "    def\n"
    )


def test_format_source_uses_comments_to_split_implicit_messages() -> None:
    source = """
agic split:
    first message
    # Plain comment splits messages.
    second message
""".strip()

    assert format_source(source) == (
        "agic split:\n"
        "  first message\n"
        "\n"
        "  # Plain comment splits messages.\n"
        "\n"
        "  second message\n"
    )


def test_format_source_keeps_comment_separators_between_agic_sections() -> None:
    source = """
agic split:
  models = gpt-5
  # directive comment
  context: repo
  # control comment
  user: hi
  # role comment
  assistant: ok
""".strip()

    assert format_source(source) == (
        "agic split:\n"
        "  models = gpt-5\n"
        "  # directive comment\n"
        "\n"
        "  context: repo\n"
        "  # control comment\n"
        "\n"
        "  user: hi\n"
        "\n"
        "  # role comment\n"
        "\n"
        "  assistant: ok\n"
    )


def test_format_source_preserves_top_comment_spacing() -> None:
    source = """
#!/usr/bin/env toolang 

#First comment block.

# Second comment block.
#    Same block.
#

struct Summary:
  title: Text
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
        "  title: Text\n"
    )


def test_format_source_formats_program_comments_and_keeps_attached_comments() -> None:
    source = """
##!program comments
##!    second program comment
## attached comments
struct Summary:
    title: Text
    summary: Text

#normal comments
struct BulletList:
    items: Text[]
""".strip()

    assert format_source(source, tab_size=4) == (
        "##! program comments\n"
        "##! second program comment\n"
        "\n"
        "## attached comments\n"
        "struct Summary:\n"
        "    title: Text\n"
        "    summary: Text\n"
        "\n"
        "# normal comments\n"
        "struct BulletList:\n"
        "    items: Text[]\n"
    )


def test_format_source_moves_program_comments_after_shebang() -> None:
    source = """
#!/usr/bin/env toolang

# attached comments
struct Summary:
    title: Text

##!later program comment
struct BulletList:
    items: Text[]
""".lstrip()

    assert format_source(source, tab_size=4) == (
        "#!/usr/bin/env toolang\n"
        "\n"
        "##! later program comment\n"
        "\n"
        "# attached comments\n"
        "struct Summary:\n"
        "    title: Text\n"
        "\n"
        "struct BulletList:\n"
        "    items: Text[]\n"
    )

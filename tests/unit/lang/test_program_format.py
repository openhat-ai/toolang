import pytest

from tests import FIXTURES_ROOT, PROJECT_ROOT
from toolang.lang import (
    Program,
    ToolangFormatError,
    format_source,
    format_statement_head,
    to_data,
)
from toolang.lang.ast import LetStmt


def test_format_statement_head_preserves_compact_source_order() -> None:
    program = Program.from_source(
        """
agic action:
  pass

agic predicate -> Boolean:
  pass

agic score -> Number:
  pass

flow work:
  let topic =
    hello
  run action
  let reviewed = run action
  let run action
  seek reviewer action
  ask: Continue?
  scatter 2 using action
  storm 3 using action in 2 lanes
  gather using action
  settle using action
  map using action in 4 lanes
  keep first 2
  keep if predicate in 2 lanes
  drop last 1
  sort descending by score  in 2 lanes
  keep first 3
  repeat 2 times:
    run action
"""
    )

    assert [
        format_statement_head(statement) for statement in program.flows[0].stmts
    ] == [
        "let topic",
        "run action",
        "let reviewed = run action",
        "let run action",
        "seek reviewer action",
        "ask",
        "scatter 2 using action",
        "storm 3 in 2 lanes using action",
        "gather using action",
        "settle using action",
        "map in 4 lanes using action",
        "keep first 2",
        "keep in 2 lanes if predicate",
        "drop last 1",
        "sort descending in 2 lanes by score",
        "keep first 3",
        "repeat 2 times",
    ]


def test_format_statement_head_hides_generated_inline_agic_names() -> None:
    program = Program.from_source(
        """
flow work:
  run: Draft a report.
  map in 3 lanes using: Rewrite this item.
  sort descending in 3 lanes by: Score this item.
  keep first 2
"""
    )

    assert [
        format_statement_head(statement) for statement in program.flows[0].stmts
    ] == [
        "run",
        "map in 3 lanes using",
        "sort descending in 3 lanes by",
        "keep first 2",
    ]


def test_format_source_normalizes_too_spacing() -> None:
    source = """
with skill   briceyan/pdf-processing

struct ReviewSummary:
    title:Text
    summary?:   Text

agic review( _:Part[],path?:Path)->Json:
    models= gpt-5
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
        "agic review(_: Part[], path?: Path) -> Json:\n"
        "  models = gpt-5\n"
        "  skills += review, patch\n"
        "\n"
        "  user: Review the target carefully.\n"
    )


def test_format_source_preserves_commas_inside_collection_queries() -> None:
    source = """
agic review:
    tools=filesystem/*[description="read,exact"],shell/*[parameters in (command,timeout)]

    Review the target.
""".strip()

    formatted = format_source(source)

    assert formatted == (
        "agic review:\n"
        '  tools = filesystem/*[description="read,exact"], '
        "shell/*[parameters in (command,timeout)]\n"
        "\n"
        "  Review the target.\n"
    )
    assert format_source(formatted) == formatted
    Program.from_source(formatted)


def test_format_source_does_not_remove_empty_query_matches() -> None:
    source = "agic review:\n  tools = filesystem/*,,shell/*,\n\n  Review.\n"

    assert format_source(source) == (
        "agic review:\n  tools = filesystem/*, , shell/*,\n\n  Review.\n"
    )


def test_format_source_preserves_hashes_inside_collection_queries() -> None:
    source = """agic review:
    tools=filesystem/*[description="read # exact"],vendor/tool#v1 # keep

    Review the target.
""".strip()

    formatted = format_source(source)
    program = Program.from_source(formatted)

    assert formatted == (
        "agic review:\n"
        '  tools = filesystem/*[description="read # exact"], vendor/tool#v1  # keep\n'
        "\n"
        "  Review the target.\n"
    )
    assert program.agics[0].directives[0].values == (
        'filesystem/*[description="read # exact"], vendor/tool#v1',
    )
    assert format_source(formatted) == formatted


def test_format_source_uses_configured_tab_size() -> None:
    source = """
struct ReviewSummary:
  title:Text

agic review(_:Part[]):
  user:
    Review it.
""".strip()

    assert format_source(source, tab_size=4) == (
        "struct ReviewSummary:\n"
        "    title: Text\n"
        "\n"
        "agic review(_: Part[]):\n"
        "    user:\n"
        "        Review it.\n"
    )


def test_format_source_expands_tabs_using_configured_tab_size() -> None:
    source = "agic followup:\n\tcontext:\n\t\trepo context\n\tuser:\n\t\thello\n"

    assert format_source(source, tab_size=8) == (
        "agic followup:\n"
        "        context:\n"
        "                repo context\n"
        "\n"
        "        user:\n"
        "                hello\n"
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


def test_format_source_indents_cap_properties_and_content_local_blocks() -> None:
    source = """
service search:
    description= Search docs.
    protocol= http
    target= https://example.com/mcp

flow collect:
    let note =
        tools = literal content
        run remains literal content
""".strip()
    expected = (
        "service search:\n"
        "  description = Search docs.\n"
        "  protocol = http\n"
        "  target = https://example.com/mcp\n"
        "\n"
        "flow collect:\n"
        "  let note =\n"
        "    tools = literal content\n"
        "    run remains literal content\n"
    )

    assert format_source(source) == expected
    assert format_source(expected) == expected
    program = Program.from_source(expected)
    statement = program.flows[0].stmts[0]
    assert isinstance(statement, LetStmt)
    assert statement.value == ("tools = literal content\nrun remains literal content")


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
        "\n"
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
agic review(_:Part[]):

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
        "agic review(_: Part[]):\n"
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
        "agic followup:\n  instruct: strict\n  context: none\n\n  user: hello\n"
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


def test_format_source_does_not_absorb_same_indent_implicit_message_after_control_block() -> (
    None
):
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
agic is_relevant(_: Part[]):
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
        "agic is_relevant(_: Part[]):\n"
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


def test_format_source_covers_complete_program_and_is_idempotent() -> None:
    source = """#!/usr/bin/env toolang<spaces>
##!complete formatter fixture

with skill   org/review

service search  :
    description= Search docs
    protocol = http
    target= https://example.com/mcp

    Search documentation.

prompt summarize:
    Summarize {{topic}}.

struct Result:
    summary:Text
    notes?: Text[]

context repo:
    Repository context.

instruct concise:
    Be concise.

agic review( _,focus ? : Text)->Result:
    models= gpt-5,claude
    context: repo
    instruct: concise
    user:
        Review {{ _ }}.

flow pipeline( _:Part[])->Result:
    tools+= shell,fs
    let drafts= scatter   2 using review
    repeat 2 times:
        run review
        until:
            Done.
""".replace("<spaces>", "   ")
    expected = """#!/usr/bin/env toolang

##! complete formatter fixture

with skill org/review

service search:
  description = Search docs
  protocol = http
  target = https://example.com/mcp

  Search documentation.

prompt summarize:
  Summarize {{topic}}.

struct Result:
  summary: Text
  notes?: Text[]

context repo:
  Repository context.

instruct concise:
  Be concise.

agic review(_: Part[], focus?: Text) -> Result:
  models = gpt-5, claude

  context: repo
  instruct: concise

  user:
    Review {{ _ }}.

flow pipeline(_: Part[]) -> Result:
  tools += shell, fs
  let drafts = scatter 2 using review
  repeat 2 times:
    run review
    until:
      Done.
"""

    formatted = format_source(source)

    assert formatted == expected
    assert format_source(formatted) == formatted
    program = Program.from_source(formatted)
    assert program.agics[0].input is not None
    assert (program.agics[0].input.name, program.agics[0].input.type_name) == (
        "_",
        "Part[]",
    )
    assert [statement.kind for statement in program.flows[0].stmts] == [
        "scatter",
        "repeat",
    ]


def test_repo_programs_format_idempotently_without_semantic_changes() -> None:
    source_paths = [
        *sorted(FIXTURES_ROOT.glob("*.too")),
        *sorted((PROJECT_ROOT / "examples").glob("*.too")),
    ]

    for source_path in source_paths:
        source = source_path.read_text(encoding="utf-8")
        formatted = format_source(source)

        assert format_source(formatted) == formatted, source_path.name
        assert _without_spans(
            to_data(Program.from_source(formatted))
        ) == _without_spans(to_data(Program.from_source(source))), source_path.name


def test_format_source_preserves_relative_content_indentation() -> None:
    source = """context example:
      first
        nested
      last
"""

    assert format_source(source) == ("context example:\n  first\n    nested\n  last\n")


@pytest.mark.parametrize("tab_size", [1, 2, 4, 8])
@pytest.mark.parametrize(
    "body, expected",
    [
        ("\t\tFirst.\n\t\t\tNested.\n\t\tLast.\n", "First.\n        Nested.\nLast."),
        ("    First.\n\tNested.\n    Last.\n", "First.\n    Nested.\nLast."),
        ("    First.\n  \tNested.\n    Last.\n", "First.\n    Nested.\nLast."),
    ],
)
def test_text_indentation_uses_grammar_tab_stops_independent_of_format_width(
    tab_size: int, body: str, expected: str
) -> None:
    source = f"flow work:\n  run:\n{body}"
    before = Program.from_source(source)
    assert before.agics[0].messages[0].content == expected
    formatted = format_source(source, tab_size=tab_size)
    assert Program.from_source(formatted).agics[0].messages[0].content == expected
    assert format_source(formatted, tab_size=tab_size) == formatted


@pytest.mark.parametrize(
    "content",
    ["Keep  these   spaces.", "See  https://example.com/a=b.", "Align\tthese values."],
)
def test_format_source_preserves_inline_content_binding_text(content: str) -> None:
    source = f"flow work:\n    let   note  =  {content}\n"
    formatted = format_source(source)
    assert formatted == f"flow work:\n  let note = {content}\n"
    assert _without_spans(to_data(Program.from_source(formatted))) == _without_spans(
        to_data(Program.from_source(source))
    )


@pytest.mark.parametrize("blank_lines", [1, 2, 3, 5])
@pytest.mark.parametrize("header", ["flow work:\n  run:", "agic work:\n  context:"])
def test_format_source_preserves_all_blank_lines_inside_explicit_text(
    header: str, blank_lines: int
) -> None:
    source = f"{header}\n    First.\n" + "\n" * blank_lines + "    Last.\n"
    formatted = format_source(source)
    before, after = (Program.from_source(item) for item in (source, formatted))
    if before.contexts:
        assert before.contexts[0].body == after.contexts[0].body
    else:
        assert before.agics[0].messages[0].content == after.agics[0].messages[0].content
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("role", ["user", "context", "instruct"])
def test_format_source_does_not_interpret_markdown_fences(role: str) -> None:
    source = (
        f"agic work:\n  {role}:\n"
        "    Here is an example: ```\n"
        "      content\n    ```\n    Last.\n"
    )
    formatted = format_source(source)
    assert formatted == source
    assert _without_spans(to_data(Program.from_source(formatted))) == _without_spans(
        to_data(Program.from_source(source))
    )


@pytest.mark.parametrize(
    "source",
    [
        "flow work:\n    repeat 2 times:\n\trun: Work.\n    run: Publish.\n",
        "flow work:\n\trepeat 2 times:\n         repeat 2 times:\n\t\trun: Work.\n",
    ],
)
def test_format_source_uses_cst_depth_when_levels_use_different_indent_styles(
    source: str,
) -> None:
    before = Program.from_source(source)
    formatted = format_source(source)
    assert _without_spans(to_data(Program.from_source(formatted))) == _without_spans(
        to_data(before)
    )
    assert format_source(formatted) == formatted


@pytest.mark.parametrize("kind", ["agic", "flow"])
@pytest.mark.parametrize("separator", ["", "\n"])
def test_format_source_does_not_attach_detached_documentation(
    kind: str, separator: str
) -> None:
    source = f"{kind} work:\n    ## Detached documentation.\n{separator}  Work.\n"
    before = Program.from_source(source)
    formatted = format_source(source)
    after = Program.from_source(formatted)
    assert before.agics[0].doc == after.agics[0].doc is None
    if before.flows:
        assert before.flows[0].stmts[0].doc == after.flows[0].stmts[0].doc is None
    else:
        assert before.agics[0].messages[0].doc == after.agics[0].messages[0].doc is None
    assert format_source(formatted) == formatted


@pytest.mark.parametrize(
    "header, indent",
    [
        ("flow work:\n  run:", "    "),
        ("agic work:\n  user:", "    "),
        ("agic work:\n  context:", "    "),
        ("agic work:\n  instruct:", "    "),
        ("context notes:", "  "),
        ("instruct rules:", "  "),
        ("prompt review:", "  "),
        ("task review:", "  "),
    ],
)
def test_format_source_preserves_literal_headers_and_markdown(
    header: str, indent: str
) -> None:
    body = (
        f"{indent}First paragraph.\n"
        f"{indent}##! Literal heading.\n"
        f"{indent}tools = prose, not a directive.\n"
        f"{indent}context: prose, not a setting.\n\n"
        f"{indent}  - Nested Markdown.\n"
        f"{indent}Last line.\n"
    )
    source = f"{header}\n{body}"
    formatted = format_source(source)
    assert body in formatted
    assert format_source(formatted) == formatted
    assert _without_spans(to_data(Program.from_source(formatted))) == _without_spans(
        to_data(Program.from_source(source))
    )


@pytest.mark.parametrize(
    "kind, properties",
    [
        ("psyche", ""),
        ("skill", "  description = Review the evidence.\n"),
        (
            "service",
            "  description = Review the evidence.\n"
            "  transport = http\n  target = https://example.com/mcp\n",
        ),
        ("prompt", ""),
    ],
)
def test_format_source_keeps_cap_comments_out_of_program_documentation(
    kind: str,
    properties: str,
) -> None:
    source = (
        f"{kind} review:\n"
        "  ##! Keep this comment inside the declaration.\n"
        f"{properties}"
        "  Review the evidence.\n"
    )
    formatted = format_source(source)
    assert Program.from_source(formatted).doc is None
    assert format_source(formatted) == formatted
    assert _without_spans(to_data(Program.from_source(formatted))) == _without_spans(
        to_data(Program.from_source(source))
    )


def test_format_source_preserves_header_comments() -> None:
    source = """struct Result: # result
  value:Text

agic review(_:Part[]):   # runnable
  pass
"""

    assert format_source(source) == (
        "struct Result:  # result\n"
        "  value: Text\n"
        "\n"
        "agic review(_: Part[]):  # runnable\n"
        "  pass\n"
    )


def test_format_source_preserves_semantic_blank_line_count() -> None:
    source = """agic messages:
    First message.


    Second message.

flow steps:
    First step.


    Second step.
"""

    formatted = format_source(source)
    program = Program.from_source(formatted)

    assert "  First message.\n\n\n  Second message.\n" in formatted
    assert "  First step.\n\n\n  Second step.\n" in formatted
    assert [message.content for message in program.agics[0].messages] == [
        "First message.",
        "Second message.",
    ]
    assert [statement.kind for statement in program.flows[0].stmts] == [
        "run",
        "run",
    ]


@pytest.mark.parametrize("tab_size", [0, -1])
def test_format_source_rejects_non_positive_tab_size(tab_size: int) -> None:
    with pytest.raises(ToolangFormatError, match="tab size must be positive"):
        format_source("agic:\n  pass\n", tab_size=tab_size)


def _without_spans(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_spans(item) for key, item in value.items() if key != "span"
        }
    if isinstance(value, list):
        return [_without_spans(item) for item in value]
    return value


def test_format_source_reports_original_syntax_error_line_after_shebang() -> None:
    source = "#!/usr/bin/env toolang\n\nnot-a-declaration\n"

    with pytest.raises(ToolangFormatError, match="line 3"):
        format_source(source)

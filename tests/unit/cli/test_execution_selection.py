"""Execution selection keeps concrete homes and explicit record arguments bound."""

from contextlib import closing

from typer import Context
from typer.core import TyperCommand

from toolang.cli.common.context import CliContext, context_layout, target_context
from toolang.cli.common.execution import select_agent_instance
from toolang.execution.store import RunStore
from toolang.up.allocation import allocate_roaming


def test_allocated_home_is_not_replaced_by_a_later_launch(tmp_path):
    source = tmp_path / "aid.too"
    root = tmp_path / "root"
    selected = allocate_roaming(source, toolang_root=root)
    later = allocate_roaming(source, toolang_root=root)
    assert selected.place != later.place
    context = Context(
        TyperCommand("chat"),
        obj=CliContext(root, source=source, layout=selected, prepared=True),
    )
    assert select_agent_instance(context).place == selected.place
    assert context_layout(context).place == selected.place


def test_history_uses_its_explicit_record_instead_of_parser_state(
    tmp_path, monkeypatch
):
    source = tmp_path / "aid.too"
    root = tmp_path / "root"
    first = allocate_roaming(source, toolang_root=root)
    second = allocate_roaming(source, toolang_root=root)
    for layout, thread in ((first, "chat_first"), (second, "chat_second")):
        with closing(RunStore(layout.run_store)) as store:
            store.create_thread(thread_id=thread)
    context = Context(TyperCommand("chat"), obj=target_context(root, source))
    context.params["thread"] = "chat_second"
    assert select_agent_instance(context, record="chat_first") == first
    assert context_layout(context) == first

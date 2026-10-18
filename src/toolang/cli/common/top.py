"""Full-screen Top interaction; observation workers never own terminal input."""

from __future__ import annotations

import asyncio
from io import StringIO

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.input import Input
from prompt_toolkit.output import Output
from rich.console import Console

from toolang.up.top import TopObserver

from .top_view import PERIODS, RECENTS, TopView, next_value


def top_application(
    observer: TopObserver,
    view: TopView,
    *,
    input: Input | None = None,
    output: Output | None = None,
) -> Application[None]:
    bindings = KeyBindings()
    snapshot = observer.tick()

    def quit(event):
        event.app.exit()

    for key in ("f10", "q", "c-c"):
        bindings.add(key)(quit)

    @bindings.add("f1")
    def help_(event):
        view.help = not view.help
        view.details = False

    def details(event):
        view.details = not view.details
        view.help = False

    bindings.add("f3")(details)
    bindings.add("enter")(details)

    @bindings.add("escape")
    def close(event):
        view.details = view.help = False

    @bindings.add("f5")
    def list_(event):
        view.list_mode = not view.list_mode

    @bindings.add("f7")
    def recent(event):
        observer.set_recent(next_value(RECENTS, observer.recent))

    @bindings.add("f8")
    def period(event):
        observer.set_period(next_value(PERIODS, observer.period))

    @bindings.add("up")
    def up(event):
        view.move(-1)

    @bindings.add("down")
    def down(event):
        view.move(1)
        if view.rows and view.selected == view.rows[-1].key:
            observer.more()

    @bindings.add("left")
    def left(event):
        view.left()

    @bindings.add("right")
    def right(event):
        view.right()

    @bindings.add("pageup")
    def pageup(event):
        view.move(-view.page_size)

    @bindings.add("pagedown")
    def pagedown(event):
        view.move(view.page_size)
        observer.more()

    def content():
        size = app.output.get_size()
        text = view.render(snapshot, width=size.columns, height=size.rows)
        output = StringIO()
        Console(
            file=output,
            width=size.columns,
            force_terminal=True,
            color_system="truecolor",
        ).print(text, end="", soft_wrap=True)
        return ANSI(output.getvalue())

    async def refresh() -> None:
        nonlocal snapshot
        while True:
            snapshot = observer.tick()
            app.invalidate()
            await asyncio.sleep(0.1)

    app: Application[None] = Application(
        layout=Layout(Window(FormattedTextControl(content), wrap_lines=False)),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=False,
        input=input,
        output=output,
    )
    app.pre_run_callables.append(lambda: app.create_background_task(refresh()))
    return app


def run_top(observer: TopObserver, view: TopView) -> None:
    top_application(observer, view).run()

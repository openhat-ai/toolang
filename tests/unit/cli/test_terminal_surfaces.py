"""Offline terminal-surface tests; PTYs never query the user's terminal."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from typing import Any
from unittest.mock import patch

import pytest

from toolang.cli.common import terminal_surfaces as surfaces

ROOT = Path(__file__).parents[3]


def test_import_is_silent() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import toolang.cli.common.terminal_surfaces"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    assert result.stdout == b""
    assert result.stderr == b""


def test_fixed_schemes_bypass_terminal_probe() -> None:
    with patch.object(
        surfaces,
        "_query_terminal_defaults",
        side_effect=AssertionError("unexpected probe"),
    ):
        dark = surfaces.resolve_terminal_surfaces(
            environment={surfaces.COLOR_SCHEME_ENV: " DARK "}
        )
        light = surfaces.resolve_terminal_surfaces(
            environment={surfaces.COLOR_SCHEME_ENV: "light"}
        )

    assert dark == surfaces.DARK_TERMINAL_SURFACES
    assert light == surfaces.LIGHT_TERMINAL_SURFACES


def test_explicit_surfaces_use_input_queue_code_order_without_derivation() -> None:
    with patch.object(
        surfaces,
        "_query_terminal_defaults",
        side_effect=AssertionError("unexpected probe"),
    ):
        resolved = surfaces.resolve_terminal_surfaces(
            environment={surfaces.COLOR_SCHEME_ENV: " #ABCDEF, #123456, #FEDCBA "}
        )

    assert resolved == surfaces.TerminalSurfaces(
        input_background="#abcdef",
        queue_background="#123456",
        code_background="#fedcba",
    )


@pytest.mark.parametrize(
    "value",
    (
        "unknown",
        "#111111",
        "#111111,#222222",
        "#111111,#222222,#333333,#444444",
        "red,#222222,#333333",
        "#111,#222,#333",
        "#gggggg,#222222,#333333",
    ),
)
def test_invalid_explicit_scheme_reports_the_public_order(value: str) -> None:
    with pytest.raises(ValueError, match="input,queue,code"):
        surfaces.resolve_terminal_surfaces(
            environment={surfaces.COLOR_SCHEME_ENV: value},
            probe=False,
        )


def test_empty_configuration_uses_complete_osc_defaults() -> None:
    with patch.object(
        surfaces,
        "_query_terminal_defaults",
        return_value=((212, 212, 212), (30, 30, 30)),
    ) as query:
        resolved = surfaces.resolve_terminal_surfaces(
            environment={surfaces.COLOR_SCHEME_ENV: "  "}
        )

    query.assert_called_once()
    assert resolved == surfaces.TerminalSurfaces(
        input_background="#313131",
        queue_background="#272727",
        code_background="#222222",
    )


def test_failed_probe_defaults_dark_and_ignores_colorfgbg() -> None:
    with patch.object(surfaces, "_query_terminal_defaults", return_value=None):
        resolved = surfaces.resolve_terminal_surfaces(environment={"COLORFGBG": "0;15"})

    assert resolved == surfaces.DARK_TERMINAL_SURFACES


def test_disabled_probe_defaults_dark() -> None:
    with patch.object(
        surfaces,
        "_query_terminal_defaults",
        side_effect=AssertionError("unexpected probe"),
    ):
        resolved = surfaces.resolve_terminal_surfaces(
            environment={},
            probe=False,
        )

    assert resolved == surfaces.DARK_TERMINAL_SURFACES


@pytest.mark.parametrize("timeout", (0, 4, float("nan"), float("inf")))
def test_invalid_timeout_fails_before_probing(timeout: float) -> None:
    with (
        patch.object(
            surfaces,
            "_query_terminal_defaults",
            side_effect=AssertionError("unexpected probe"),
        ),
        pytest.raises(ValueError, match="timeout"),
    ):
        surfaces.resolve_terminal_surfaces(environment={}, timeout=timeout)


def test_non_tty_probe_is_silent() -> None:
    output = io.StringIO()

    resolved = surfaces.resolve_terminal_surfaces(
        environment={},
        input_stream=io.StringIO(),
        output_stream=output,
    )

    assert output.getvalue() == ""
    assert resolved == surfaces.DARK_TERMINAL_SURFACES


def test_non_posix_probe_defaults_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surfaces.os, "name", "nt")

    resolved = surfaces.resolve_terminal_surfaces(environment={})

    assert resolved == surfaces.DARK_TERMINAL_SURFACES


def test_terminal_stream_error_defaults_dark() -> None:
    class BrokenTty(io.StringIO):
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            raise OSError("unavailable terminal")

    resolved = surfaces.resolve_terminal_surfaces(
        environment={},
        input_stream=BrokenTty(),
        output_stream=BrokenTty(),
    )

    assert resolved == surfaces.DARK_TERMINAL_SURFACES


@pytest.mark.parametrize(
    ("background", "foreground", "expected"),
    (
        (
            "#000000",
            "#ffffff",
            surfaces.TerminalSurfaces("#1f1f1f", "#121212", "#0b0b0b"),
        ),
        (
            "#ffffff",
            "#000000",
            surfaces.TerminalSurfaces("#e3e3e3", "#f2f2f2", "#f9f9f9"),
        ),
        (
            "#1e1e1e",
            "#d4d4d4",
            surfaces.TerminalSurfaces("#313131", "#272727", "#222222"),
        ),
        (
            "#fafafa",
            "#202020",
            surfaces.TerminalSurfaces("#dfdfdf", "#ededed", "#f4f4f4"),
        ),
        (
            "#002b36",
            "#93a1a1",
            surfaces.TerminalSurfaces("#213942", "#14323b", "#0a2e39"),
        ),
    ),
)
def test_derivation_matches_reference_palettes(
    background: str,
    foreground: str,
    expected: surfaces.TerminalSurfaces,
) -> None:
    assert (
        surfaces.derive_terminal_surfaces(
            foreground=foreground,
            background=background,
        )
        == expected
    )


def test_derived_surfaces_preserve_readable_terminal_text() -> None:
    foreground = "#777777"
    resolved = surfaces.derive_terminal_surfaces(
        foreground=foreground,
        background="#000000",
    )
    fg = surfaces._parse_hex_rgb(foreground)

    for color in (
        resolved.code_background,
        resolved.queue_background,
        resolved.input_background,
    ):
        assert surfaces._contrast(fg, surfaces._parse_hex_rgb(color)) >= 4.49


@pytest.mark.parametrize(
    ("foreground", "background"),
    (
        ("#e8dd50", "#045baa"),
        ("#4a4c9d", "#62e566"),
        ("#9e94be", "#2cacc6"),
    ),
)
def test_contrast_cap_preserves_surface_order_on_a_tinted_theme(
    foreground: str,
    background: str,
) -> None:
    resolved = surfaces.derive_terminal_surfaces(
        foreground=foreground,
        background=background,
    )
    fg = surfaces._parse_hex_rgb(foreground)
    bg = surfaces._parse_hex_rgb(background)
    colors = tuple(
        surfaces._parse_hex_rgb(color)
        for color in (
            resolved.code_background,
            resolved.queue_background,
            resolved.input_background,
        )
    )

    minimum_text = min(surfaces.MINIMUM_TEXT_CONTRAST, surfaces._contrast(fg, bg))
    assert all(surfaces._contrast(fg, color) >= minimum_text for color in colors)
    code, queue, input_ = (surfaces._contrast(bg, color) for color in colors)
    assert code < queue < input_


def test_osc_parser_accepts_scaled_channels_and_both_terminators() -> None:
    replies = surfaces._parse_osc_replies(
        b"\x1b]10;rgb:f/8/0\x07\x1b]11;rgb:1111/2222/3333\x1b\\"
    )

    assert replies == {10: (255, 136, 0), 11: (17, 34, 51)}
    assert surfaces._parse_osc_replies(b"\x1b]11;not-rgb\x07") == {}


@pytest.mark.skipif(os.name != "posix", reason="terminal probing is POSIX-only")
class TestTerminalProbe:
    @staticmethod
    def _payload(output: bytes) -> dict[str, str]:
        return json.loads(output[output.rfind(b"{") :])

    def _run_probe(self, mode: str) -> tuple[int, bytes]:
        import pty
        import termios

        def input_mode(fd: int) -> list[Any]:
            attributes = termios.tcgetattr(fd)
            attributes[3] &= ~getattr(termios, "PENDIN", 0)
            return attributes

        master, slave = pty.openpty()
        original = input_mode(slave)
        if mode == "pending":
            os.write(master, b"pending input\n")
        elif mode == "pending_partial":
            os.write(master, b"x")
        program = (
            "import json, sys\n"
            "from dataclasses import asdict\n"
            "from toolang.cli.common.terminal_surfaces import "
            "resolve_terminal_surfaces\n"
            "try:\n"
            " print(json.dumps(asdict(resolve_terminal_surfaces("
            "environment={}, timeout=0.1))))\n"
            "except KeyboardInterrupt:\n"
            " sys.exit(130)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=ROOT,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        data = b""
        sent = False
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.02)[0]:
                    data += os.read(master, 65536)
                if not sent and surfaces._OSC_QUERY in data:
                    sent = True
                    if mode in {"success", "partial"}:
                        os.write(master, b"\x1b]10;rgb:dddd/dddd/dddd\x07")
                        if mode == "success":
                            os.write(
                                master,
                                b"\x1b]11;rgb:1111/1111/1111\x1b\\",
                            )
                    elif mode == "interrupt":
                        child.send_signal(signal.SIGINT)
                if child.poll() is not None:
                    while select.select([master], [], [], 0)[0]:
                        data += os.read(master, 65536)
                    break
            assert child.poll() is not None, "probe did not terminate"
            assert input_mode(slave) == original
            if mode == "pending":
                assert not sent
                assert select.select([slave], [], [], 0)[0]
                assert os.read(slave, 100) == b"pending input\n"
            elif mode == "pending_partial":
                assert not sent
                os.write(master, b"\n")
                assert select.select([slave], [], [], 1)[0]
                assert os.read(slave, 100) == b"x\n"
            else:
                assert sent
            return child.wait(timeout=1), data
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            os.close(master)
            os.close(slave)

    def test_success_restores_input_mode(self) -> None:
        code, output = self._run_probe("success")

        assert code == 0
        payload = self._payload(output)
        assert payload == {
            "input_background": "#282828",
            "queue_background": "#1d1d1d",
            "code_background": "#171717",
        }
        assert output.count(b"\x1b]") == 2

    @pytest.mark.parametrize("mode", ("timeout", "partial"))
    def test_incomplete_probe_restores_input_mode_and_defaults_dark(
        self, mode: str
    ) -> None:
        code, output = self._run_probe(mode)

        assert code == 0
        payload = self._payload(output)
        assert payload == {
            "input_background": "#1f1f1f",
            "queue_background": "#121212",
            "code_background": "#0b0b0b",
        }

    def test_interrupt_restores_input_mode(self) -> None:
        code, _ = self._run_probe("interrupt")

        assert code == 130

    def test_pending_input_is_not_consumed(self) -> None:
        code, output = self._run_probe("pending")

        assert code == 0
        assert b'"input_background": "#1f1f1f"' in output

    def test_pending_input_without_newline_is_not_consumed(self) -> None:
        code, output = self._run_probe("pending_partial")

        assert code == 0
        assert b'"input_background": "#1f1f1f"' in output

    def test_mismatched_terminals_do_not_probe(self) -> None:
        import pty

        first_master, first_slave = pty.openpty()
        second_master, second_slave = pty.openpty()
        input_stream = os.fdopen(first_slave, "r", closefd=False)
        output_stream = os.fdopen(second_slave, "w", closefd=False)
        try:
            assert (
                surfaces.resolve_terminal_surfaces(
                    environment={},
                    input_stream=input_stream,
                    output_stream=output_stream,
                )
                == surfaces.DARK_TERMINAL_SURFACES
            )
            assert not select.select([first_master], [], [], 0)[0]
            assert not select.select([second_master], [], [], 0)[0]
        finally:
            input_stream.close()
            output_stream.close()
            os.close(first_master)
            os.close(first_slave)
            os.close(second_master)
            os.close(second_slave)

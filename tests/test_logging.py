from __future__ import annotations

import datetime as _datetime
import io
import logging
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.logging import (
    LudosHandler,
    _NullProgress,
    _PlainConsole,
    configure_tracebacks,
    confirm,
    piter,
    pstream,
)


class LudosLoggingTests(unittest.TestCase):
    def test_repeated_timestamp_keeps_padding(self) -> None:
        output = io.StringIO()
        handler = LudosHandler()
        created = _datetime.datetime(2026, 6, 16, 12, 34).timestamp()

        with (
            patch("ludos.logging.AGENT", False),
            patch(
                "ludos.logging.console",
                _PlainConsole(file=output, force_terminal=False),
            ),
        ):
            handler._emit_lines(logging.INFO, "INFO", created, ["first"])
            handler._emit_lines(logging.INFO, "INFO", created + 10, ["second"])

        self.assertEqual(
            output.getvalue(),
            "[12:34] first\n        second\n",
        )

    def test_multiline_info_uses_exact_continuation_indent(self) -> None:
        output = io.StringIO()
        handler = LudosHandler()
        created = _datetime.datetime(2026, 6, 16, 12, 34).timestamp()

        with (
            patch("ludos.logging.AGENT", False),
            patch(
                "ludos.logging.console",
                _PlainConsole(file=output, force_terminal=False),
            ),
        ):
            handler._emit_lines(
                logging.INFO,
                "INFO",
                created,
                [
                    "Importing localhost/anatase:f44-x86_64",
                    "| OT: using fuse: 0",
                ],
            )

        self.assertEqual(
            output.getvalue(),
            "[12:34] Importing localhost/anatase:f44-x86_64\n"
            "        | OT: using fuse: 0\n",
        )

    def test_terminal_log_lines_use_tqdm_write(self) -> None:
        output = io.StringIO()
        handler = LudosHandler()
        created = _datetime.datetime(2026, 6, 16, 12, 34).timestamp()

        with (
            patch("ludos.logging.AGENT", False),
            patch(
                "ludos.logging.console",
                _PlainConsole(file=output, force_terminal=True),
            ),
            patch("ludos.logging.tqdm.write") as write,
        ):
            handler._emit_lines(logging.INFO, "INFO", created, ["progress-safe"])

        write.assert_called_once_with("[12:34] progress-safe", file=output)
        self.assertEqual(output.getvalue(), "")

    def test_piter_is_hidden_when_not_terminal(self) -> None:
        output = io.StringIO()
        console = _PlainConsole(file=output, force_terminal=False)

        with (
            patch("ludos.logging.AGENT", False),
            patch("ludos.logging.console", console),
            patch("ludos.logging.tqdm") as tqdm,
        ):
            piter(total=5, desc="Importing OSTree")

        tqdm.assert_called_once()
        self.assertIs(tqdm.call_args.kwargs["disable"], True)
        self.assertIs(tqdm.call_args.kwargs["file"], output)

    def test_piter_is_visible_on_terminal_outside_agent(self) -> None:
        output = io.StringIO()
        console = _PlainConsole(file=output, force_terminal=True)

        with (
            patch("ludos.logging.AGENT", False),
            patch("ludos.logging.console", console),
            patch("ludos.logging.tqdm") as tqdm,
        ):
            piter(total=5, desc="Importing OSTree")

        tqdm.assert_called_once()
        self.assertIs(tqdm.call_args.kwargs["disable"], False)
        self.assertIs(tqdm.call_args.kwargs["file"], output)

    def test_pstream_uses_tqdm_write_on_terminal(self) -> None:
        output = io.StringIO()
        console = _PlainConsole(file=output, force_terminal=True)

        with (
            patch("ludos.logging.AGENT", False),
            patch("ludos.logging.console", console),
            patch("ludos.logging.tqdm.write") as write,
        ):
            pstream("OT: ingesting file")

        write.assert_called_once_with("        | OT: ingesting file")

    def test_pstream_falls_back_to_stream_when_hidden(self) -> None:
        output = io.StringIO()
        console = _PlainConsole(file=output, force_terminal=False)

        with (
            patch("ludos.logging.AGENT", False),
            patch("ludos.logging.console", console),
            patch("ludos.logging.stream") as stream,
        ):
            pstream("OT: ingesting file")

        stream.assert_called_once_with("OT: ingesting file")

    def test_terminal_log_lines_write_directly_without_tqdm(self) -> None:
        output = io.StringIO()
        handler = LudosHandler()
        created = _datetime.datetime(2026, 6, 16, 12, 34).timestamp()

        with (
            patch("ludos.logging.AGENT", False),
            patch(
                "ludos.logging.console",
                _PlainConsole(file=output, force_terminal=True),
            ),
            patch("ludos.logging.tqdm", None),
        ):
            handler._emit_lines(logging.INFO, "INFO", created, ["plain"])

        self.assertEqual(output.getvalue(), "[12:34] plain\n")

    def test_pstream_uses_stream_without_tqdm(self) -> None:
        with (
            patch("ludos.logging.AGENT", False),
            patch(
                "ludos.logging.console",
                _PlainConsole(file=io.StringIO(), force_terminal=True),
            ),
            patch("ludos.logging.tqdm", None),
            patch("ludos.logging.stream") as stream,
        ):
            pstream("OT: ingesting file")

        stream.assert_called_once_with("OT: ingesting file")

    def test_piter_without_tqdm_preserves_progress_protocol(self) -> None:
        with patch("ludos.logging.tqdm", None):
            progress = piter(["a", "b"], desc="Silent")

        self.assertIsInstance(progress, _NullProgress)
        self.assertEqual(progress.total, 2)
        self.assertEqual(list(progress), ["a", "b"])
        self.assertEqual(progress.n, 2)
        progress.total = 5
        progress.refresh()
        progress.update(2)
        progress.close()
        self.assertEqual(progress.n, 4)

        with progress as entered:
            self.assertIs(entered, progress)

    def test_configure_tracebacks_without_rich_is_noop(self) -> None:
        with patch("ludos.logging.install_rich_traceback", None):
            configure_tracebacks()

    def test_logging_imports_without_site_packages(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(source)
        script = "\n".join(
            (
                "import ludos.logging as logging",
                "assert logging.RichConsole is None",
                "assert logging.tqdm is None",
                "logging.configure_tracebacks()",
                "progress = logging.piter([1, 2])",
                "assert list(progress) == [1, 2]",
                "assert progress.n == 2",
            )
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_confirm_pads_prompt_outside_agent(self) -> None:
        with (
            patch("ludos.logging.AGENT", False),
            patch("builtins.input", return_value="y") as input_mock,
        ):
            self.assertTrue(confirm("Update card:pkg"))

        input_mock.assert_called_once_with("        Update card:pkg [y/N] ")

    def test_confirm_does_not_pad_prompt_in_agent(self) -> None:
        with (
            patch("ludos.logging.AGENT", True),
            patch("builtins.input", return_value="yes") as input_mock,
        ):
            self.assertTrue(confirm("Update card:pkg"))

        input_mock.assert_called_once_with("Update card:pkg [y/N] ")

    def test_confirm_declines_blank_answer(self) -> None:
        with patch("builtins.input", return_value=""):
            self.assertFalse(confirm("Update card:pkg"))

    def test_confirm_accepts_blank_answer_when_default_is_yes(self) -> None:
        with (
            patch("ludos.logging.AGENT", False),
            patch("builtins.input", return_value="") as input_mock,
        ):
            self.assertTrue(confirm("Replace image", default=True))

        input_mock.assert_called_once_with("        Replace image [Y/n] ")

    def test_confirm_declines_eof(self) -> None:
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(confirm("Update card:pkg"))


if __name__ == "__main__":
    unittest.main()

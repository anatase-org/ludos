from __future__ import annotations

import datetime as _datetime
import io
import logging
import unittest
from unittest.mock import patch

from rich.console import Console

from ludos.logging import LudosHandler


class LudosLoggingTests(unittest.TestCase):
    def test_repeated_timestamp_keeps_padding(self) -> None:
        output = io.StringIO()
        handler = LudosHandler()
        created = _datetime.datetime(2026, 6, 16, 12, 34).timestamp()

        with (
            patch("ludos.logging.AGENT", False),
            patch(
                "ludos.logging.console",
                Console(file=output, force_terminal=False, color_system=None),
            ),
        ):
            handler._emit_lines(logging.INFO, "INFO", created, ["first"])
            handler._emit_lines(logging.INFO, "INFO", created + 10, ["second"])

        self.assertEqual(
            output.getvalue(),
            "[12:34] first\n        second\n",
        )


if __name__ == "__main__":
    unittest.main()

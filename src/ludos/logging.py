from __future__ import annotations

import datetime as _datetime
import logging

from rich.console import Console
from rich.text import Text
from rich.traceback import install as install_rich_traceback


console = Console()
error_console = Console(stderr=True)


LOGO_STR = r""".____          .___              ╭──────────/┐
│   / __ __  __│ _/____  ____/\ ╭──────────/┐│
│  │ │  │  \/ __ │/ __ \/  ___/┏━━━━━━━━━━━┓││
│  │ │  │  / /_/ ( /_/  )___ \ ┃ L       ♦ ┃││
│  │ \____/\____ /\____/____ │ ┃   U       ┃││
│   \______________________/ / ┃     ♦     ┃││
 \_________________________ /  ┃       O   ┃│╯
                          \/   ┃ ♦       S ┃╯
                               ┗━━━━━━━━━━━┛"""


class LudosHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._last_timestamp: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        timestamp = _datetime.datetime.fromtimestamp(record.created).strftime("%H:%M")
        time_prefix = f"[{timestamp}]" if timestamp != self._last_timestamp else " " * 7
        self._last_timestamp = timestamp
        level = f" {record.levelname}" if record.levelno >= logging.WARNING else ""
        prefix = f"{time_prefix}{level} "
        target = error_console if record.levelno >= logging.WARNING else console
        message = record.getMessage()
        lines = message.splitlines() or [""]
        for index, line in enumerate(lines):
            line_prefix = prefix if index == 0 else " " * len(prefix)
            target.print(Text(line_prefix, no_wrap=True), line, sep="")
        target.file.flush()


logger = logging.getLogger("ludos")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.handlers.clear()
logger.addHandler(LudosHandler())


def configure_tracebacks() -> None:
    install_rich_traceback(show_locals=False, suppress=[])


def log(message: object = "") -> None:
    logger.info("%s", message)


def warning(message: object = "") -> None:
    logger.warning("%s", message)


def error(message: object = "") -> None:
    logger.error("%s", message)


def stream(message: str) -> None:
    for line in message.splitlines():
        logger.info("| %s", line)


def main() -> None:
    log(LOGO_STR)


if __name__ == "__main__":
    main()

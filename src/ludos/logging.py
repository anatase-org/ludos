from __future__ import annotations

import datetime as _datetime
import logging
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.errors import MarkupError
from rich.text import Text
from rich.traceback import install as install_rich_traceback


console = Console()
error_console = Console(stderr=True)

STREAM_HISTORY_LIMIT = 15
STREAM_TRUNCATED_LINE = "| ... <truncated>"
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "ludos.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


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
        self._stream_lines: list[str] = []
        self._stream_created: float | None = None
        self._stream_rendered_lines = 0

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "ludos_stream", False):
            self._emit_stream(record)
            return

        self._bake_stream_history()
        self._emit_record(record)

    def _emit_record(self, record: logging.LogRecord) -> None:
        lines = record.getMessage().splitlines() or [""]
        self._emit_lines(record.levelno, record.levelname, record.created, lines)

    def _emit_lines(
        self, levelno: int, levelname: str, created: float, lines: list[str]
    ) -> None:
        timestamp = _datetime.datetime.fromtimestamp(created).strftime("%H:%M")
        time_prefix = f"[{timestamp}]" if timestamp != self._last_timestamp else " " * 7
        self._last_timestamp = timestamp
        target = error_console if levelno >= logging.WARNING else console
        for index, line in enumerate(lines):
            if index == 0:
                line_prefix = Text(time_prefix, no_wrap=True)
                if levelno >= logging.ERROR:
                    line_prefix.append(f" {levelname}:", style="red")
                elif levelno >= logging.WARNING:
                    line_prefix.append(f" {levelname}:", style="yellow")
                line_prefix.append(" ")
            else:
                width = 8 + (len(levelname) + 2 if levelno >= logging.WARNING else 0)
                line_prefix = Text(" " * width, no_wrap=True)
            try:
                target.print(line_prefix, line, sep="")
            except MarkupError:
                target.print(line_prefix, line, sep="", markup=False)
        target.file.flush()

    def _emit_stream(self, record: logging.LogRecord) -> None:
        lines = self._stream_record_lines(record.getMessage())
        if not self._supports_ephemeral_stream():
            self._emit_lines(logging.INFO, "INFO", record.created, lines)
            return

        if self._stream_created is None:
            self._stream_created = record.created
        self._stream_lines.extend(lines)
        self._render_stream()

    def _stream_record_lines(self, message: str) -> list[str]:
        return [f"| {line}" for line in message.splitlines() or [""]]

    def _supports_ephemeral_stream(self) -> bool:
        return console.is_terminal

    def _stream_display_limit(self) -> int:
        terminal_lines = shutil.get_terminal_size(fallback=(80, 24)).lines
        return max(1, terminal_lines - 1)

    def _stream_snapshot(self, limit: int) -> list[str]:
        if len(self._stream_lines) <= limit:
            return self._stream_lines.copy()
        if limit == 1:
            return [STREAM_TRUNCATED_LINE]
        return [STREAM_TRUNCATED_LINE, *self._stream_lines[-(limit - 1) :]]

    def _render_stream(self) -> None:
        self._clear_stream_display()
        lines = self._stream_snapshot(self._stream_display_limit())
        for line in lines:
            console.print(line, markup=False, no_wrap=True, overflow="crop")
        self._stream_rendered_lines = len(lines)
        console.file.flush()

    def _clear_stream_display(self) -> None:
        if self._stream_rendered_lines <= 0:
            return
        console.file.write("\033[F\033[2K" * self._stream_rendered_lines)
        console.file.flush()
        self._stream_rendered_lines = 0

    def _bake_stream_history(self) -> None:
        if not self._stream_lines:
            return

        self._clear_stream_display()
        lines = self._stream_snapshot(STREAM_HISTORY_LIMIT)
        created = self._stream_created if self._stream_created is not None else 0
        self._stream_lines.clear()
        self._stream_created = None
        self._emit_lines(logging.INFO, "INFO", created, lines)


class LudosFileFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = _datetime.datetime.fromtimestamp(record.created).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"

        if getattr(record, "ludos_stream", False):
            lines = [f"| {line}" for line in message.splitlines() or [""]]
        else:
            lines = message.splitlines() or [""]

        prefix = f"[{timestamp}] {record.levelname}: "
        continuation = " " * len(prefix)
        return "\n".join(
            f"{prefix if index == 0 else continuation}{line}"
            for index, line in enumerate(lines)
        )


def _make_file_handler() -> logging.Handler:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(LudosFileFormatter())
    return handler


logger = logging.getLogger("ludos")
logger.setLevel(logging.INFO)
logger.propagate = False
logger.handlers.clear()
logger.addHandler(LudosHandler())
logger.addHandler(_make_file_handler())


def configure_tracebacks() -> None:
    install_rich_traceback(show_locals=False, suppress=[])


def log(message: object = "") -> None:
    logger.info("%s", message)


def warning(message: object = "") -> None:
    logger.warning("%s", message)


def error(message: object = "") -> None:
    logger.error("%s", message)


def stream(message: str) -> None:
    logger.info("%s", message, extra={"ludos_stream": True})


def main() -> None:
    log(LOGO_STR)


if __name__ == "__main__":
    main()

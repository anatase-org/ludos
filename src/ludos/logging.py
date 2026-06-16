from __future__ import annotations

import datetime as _datetime
import logging
import os
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.errors import MarkupError
from rich.text import Text
from rich.traceback import install as install_rich_traceback


console = Console()
error_console = Console(stderr=True)

AGENT = os.environ.get("CODEX_CI") == "1" or os.environ.get("AGENT") == "1"
STREAM_HISTORY_LIMIT = 15
STREAM_TRUNCATED_LINE = "| ... <truncated>"
INFO_MESSAGE_INDENT = 8
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "ludos.log"
LOG_BACKUP_COUNT = 5
LOG_ROTATE_ON_START = True


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
        self._stream_rendered_snapshot: list[str] = []

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
        if AGENT:
            time_prefix = ""
        else:
            timestamp = _datetime.datetime.fromtimestamp(created).strftime("%H:%M")
            time_prefix = (
                f"[{timestamp}]" if timestamp != self._last_timestamp else " " * 7
            )
            self._last_timestamp = timestamp
        target = error_console if levelno >= logging.WARNING else console
        for index, line in enumerate(lines):
            if index == 0:
                line_prefix = Text(time_prefix, no_wrap=True)
                prefix_has_text = bool(time_prefix)
                if levelno >= logging.ERROR:
                    if prefix_has_text:
                        line_prefix.append(" ")
                    line_prefix.append(f"{levelname}:", style="red")
                    prefix_has_text = True
                elif levelno >= logging.WARNING:
                    if prefix_has_text:
                        line_prefix.append(" ")
                    line_prefix.append(f"{levelname}:", style="yellow")
                    prefix_has_text = True
                if prefix_has_text:
                    line_prefix.append(" ")
            else:
                width = (
                    (len(time_prefix) + 1 if time_prefix else 0)
                    + (len(levelname) + 2 if levelno >= logging.WARNING else 0)
                )
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
        return console.is_terminal and not AGENT

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
        lines = self._stream_snapshot(self._stream_display_limit())
        if lines == self._stream_rendered_snapshot:
            return

        old_lines = self._stream_rendered_snapshot
        shared_lines = self._shared_prefix_length(old_lines, lines)
        output: list[str] = []

        if old_lines:
            output.append("\033[F" * (len(old_lines) - shared_lines))

        rendered_suffix = self._render_stream_lines(lines[shared_lines:])
        if rendered_suffix:
            for rendered_line in rendered_suffix.splitlines(keepends=True):
                output.append("\033[2K")
                output.append(rendered_line)

        stale_lines = len(old_lines) - len(lines)
        if stale_lines > 0:
            output.append("\033[2K\n" * stale_lines)
            output.append("\033[F" * stale_lines)

        console.file.write("".join(output))
        self._stream_rendered_snapshot = lines
        self._stream_rendered_lines = len(lines)
        console.file.flush()

    def _render_stream_lines(self, lines: list[str]) -> str:
        if not lines:
            return ""
        with console.capture() as capture:
            for line in lines:
                console.print(
                    " " * INFO_MESSAGE_INDENT + line,
                    markup=False,
                    no_wrap=True,
                    overflow="crop",
                )
        return capture.get()

    def _shared_prefix_length(self, old_lines: list[str], new_lines: list[str]) -> int:
        shared_lines = 0
        for old_line, new_line in zip(old_lines, new_lines):
            if old_line != new_line:
                break
            shared_lines += 1
        return shared_lines

    def _clear_stream_display(self) -> None:
        if self._stream_rendered_lines <= 0:
            return
        console.file.write("\033[F\033[2K" * self._stream_rendered_lines)
        console.file.flush()
        self._stream_rendered_lines = 0
        self._stream_rendered_snapshot = []

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
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"

        if getattr(record, "ludos_stream", False):
            lines = [f"| {line}" for line in message.splitlines() or [""]]
        else:
            lines = message.splitlines() or [""]

        if record.levelno < logging.WARNING:
            return "\n".join(lines)

        return "\n".join(
            f"{record.levelname}: {line}" if index == 0 else line
            for index, line in enumerate(lines)
        )


class SkipLogoFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != LOGO_STR


def _make_file_handler() -> logging.Handler:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=0,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    if LOG_ROTATE_ON_START and LOG_FILE.exists() and LOG_FILE.stat().st_size > 0:
        handler.doRollover()
    handler.addFilter(SkipLogoFilter())
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

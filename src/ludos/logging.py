from __future__ import annotations

import datetime as _datetime
import io
import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator, TextIO


class _PlainMarkupError(Exception):
    pass

try:
    from rich.console import Console as RichConsole
    from rich.errors import MarkupError as RichMarkupError
    from rich.text import Text as RichText
    from rich.traceback import install as install_rich_traceback
except ImportError:
    RichConsole = None  # type: ignore[assignment,misc]
    RichMarkupError = _PlainMarkupError  # type: ignore[assignment,misc]
    RichText = None  # type: ignore[assignment,misc]
    install_rich_traceback = None

try:
    from tqdm.auto import tqdm
except ImportError:
    tqdm = None  # type: ignore[assignment,misc]


class _PlainCapture:
    def __init__(self, console: "_PlainConsole") -> None:
        self.console = console
        self.output = io.StringIO()
        self.previous_file: TextIO | None = None

    def __enter__(self) -> "_PlainCapture":
        self.previous_file = self.console.file
        self.console.file = self.output
        return self

    def __exit__(self, *args: object) -> None:
        assert self.previous_file is not None
        self.console.file = self.previous_file

    def get(self) -> str:
        return self.output.getvalue()


class _PlainConsole:
    def __init__(
        self,
        *,
        file: TextIO | None = None,
        stderr: bool = False,
        force_terminal: bool | None = None,
        **_: object,
    ) -> None:
        self.file = file if file is not None else (sys.stderr if stderr else sys.stdout)
        self.force_terminal = force_terminal

    @property
    def is_terminal(self) -> bool:
        if self.force_terminal is not None:
            return self.force_terminal
        isatty = getattr(self.file, "isatty", None)
        return bool(isatty and isatty())

    def print(
        self,
        *objects: object,
        sep: str = " ",
        end: str = "\n",
        **_: object,
    ) -> None:
        self.file.write(sep.join(str(item) for item in objects) + end)

    def capture(self) -> _PlainCapture:
        return _PlainCapture(self)


class _NullProgress:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.iterable = args[0] if args else None
        total = kwargs.get("total")
        if total is None and self.iterable is not None:
            try:
                total = len(self.iterable)  # type: ignore[arg-type]
            except TypeError:
                pass
        self.total = total
        self.n: int | float = 0

    def __enter__(self) -> "_NullProgress":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[Any]:
        if self.iterable is None:
            return
        for item in self.iterable:  # type: ignore[union-attr]
            yield item
            self.update()

    def update(self, amount: int | float = 1) -> None:
        self.n += amount

    def refresh(self) -> None:
        pass

    def close(self) -> None:
        pass


console = RichConsole() if RichConsole is not None else _PlainConsole()
error_console = (
    RichConsole(stderr=True)
    if RichConsole is not None
    else _PlainConsole(stderr=True)
)

AGENT = os.environ.get("CODEX_CI") == "1" or os.environ.get("AGENT") == "1" or os.environ.get("GITHUB_ACTIONS") 
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
                line_prefix = time_prefix
                prefix_has_text = bool(time_prefix)
                if levelno >= logging.ERROR:
                    if prefix_has_text:
                        line_prefix += " "
                    line_prefix += f"{levelname}:"
                    prefix_has_text = True
                elif levelno >= logging.WARNING:
                    if prefix_has_text:
                        line_prefix += " "
                    line_prefix += f"{levelname}:"
                    prefix_has_text = True
                if prefix_has_text:
                    line_prefix += " "
            else:
                width = (
                    (len(time_prefix) + 1 if time_prefix else 0)
                    + (len(levelname) + 2 if levelno >= logging.WARNING else 0)
                )
                line_prefix = " " * width
            rendered_prefix: object = line_prefix
            if RichText is not None:
                rendered_prefix = RichText(line_prefix, no_wrap=True)
                if index == 0 and levelno >= logging.WARNING:
                    label = f"{levelname}:"
                    start = line_prefix.index(label)
                    style = "red" if levelno >= logging.ERROR else "yellow"
                    rendered_prefix.stylize(style, start, start + len(label))
            self._emit_rendered_line(target, rendered_prefix, line)
        target.file.flush()

    def _emit_rendered_line(
        self,
        target: Any,
        line_prefix: object,
        line: str,
    ) -> None:
        if self._should_use_tqdm_write(target):
            with target.capture() as capture:
                self._print_line(target, line_prefix, line)
            assert tqdm is not None
            tqdm.write(capture.get().rstrip("\n"), file=target.file)
            return
        self._print_line(target, line_prefix, line)

    def _print_line(self, target: Any, line_prefix: object, line: str) -> None:
        prefix = (
            RichText(line_prefix, no_wrap=True)
            if RichText is not None and not isinstance(line_prefix, RichText)
            else line_prefix
        )
        try:
            target.print(prefix, line, sep="")
        except RichMarkupError:
            target.print(prefix, line, sep="", markup=False)

    def _should_use_tqdm_write(self, target: Any) -> bool:
        return tqdm is not None and target.is_terminal and not AGENT

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
_logging_configured = False


def configure_logging() -> None:
    global _logging_configured

    if _logging_configured:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    logger.addHandler(LudosHandler())
    logger.addHandler(_make_file_handler())
    _logging_configured = True


def configure_tracebacks() -> None:
    if install_rich_traceback is not None:
        install_rich_traceback(show_locals=False, suppress=[])


def log(message: object = "") -> None:
    logger.info("%s", message)


def warning(message: object = "") -> None:
    logger.warning("%s", message)


def error(message: object = "") -> None:
    logger.error("%s", message)


def confirm(message: str) -> bool:
    prefix = "" if AGENT else " " * INFO_MESSAGE_INDENT
    try:
        response = input(f"{prefix}{message} [y/N] ")
    except EOFError:
        return False
    return response.strip().lower() in {"y", "yes"}


def stream(message: str) -> None:
    logger.info("%s", message, extra={"ludos_stream": True})


def piter(*args: object, **kwargs: object) -> Any:
    if tqdm is None:
        return _NullProgress(*args, **kwargs)
    kwargs.setdefault("disable", not (console.is_terminal and not AGENT))
    kwargs.setdefault("file", console.file)
    if "desc" in kwargs:
        kwargs['desc'] = " " * 8 + kwargs["desc"] # type: ignore
    return tqdm(*args, **kwargs) # type: ignore


def pstream(message: str) -> None:
    if tqdm is None or not (console.is_terminal and not AGENT):
        stream(message)
        return
    tqdm.write(f"{' ' * INFO_MESSAGE_INDENT}| {message}")


def main() -> None:
    configure_logging()
    log(LOGO_STR)


if __name__ == "__main__":
    main()

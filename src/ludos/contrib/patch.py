from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..logging import log, stream
from ..model import Card, ConfigError, PatchRef, SpecBuild


DEFAULT_PATCHWORK_DIR = Path("patchwork")
PATCH_SHA = "patch-sha"
SHORT_SHA_LENGTH = 12
LUDOS_BRANCH = "ludos"


@dataclass(frozen=True)
class PatchSource:
    key: str
    source_dir: Path
    spec: SpecBuild
    patch: PatchRef


def patch_sources(card: Card) -> tuple[PatchSource, ...]:
    source = card_source(card)
    base = card_base_dir(source)
    sources: list[PatchSource] = []
    seen: set[str] = set()
    for spec in card.specs:
        if spec.patch is None:
            continue
        if spec.patch.type != "git":
            raise ConfigError(
                f"{source}: unsupported patch type "
                f"'{spec.patch.type}' for spec '{spec.spec}'"
            )
        spec_path = spec_source_path(source, base, spec)
        source_dir = spec_path.parent
        key = source_dir.relative_to(base).as_posix()
        if key == ".":
            key = base.name
        if key in seen:
            raise ConfigError(f"{source}: duplicate patch source '{key}'")
        seen.add(key)
        sources.append(
            PatchSource(
                key=key,
                source_dir=source_dir,
                spec=spec,
                patch=spec.patch,
            )
        )
    return tuple(sources)


def ensure_patchwork_repo(
    patchwork_base: Path,
    card_label: str,
    source: PatchSource,
) -> Path:
    repo_dir = (patchwork_base / source.key).resolve()
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    git_dir = repo_dir / ".git"
    if not repo_dir.exists():
        log(
            f"Cloning patchwork for '{card_label}:{source.key}' "
            f"into {display_path(repo_dir)}"
        )
        run(["git", "clone", "--origin", "upstream", source.patch.url, str(repo_dir)])
    elif not git_dir.exists():
        raise ConfigError(f"{repo_dir}: patchwork path exists but is not a git repository")
    else:
        log(f"Fetching patchwork for '{card_label}:{source.key}'")
        remotes = run_git(repo_dir, ["remote"], capture=True).stdout.splitlines()
        if "upstream" in remotes:
            run_git(repo_dir, ["remote", "set-url", "upstream", source.patch.url])
        else:
            run_git(repo_dir, ["remote", "add", "upstream", source.patch.url])
        run_git(repo_dir, ["fetch", "--prune", "--tags", "upstream"])
    return repo_dir


def render_patch_ref(source: PatchSource, card_source: Path) -> str:
    spec_path = source.source_dir / Path(source.spec.spec).name
    spec_values = _spec_values(spec_path)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = spec_values.get(key)
        if value is None:
            raise ConfigError(
                f"{card_source}: spec field '{key}' is not available for patch ref "
                f"'{source.patch.ref}'"
            )
        return value

    return re.sub(r"\$\{spec:([A-Za-z][A-Za-z0-9_]*)\}", replace, source.patch.ref)


def patch_file_path(
    card_source: Path,
    source: PatchSource,
    *,
    require_exists: bool = True,
) -> Path:
    patch_path = Path(source.patch.file)
    if patch_path.is_absolute() or ".." in patch_path.parts:
        raise ConfigError(
            f"{card_source}: patch file '{source.patch.file}' must stay inside the spec directory"
        )
    path = (source.source_dir / patch_path).resolve()
    try:
        path.relative_to(source.source_dir)
    except ValueError as exc:
        raise ConfigError(
            f"{card_source}: patch file '{source.patch.file}' escapes the spec directory"
        ) from exc
    if require_exists and not path.is_file():
        raise ConfigError(f"{card_source}: patch file '{source.patch.file}' is missing")
    return path


def guard_ludos_branch_clean(repo_dir: Path) -> None:
    if current_branch(repo_dir) != LUDOS_BRANCH:
        return
    if git_tree_clean(repo_dir):
        return
    raise ConfigError(
        f"{repo_dir}: refusing to replace dirty '{LUDOS_BRANCH}' patchwork branch. "
        "Resolve or clean it before continuing."
    )


def guard_worktree_clean(repo_dir: Path) -> None:
    if git_tree_clean(repo_dir):
        return
    raise ConfigError(
        f"{repo_dir}: refusing to replace a dirty patchwork checkout. "
        "Commit, stash, or clean it before continuing."
    )


def reset_patchwork(repo_dir: Path) -> None:
    run_git(repo_dir, ["am", "--abort"], check=False, capture=True)
    run_git(repo_dir, ["rebase", "--abort"], check=False, capture=True)
    reset_worktree(repo_dir)


def write_patch_series(
    repo_dir: Path,
    base_sha: str,
    patch_file: Path,
    *,
    revision: str = "HEAD",
) -> None:
    result = run_git(
        repo_dir,
        [
            "format-patch",
            "--stdout",
            "--zero-commit",
            "--no-renames",
            "-k",
            f"{base_sha}..{revision}",
        ],
        capture=True,
    )
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(
        strip_patch_series_format_signatures(result.stdout),
        encoding="utf-8",
    )


def apply_patch_series(repo_dir: Path, patch_file: Path) -> int:
    if not patch_file.read_text(encoding="utf-8").strip():
        return 0
    with tempfile.TemporaryDirectory(prefix="ludos-am-") as temp_dir:
        mail_dir = Path(temp_dir)
        run(["git", "mailsplit", f"-o{mail_dir}", str(patch_file)], capture=True)
        for mail in sorted(mail_dir.iterdir()):
            am_code, _am_output = run_git_streamed(
                repo_dir,
                ["am", "-k", "--empty=keep", str(mail)],
            )
            if am_code != 0:
                return am_code
            strip_empty_commit_format_patch_signature(repo_dir)
    return 0


def read_lock(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = load_mapping(path)
    return data


def write_lock(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False)
    path.write_text(text, encoding="utf-8")


def locked_sha(data: dict[str, Any], key: str, field: str) -> str:
    value = data.get(key)
    if not isinstance(value, dict):
        return ""
    sha = value.get(field, "")
    return sha if isinstance(sha, str) else ""


def set_locked_sha(data: dict[str, Any], key: str, field: str, sha: str) -> None:
    value = data.get(key)
    if not isinstance(value, dict):
        value = {}
        data[key] = value
    value[field] = sha


def spec_source_path(card_source: Path, card_base: Path, spec: SpecBuild) -> Path:
    spec_path = Path(spec.spec)
    if spec_path.is_absolute() or ".." in spec_path.parts:
        raise ConfigError(f"{card_source}: spec '{spec.spec}' escapes the card")
    source = (card_base / spec_path).resolve()
    try:
        source.relative_to(card_base)
    except ValueError as exc:
        raise ConfigError(f"{card_source}: spec '{spec.spec}' escapes the card") from exc
    if not source.is_file():
        raise ConfigError(f"{card_source}: spec '{spec.spec}' is missing")
    return source


def card_source(card: Card) -> Path:
    if card.source is None:
        raise ConfigError("card has no source path")
    return card.source.resolve()


def card_base_dir(source: Path) -> Path:
    return source.parent.resolve()


def lock_path(card_source: Path) -> Path:
    return card_source.with_name(f"{card_source.stem}.lock.yml")


def card_label(card_source: Path) -> str:
    source = card_source.resolve()
    try:
        relative = source.relative_to(Path.cwd())
    except ValueError:
        relative = source

    parts = list(relative.parts)
    if parts and parts[0] == "cards":
        parts = parts[1:]

    if parts and parts[-1] in ("card.yml", "card.yaml"):
        parts = parts[:-1]
    elif parts and Path(parts[-1]).suffix in (".yml", ".yaml"):
        parts[-1] = Path(parts[-1]).stem

    if not parts:
        return source.stem
    return "/".join(parts)


def patchwork_dir(patchwork_base: Path | None) -> Path:
    if patchwork_base is None:
        patchwork_base = DEFAULT_PATCHWORK_DIR
    return patchwork_base.expanduser().resolve()


def current_branch(repo_dir: Path) -> str:
    result = run_git(
        repo_dir,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def is_ancestor(repo_dir: Path, ancestor: str, descendant: str) -> bool:
    result = run_git(
        repo_dir,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def conflicted_paths(repo_dir: Path) -> tuple[str, ...]:
    conflicts = run_git(
        repo_dir,
        ["diff", "--name-only", "--diff-filter=U"],
        capture=True,
        check=False,
    )
    if conflicts.returncode != 0 or not conflicts.stdout.strip():
        return tuple()
    return tuple(conflicts.stdout.splitlines())


def git_tree_clean(repo_dir: Path) -> bool:
    status = run_git(repo_dir, ["status", "--porcelain"], capture=True).stdout
    return not status.strip()


def rev_parse(repo_dir: Path, rev: str) -> str:
    result = run_git(
        repo_dir,
        ["rev-parse", "--verify", f"{rev}^{{commit}}"],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        raise ConfigError(f"{repo_dir}: git revision not found: {rev}")
    return result.stdout.strip()


def short_sha(sha: str) -> str:
    return sha[:SHORT_SHA_LENGTH]


def display_path(path: Path) -> str:
    try:
        return f"./{path.resolve().relative_to(Path.cwd())}"
    except ValueError:
        return str(path)


def run_git(
    repo_dir: Path,
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", *args],
        cwd=repo_dir,
        check=check,
        capture=capture,
    )


def run_git_streamed(repo_dir: Path, args: list[str]) -> tuple[int, str]:
    return run_streamed(["git", *args], cwd=repo_dir)


def run_git_input(
    repo_dir: Path,
    args: list[str],
    input_text: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        input=input_text,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", *args],
            output=result.stdout,
            stderr=result.stderr,
        )
    if result.stderr:
        log(result.stderr.rstrip())
    return result


def run_streamed(command: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    output_lines: list[str] = []
    try:
        assert process.stdout is not None
        for line in process.stdout:
            output_lines.append(line)
            stream(line)
        return process.wait(), "".join(output_lines)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            command,
            output=result.stdout,
            stderr=result.stderr,
        )
    if not capture and result.stderr:
        log(result.stderr.rstrip())
    return result


def load_mapping(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: file does not exist") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a YAML mapping")
    return data


def reset_worktree(repo_dir: Path) -> None:
    run_git(repo_dir, ["merge", "--abort"], check=False, capture=True)
    run_git(repo_dir, ["reset", "--hard"], capture=True)
    run_git(repo_dir, ["clean", "-fdx"], capture=True)


def strip_empty_commit_format_patch_signature(repo_dir: Path) -> None:
    if not head_is_empty_commit(repo_dir):
        return
    result = run_git(repo_dir, ["log", "-1", "--format=%B"], capture=True)
    message = result.stdout
    stripped = strip_format_patch_signature(message)
    if stripped == message:
        return
    run_git_input(
        repo_dir,
        [
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--amend",
            "--allow-empty",
            "-F",
            "-",
        ],
        stripped,
    )


def head_is_empty_commit(repo_dir: Path) -> bool:
    result = run_git(
        repo_dir,
        ["diff", "--quiet", "HEAD^", "HEAD"],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def strip_format_patch_signature(message: str) -> str:
    lines = message.rstrip("\n").splitlines()
    if len(lines) < 2:
        return message
    if lines[-2].strip() != "--":
        return message
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", lines[-1].strip()):
        return message
    lines = lines[:-2]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def strip_patch_series_format_signatures(text: str) -> str:
    mails = split_patch_mbox(text)
    if not mails:
        return text
    stripped = [strip_format_patch_signature(mail).rstrip("\n") for mail in mails]
    return "\n\n".join(stripped) + "\n"


def split_patch_mbox(text: str) -> list[str]:
    mails: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if current and is_patch_mail_boundary(line):
            mails.append("".join(current))
            current = [line]
            continue
        current.append(line)
    if current:
        mails.append("".join(current))
    return mails


def is_patch_mail_boundary(line: str) -> bool:
    return bool(re.match(r"^From [0-9a-f]{40} Mon Sep 17 00:00:00 2001$", line.rstrip("\n")))


def _spec_values(spec_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):\s*(.*?)\s*$")
    for line in spec_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values

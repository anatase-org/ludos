from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .logging import log
from .model import Card, ConfigError, SpecBuild, UpstreamRef, validate_manifest


DEFAULT_CACHE_DIR = Path("cache")
DIST_GIT_CACHE = "dist-git"
UPSTREAM_SHA = "upstream-sha"
SHORT_SHA_LENGTH = 12


@dataclass(frozen=True)
class CardUpdateResult:
    initialized: int = 0
    skipped: int = 0
    updated: int = 0


@dataclass(frozen=True)
class UpstreamSource:
    key: str
    source_dir: Path
    spec: SpecBuild
    upstream: UpstreamRef


@dataclass(frozen=True)
class UpstreamHead:
    sha: str
    label: str


def update_targets(
    targets: tuple[Path, ...],
    cache_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    cache_dir = _cache_dir(cache_dir)
    cards = _target_cards(targets)
    totals = CardUpdateResult()
    for card in cards:
        result = update_card(card, cache_dir, dry_run=dry_run)
        totals = CardUpdateResult(
            initialized=totals.initialized + result.initialized,
            skipped=totals.skipped + result.skipped,
            updated=totals.updated + result.updated,
        )

    log(
        f"{'Dry run complete' if dry_run else 'Update complete'}: "
        f"{totals.updated} updated, {totals.initialized} initialized, "
        f"{totals.skipped} unchanged"
    )
    return 0


def update_card(
    card: Card,
    cache_dir: Path,
    *,
    dry_run: bool = False,
) -> CardUpdateResult:
    card_source = _card_source(card)
    sources = _upstream_sources(card)
    if not sources:
        log(f"No upstream-backed specs in {card_source}")
        return CardUpdateResult()

    log(f"Updating card: {_display_path(card_source)}")
    card_label = _card_label(card_source)
    lock_path = _lock_path(card_source)
    lock_data = _read_lock(lock_path)
    result = CardUpdateResult()
    for source in sources:
        old_sha = _locked_sha(lock_data, source.key)
        repo_dir = _ensure_dist_git_repo(
            cache_dir,
            card_label,
            source.key,
            source.upstream,
        )
        upstream_head = _resolve_upstream_head(repo_dir, source.upstream)

        if not old_sha:
            action = "Would initialize" if dry_run else "Initializing"
            log(f"{action} upstream lock for {source.key}: {upstream_head.sha}")
            if not dry_run:
                _set_locked_sha(lock_data, source.key, upstream_head.sha)
                _write_lock(lock_path, lock_data)
            result = CardUpdateResult(
                initialized=result.initialized + 1,
                skipped=result.skipped,
                updated=result.updated,
            )
            continue

        if old_sha == upstream_head.sha:
            log(f"Upstream unchanged for {source.key}: {upstream_head.sha}")
            result = CardUpdateResult(
                initialized=result.initialized,
                skipped=result.skipped + 1,
                updated=result.updated,
            )
            continue

        log(
            f"Merging {source.key} to {upstream_head.label} ({_short_sha(old_sha)}...{_short_sha(upstream_head.sha)}):\n"
            f"Commits:\n"
            f"{_commit_summary(repo_dir, old_sha, upstream_head.sha)}"
        )
        conflict_paths = _merge_dist_git_update(
            repo_dir=repo_dir,
            source=source,
            old_sha=old_sha,
            new_sha=upstream_head.sha,
        )
        if not dry_run:
            _copy_worktree_to_source(repo_dir, source.source_dir)
            _set_locked_sha(lock_data, source.key, upstream_head.sha)
            _write_lock(lock_path, lock_data)

        if conflict_paths:
            copied = (
                "no files were copied back"
                if dry_run
                else "conflicted files were copied back"
            )
            raise ConfigError(
                f"merge conflicts found for '{card_label}:{source.key}' ({copied}):\n"
                f"{_conflict_summary(repo_dir, source, conflict_paths, dry_run=dry_run)}"
            )

        log(f"Updated '{card_label}:{source.key}' to '{upstream_head.sha}'")
        result = CardUpdateResult(
            initialized=result.initialized,
            skipped=result.skipped,
            updated=result.updated + 1,
        )

    return result


def _target_cards(targets: tuple[Path, ...]) -> tuple[Card, ...]:
    cards: list[Card] = []
    seen: set[Path] = set()
    for target in targets:
        for card in _cards_for_target(target):
            source = _card_source(card).resolve()
            if source in seen:
                continue
            seen.add(source)
            cards.append(card)
    return tuple(cards)


def _cards_for_target(target: Path) -> tuple[Card, ...]:
    target = target.expanduser().resolve()
    if _is_manifest(target):
        validation = validate_manifest(target)
        if validation.missing_bootstrap:
            raise ConfigError(
                f"{target}: missing bootstrap card: {validation.missing_bootstrap}"
            )
        if validation.missing_repos:
            missing = ", ".join(validation.missing_repos)
            raise ConfigError(f"{target}: missing repository definitions: {missing}")
        if validation.missing_cards:
            missing = ", ".join(validation.missing_cards)
            raise ConfigError(f"{target}: missing card definitions: {missing}")

        cards: list[Card] = []
        if validation.bootstrap is not None:
            cards.append(validation.bootstrap)
        cards.extend(validation.cards)
        return tuple(cards)

    return (Card.from_file(target),)


def _is_manifest(path: Path) -> bool:
    data = _load_mapping(path)
    manifest_keys = {"releasever", "distro", "orchestrator", "bootstrap", "cards"}
    return manifest_keys.issubset(data)


def _upstream_sources(card: Card) -> tuple[UpstreamSource, ...]:
    card_source = _card_source(card)
    card_base = _card_base_dir(card_source)
    sources: list[UpstreamSource] = []
    seen: set[str] = set()
    for spec in card.specs:
        if spec.upstream is None:
            continue
        if spec.upstream.type != "dist-git":
            raise ConfigError(
                f"{card_source}: unsupported upstream type "
                f"'{spec.upstream.type}' for spec '{spec.spec}'"
            )
        spec_path = _spec_source_path(card_source, card_base, spec)
        source_dir = spec_path.parent
        key = source_dir.relative_to(card_base).as_posix()
        if key in seen:
            raise ConfigError(f"{card_source}: duplicate upstream source '{key}'")
        seen.add(key)
        sources.append(
            UpstreamSource(
                key=key,
                source_dir=source_dir,
                spec=spec,
                upstream=spec.upstream,
            )
        )
    return tuple(sources)


def _ensure_dist_git_repo(
    cache_dir: Path,
    card_label: str,
    key: str,
    upstream: UpstreamRef,
) -> Path:
    repo_dir = (cache_dir / DIST_GIT_CACHE / key).resolve()
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    git_dir = repo_dir / ".git"
    if not repo_dir.exists():
        log(
            f"Cloning upstream for '{card_label}:{key}' "
            f"into {_display_path(repo_dir)}"
        )
        _run(["git", "clone", "--origin", "upstream", upstream.url, str(repo_dir)])
    elif not git_dir.exists():
        raise ConfigError(f"{repo_dir}: cache path exists but is not a git repository")
    else:
        log(f"Fetching upstream for '{card_label}:{key}'")
        remotes = _run_git(repo_dir, ["remote"], capture=True).stdout.splitlines()
        if "upstream" in remotes:
            _run_git(repo_dir, ["remote", "set-url", "upstream", upstream.url])
        else:
            _run_git(repo_dir, ["remote", "add", "upstream", upstream.url])
        _run_git(repo_dir, ["fetch", "--prune", "--tags", "upstream"])
    return repo_dir


def _resolve_upstream_head(repo_dir: Path, upstream: UpstreamRef) -> UpstreamHead:
    if upstream.ref:
        return UpstreamHead(sha=_rev_parse(repo_dir, upstream.ref), label=upstream.ref)
    if upstream.branch:
        return UpstreamHead(
            sha=_rev_parse(repo_dir, f"refs/remotes/upstream/{upstream.branch}"),
            label=upstream.branch,
        )

    _run_git(repo_dir, ["remote", "set-head", "upstream", "-a"])
    symbolic = _run_git(
        repo_dir,
        ["symbolic-ref", "--quiet", "refs/remotes/upstream/HEAD"],
        capture=True,
    ).stdout.strip()
    if not symbolic:
        raise ConfigError(f"{repo_dir}: upstream default branch could not be resolved")
    return UpstreamHead(
        sha=_rev_parse(repo_dir, symbolic),
        label=_upstream_branch_label(symbolic),
    )


def _merge_dist_git_update(
    *,
    repo_dir: Path,
    source: UpstreamSource,
    old_sha: str,
    new_sha: str,
) -> tuple[str, ...]:
    _reset_worktree(repo_dir)
    _rev_parse(repo_dir, old_sha)
    _run_git(repo_dir, ["checkout", "-B", "ludos-update", old_sha], capture=True)
    _replace_worktree_contents(repo_dir, source.source_dir)
    _run_git(repo_dir, ["add", "-A"])
    if not _git_tree_clean(repo_dir):
        _run_git(
            repo_dir,
            [
                "-c",
                "user.name=Ludos",
                "-c",
                "user.email=ludos@localhost",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-m",
                f"Apply local {source.key} changes",
            ],
        )

    merge = _run_git(
        repo_dir,
        [
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--no-edit",
            new_sha,
        ],
        check=False,
        capture=True,
    )
    if merge.returncode == 0:
        return tuple()

    conflicts = _run_git(
        repo_dir,
        ["diff", "--name-only", "--diff-filter=U"],
        capture=True,
        check=False,
    )
    if conflicts.returncode == 0 and conflicts.stdout.strip():
        return tuple(conflicts.stdout.splitlines())
    raise ConfigError(
        f"{repo_dir}: git merge failed with exit status {merge.returncode}"
    )


def _conflict_summary(
    repo_dir: Path,
    source: UpstreamSource,
    conflict_paths: tuple[str, ...],
    *,
    dry_run: bool,
) -> str:
    base_dir = repo_dir if dry_run else source.source_dir
    return "\n".join(f" - {_display_path(base_dir / path)}" for path in conflict_paths)


def _reset_worktree(repo_dir: Path) -> None:
    _run_git(repo_dir, ["merge", "--abort"], check=False, capture=True)
    _run_git(repo_dir, ["reset", "--hard"], capture=True)
    _run_git(repo_dir, ["clean", "-fdx"], capture=True)


def _replace_worktree_contents(repo_dir: Path, source_dir: Path) -> None:
    for child in repo_dir.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    _copy_directory_contents(source_dir, repo_dir)


def _copy_worktree_to_source(repo_dir: Path, source_dir: Path) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    for child in source_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    _copy_directory_contents(repo_dir, source_dir, exclude={".git"})


def _copy_directory_contents(
    source_dir: Path, destination_dir: Path, exclude: set[str] | None = None
) -> None:
    exclude = exclude or set()
    for source in source_dir.iterdir():
        if source.name in exclude:
            continue
        destination = destination_dir / source.name
        if source.is_symlink():
            target = os.readlink(source)
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            os.symlink(target, destination)
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)


def _git_tree_clean(repo_dir: Path) -> bool:
    status = _run_git(repo_dir, ["status", "--porcelain"], capture=True).stdout
    return not status.strip()


def _rev_parse(repo_dir: Path, rev: str) -> str:
    result = _run_git(
        repo_dir,
        ["rev-parse", "--verify", f"{rev}^{{commit}}"],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        raise ConfigError(f"{repo_dir}: git revision not found: {rev}")
    return result.stdout.strip()


def _commit_summary(repo_dir: Path, old_sha: str, new_sha: str) -> str:
    result = _run_git(
        repo_dir,
        [
            "log",
            "--date=default",
            "--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n    %s%n",
            f"{old_sha}..{new_sha}",
        ],
        capture=True,
    )
    summary = "\n| ".join(result.stdout.strip().splitlines())
    if not summary:
        return "(none)"
    return "| " + summary + "\n| "


def _run_git(
    repo_dir: Path,
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["git", *args],
        cwd=repo_dir,
        check=check,
        capture=capture,
    )


def _run(
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


def _read_lock(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = _load_mapping(path)
    return data


def _write_lock(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=True)
    path.write_text(text, encoding="utf-8")


def _locked_sha(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, dict):
        return ""
    sha = value.get(UPSTREAM_SHA, "")
    return sha if isinstance(sha, str) else ""


def _set_locked_sha(data: dict[str, Any], key: str, sha: str) -> None:
    value = data.get(key)
    if not isinstance(value, dict):
        value = {}
        data[key] = value
    value[UPSTREAM_SHA] = sha


def _load_mapping(path: Path) -> dict[str, Any]:
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


def _spec_source_path(card_source: Path, card_base: Path, spec: SpecBuild) -> Path:
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


def _card_source(card: Card) -> Path:
    if card.source is None:
        raise ConfigError("card has no source path")
    return card.source.resolve()


def _card_base_dir(source: Path) -> Path:
    return source.parent.resolve()


def _lock_path(card_source: Path) -> Path:
    return card_source.with_name(f"{card_source.stem}.lock.yml")


def _card_label(card_source: Path) -> str:
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


def _cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR
    return cache_dir.expanduser().resolve()


def _short_sha(sha: str) -> str:
    return sha[:SHORT_SHA_LENGTH]


def _upstream_branch_label(ref: str) -> str:
    prefix = "refs/remotes/upstream/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return ref


def _display_path(path: Path) -> str:
    try:
        return f"./{path.resolve().relative_to(Path.cwd())}"
    except ValueError:
        return str(path)

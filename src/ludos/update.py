from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .logging import log, stream
from .model import Card, ConfigError, PatchRef, SpecBuild, UpstreamRef, validate_manifest


DEFAULT_CACHE_DIR = Path("cache")
DEFAULT_PATCHWORK_DIR = Path("patchwork")
DIST_GIT_CACHE = "dist-git"
UPSTREAM_SHA = "upstream-sha"
PATCH_SHA = "patch-sha"
SHORT_SHA_LENGTH = 12
LUDOS_BRANCH = "ludos"


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
class PatchSource:
    key: str
    source_dir: Path
    spec: SpecBuild
    patch: PatchRef


@dataclass(frozen=True)
class UpstreamHead:
    sha: str
    label: str


def update_targets(
    targets: tuple[Path, ...],
    cache_dir: Path | None = None,
    patchwork_dir: Path | None = None,
    dry_run: bool = False,
) -> int:
    cache_dir = _cache_dir(cache_dir)
    patchwork_dir = _patchwork_dir(patchwork_dir)
    cards = _target_cards(targets)
    totals = CardUpdateResult()
    for card in cards:
        result = update_card(
            card,
            cache_dir,
            patchwork_dir,
            dry_run=dry_run,
        )
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
    patchwork_dir: Path,
    *,
    dry_run: bool = False,
) -> CardUpdateResult:
    card_source = _card_source(card)
    sources = _upstream_sources(card)
    patch_sources = _patch_sources(card)
    if not sources and not patch_sources:
        return CardUpdateResult()

    log(f"Updating card: {_display_path(card_source)}")
    card_label = _card_label(card_source)
    lock_path = _lock_path(card_source)
    lock_data = _read_lock(lock_path)
    result = CardUpdateResult()
    for source in sources:
        old_sha = _locked_sha(lock_data, source.key, UPSTREAM_SHA)
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
                _set_locked_sha(lock_data, source.key, UPSTREAM_SHA, upstream_head.sha)
                _write_lock(lock_path, lock_data)
            result = CardUpdateResult(
                initialized=result.initialized + 1,
                skipped=result.skipped,
                updated=result.updated,
            )
            continue

        if old_sha == upstream_head.sha:
            log(f"No updates for '{upstream_head.label}' ('{_short_sha(upstream_head.sha)}')")
            result = CardUpdateResult(
                initialized=result.initialized,
                skipped=result.skipped + 1,
                updated=result.updated,
            )
            continue

        log(
            f"Merging '{source.key}' to '{upstream_head.label}' ({_short_sha(old_sha)}...{_short_sha(upstream_head.sha)}):\n"
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
            _copy_merged_source(repo_dir, source)
            _set_locked_sha(lock_data, source.key, UPSTREAM_SHA, upstream_head.sha)
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

        log(f"Updated '{card_label}:{source.key}' to '{_short_sha(upstream_head.sha)}'")
        result = CardUpdateResult(
            initialized=result.initialized,
            skipped=result.skipped,
            updated=result.updated + 1,
        )

    for source in patch_sources:
        patch_result = _update_patch_source(
            source=source,
            card_label=card_label,
            card_source=card_source,
            patchwork_dir=patchwork_dir,
            lock_path=lock_path,
            lock_data=lock_data,
            dry_run=dry_run,
        )
        result = CardUpdateResult(
            initialized=result.initialized + patch_result.initialized,
            skipped=result.skipped + patch_result.skipped,
            updated=result.updated + patch_result.updated,
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
        if key == ".":
            key = card_base.name
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


def _patch_sources(card: Card) -> tuple[PatchSource, ...]:
    card_source = _card_source(card)
    card_base = _card_base_dir(card_source)
    sources: list[PatchSource] = []
    seen: set[str] = set()
    for spec in card.specs:
        if spec.patch is None:
            continue
        if spec.patch.type != "git":
            raise ConfigError(
                f"{card_source}: unsupported patch type "
                f"'{spec.patch.type}' for spec '{spec.spec}'"
            )
        spec_path = _spec_source_path(card_source, card_base, spec)
        source_dir = spec_path.parent
        key = source_dir.relative_to(card_base).as_posix()
        if key == ".":
            key = card_base.name
        if key in seen:
            raise ConfigError(f"{card_source}: duplicate patch source '{key}'")
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
    _run_git(repo_dir, ["checkout", "-B", LUDOS_BRANCH, old_sha], capture=True)
    if source.spec.files:
        _overlay_spec_files(repo_dir, source)
    else:
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


def _update_patch_source(
    *,
    source: PatchSource,
    card_label: str,
    card_source: Path,
    patchwork_dir: Path,
    lock_path: Path,
    lock_data: dict[str, Any],
    dry_run: bool,
) -> CardUpdateResult:
    repo_dir = _ensure_patchwork_repo(patchwork_dir, card_label, source)
    ref = _render_patch_ref(source, card_source)
    new_sha = _rev_parse(repo_dir, ref)
    old_sha = _locked_sha(lock_data, source.key, PATCH_SHA)
    patch_file = _patch_file_path(card_source, source)

    if not old_sha:
        action = "Would initialize" if dry_run else "Initializing"
        log(f"{action} patch lock for {source.key}: {new_sha}")
        if not dry_run:
            _set_locked_sha(lock_data, source.key, PATCH_SHA, new_sha)
            _write_lock(lock_path, lock_data)
        return CardUpdateResult(initialized=1)

    if old_sha == new_sha:
        log(f"No patch updates for '{ref}' ('{_short_sha(new_sha)}')")
        return CardUpdateResult(skipped=1)

    if _finish_clean_patch_rebase(
        repo_dir=repo_dir,
        source=source,
        card_label=card_label,
        ref=ref,
        new_sha=new_sha,
        patch_file=patch_file,
        lock_path=lock_path,
        lock_data=lock_data,
        dry_run=dry_run,
    ):
        return CardUpdateResult(updated=1)

    _guard_ludos_branch_clean(repo_dir)
    _rev_parse(repo_dir, old_sha)
    log(
        f"Rebasing patch series for '{card_label}:{source.key}' "
        f"onto '{ref}' ({_short_sha(old_sha)}...{_short_sha(new_sha)})"
    )

    _reset_patchwork(repo_dir)
    _run_git(repo_dir, ["checkout", "-B", LUDOS_BRANCH, old_sha], capture=True)
    am_code = _apply_patch_series(repo_dir, patch_file)
    if am_code != 0:
        raise ConfigError(
            f"git am failed for '{card_label}:{source.key}' while applying in {_display_path(repo_dir)}"
        )

    rebase_code, _rebase_output = _run_git_streamed(
        repo_dir,
        ["rebase", "--onto", new_sha, old_sha],
    )
    if rebase_code != 0:
        conflicts = _conflicted_paths(repo_dir)
        conflict_text = (
            "\nConflicts:\n" + "\n".join(f" - {_display_path(repo_dir / path)}" for path in conflicts)
            if conflicts
            else ""
        )
        raise ConfigError(
            f"patch rebase failed for '{card_label}:{source.key}'. "
            f"Resolve it in {_display_path(repo_dir)} and run update again."
            f"{conflict_text}"
        )

    if not dry_run:
        _write_patch_series(repo_dir, new_sha, patch_file)
        _set_locked_sha(lock_data, source.key, PATCH_SHA, new_sha)
        _write_lock(lock_path, lock_data)

    log(f"Updated patch series for '{card_label}:{source.key}' to '{_short_sha(new_sha)}'")
    return CardUpdateResult(updated=1)


def _finish_clean_patch_rebase(
    *,
    repo_dir: Path,
    source: PatchSource,
    card_label: str,
    ref: str,
    new_sha: str,
    patch_file: Path,
    lock_path: Path,
    lock_data: dict[str, Any],
    dry_run: bool,
) -> bool:
    if _current_branch(repo_dir) != LUDOS_BRANCH:
        return False
    if not _git_tree_clean(repo_dir):
        raise ConfigError(
            f"{repo_dir}: patch rebase is still dirty. "
            "Resolve conflicts or clean the ludos branch before running update again."
        )
    if not _is_ancestor(repo_dir, new_sha, "HEAD"):
        return False

    action = "Would apply" if dry_run else "Applying"
    log(
        f"{action} clean rebase for '{card_label}:{source.key}' "
        f"onto '{ref}' ('{_short_sha(new_sha)}')"
    )
    if not dry_run:
        _write_patch_series(repo_dir, new_sha, patch_file)
        _set_locked_sha(lock_data, source.key, PATCH_SHA, new_sha)
        _write_lock(lock_path, lock_data)
    return True


def _ensure_patchwork_repo(
    patchwork_dir: Path,
    card_label: str,
    source: PatchSource,
) -> Path:
    repo_dir = (patchwork_dir / source.key).resolve()
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    git_dir = repo_dir / ".git"
    if not repo_dir.exists():
        log(
            f"Cloning patchwork for '{card_label}:{source.key}' "
            f"into {_display_path(repo_dir)}"
        )
        _run(["git", "clone", "--origin", "upstream", source.patch.url, str(repo_dir)])
    elif not git_dir.exists():
        raise ConfigError(f"{repo_dir}: patchwork path exists but is not a git repository")
    else:
        log(f"Fetching patchwork for '{card_label}:{source.key}'")
        remotes = _run_git(repo_dir, ["remote"], capture=True).stdout.splitlines()
        if "upstream" in remotes:
            _run_git(repo_dir, ["remote", "set-url", "upstream", source.patch.url])
        else:
            _run_git(repo_dir, ["remote", "add", "upstream", source.patch.url])
        _run_git(repo_dir, ["fetch", "--prune", "--tags", "upstream"])
    return repo_dir


def _render_patch_ref(source: PatchSource, card_source: Path) -> str:
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


def _spec_values(spec_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):\s*(.*?)\s*$")
    for line in spec_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _patch_file_path(card_source: Path, source: PatchSource) -> Path:
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
    if not path.is_file():
        raise ConfigError(f"{card_source}: patch file '{source.patch.file}' is missing")
    return path


def _guard_ludos_branch_clean(repo_dir: Path) -> None:
    if _current_branch(repo_dir) != LUDOS_BRANCH:
        return
    if _git_tree_clean(repo_dir):
        return
    raise ConfigError(
        f"{repo_dir}: refusing to replace dirty '{LUDOS_BRANCH}' patchwork branch. "
        "Resolve or clean it before running update again."
    )


def _reset_patchwork(repo_dir: Path) -> None:
    _run_git(repo_dir, ["am", "--abort"], check=False, capture=True)
    _run_git(repo_dir, ["rebase", "--abort"], check=False, capture=True)
    _reset_worktree(repo_dir)


def _write_patch_series(repo_dir: Path, base_sha: str, patch_file: Path) -> None:
    result = _run_git(
        repo_dir,
        [
            "format-patch",
            "--stdout",
            "--zero-commit",
            "--no-renames",
            "-k",
            base_sha,
        ],
        capture=True,
    )
    patch_file.write_text(
        _strip_patch_series_format_signatures(result.stdout),
        encoding="utf-8",
    )


def _apply_patch_series(repo_dir: Path, patch_file: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="ludos-am-") as temp_dir:
        mail_dir = Path(temp_dir)
        _run(["git", "mailsplit", f"-o{mail_dir}", str(patch_file)], capture=True)
        for mail in sorted(mail_dir.iterdir()):
            am_code, _am_output = _run_git_streamed(
                repo_dir,
                ["am", "-k", "--empty=keep", str(mail)],
            )
            if am_code != 0:
                return am_code
            _strip_empty_commit_format_patch_signature(repo_dir)
    return 0


def _strip_empty_commit_format_patch_signature(repo_dir: Path) -> None:
    if not _head_is_empty_commit(repo_dir):
        return
    result = _run_git(repo_dir, ["log", "-1", "--format=%B"], capture=True)
    message = result.stdout
    stripped = _strip_format_patch_signature(message)
    if stripped == message:
        return
    _run_git_input(
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


def _head_is_empty_commit(repo_dir: Path) -> bool:
    result = _run_git(
        repo_dir,
        ["diff", "--quiet", "HEAD^", "HEAD"],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def _strip_format_patch_signature(message: str) -> str:
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


def _strip_patch_series_format_signatures(text: str) -> str:
    mails = _split_patch_mbox(text)
    if not mails:
        return text
    stripped = [_strip_format_patch_signature(mail).rstrip("\n") for mail in mails]
    return "\n\n".join(stripped) + "\n"


def _split_patch_mbox(text: str) -> list[str]:
    mails: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if current and _is_patch_mail_boundary(line):
            mails.append("".join(current))
            current = [line]
            continue
        current.append(line)
    if current:
        mails.append("".join(current))
    return mails


def _is_patch_mail_boundary(line: str) -> bool:
    return bool(re.match(r"^From [0-9a-f]{40} Mon Sep 17 00:00:00 2001$", line.rstrip("\n")))


def _current_branch(repo_dir: Path) -> str:
    result = _run_git(
        repo_dir,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_ancestor(repo_dir: Path, ancestor: str, descendant: str) -> bool:
    result = _run_git(
        repo_dir,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def _conflicted_paths(repo_dir: Path) -> tuple[str, ...]:
    conflicts = _run_git(
        repo_dir,
        ["diff", "--name-only", "--diff-filter=U"],
        capture=True,
        check=False,
    )
    if conflicts.returncode != 0 or not conflicts.stdout.strip():
        return tuple()
    return tuple(conflicts.stdout.splitlines())


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


def _copy_merged_source(repo_dir: Path, source: UpstreamSource) -> None:
    if source.spec.files:
        _copy_merged_spec_files(repo_dir, source)
    else:
        _copy_worktree_to_source(repo_dir, source.source_dir)


def _overlay_spec_files(repo_dir: Path, source: UpstreamSource) -> None:
    spec_name = Path(source.spec.spec).name
    shutil.copy2(source.source_dir / spec_name, repo_dir / spec_name)
    for pattern in source.spec.files:
        matches = sorted(source.source_dir.glob(pattern))
        if not matches:
            continue
        _remove_matches(repo_dir, pattern)
        for path in matches:
            _copy_relative_path(path, source.source_dir, repo_dir)


def _copy_merged_spec_files(repo_dir: Path, source: UpstreamSource) -> None:
    spec_name = Path(source.spec.spec).name
    shutil.copy2(repo_dir / spec_name, source.source_dir / spec_name)
    for pattern in source.spec.files:
        _remove_matches(source.source_dir, pattern)
        for path in sorted(repo_dir.glob(pattern)):
            _copy_relative_path(path, repo_dir, source.source_dir)


def _remove_matches(base_dir: Path, pattern: str) -> None:
    for path in sorted(base_dir.glob(pattern), reverse=True):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


def _copy_relative_path(
    source_path: Path,
    source_base: Path,
    destination_base: Path,
) -> None:
    relative = source_path.relative_to(source_base)
    destination = destination_base / relative
    if source_path.is_dir() and not source_path.is_symlink():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source_path, destination, symlinks=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source_path.is_symlink():
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        os.symlink(os.readlink(source_path), destination)
        return
    shutil.copy2(source_path, destination)


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


def _run_git_streamed(repo_dir: Path, args: list[str]) -> tuple[int, str]:
    return _run_streamed(["git", *args], cwd=repo_dir)


def _run_git_input(
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


def _run_streamed(command: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
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
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)


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
    text = yaml.safe_dump(data, sort_keys=False)
    path.write_text(text, encoding="utf-8")


def _locked_sha(data: dict[str, Any], key: str, field: str) -> str:
    value = data.get(key)
    if not isinstance(value, dict):
        return ""
    sha = value.get(field, "")
    return sha if isinstance(sha, str) else ""


def _set_locked_sha(data: dict[str, Any], key: str, field: str, sha: str) -> None:
    value = data.get(key)
    if not isinstance(value, dict):
        value = {}
        data[key] = value
    value[field] = sha


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


def _patchwork_dir(patchwork_dir: Path | None) -> Path:
    if patchwork_dir is None:
        patchwork_dir = DEFAULT_PATCHWORK_DIR
    return patchwork_dir.expanduser().resolve()


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

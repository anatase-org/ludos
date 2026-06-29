from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from ..logging import confirm, log, stream
from ..model import (
    Card,
    ConfigError,
    SpecBuild,
    UpstreamRef,
    _resolve_card_path,
    validate_manifest,
)
from .patch import (
    LUDOS_BRANCH,
    PATCH_SHA,
    PatchSource,
    apply_patch_series,
    card_base_dir,
    card_label,
    card_source,
    conflicted_paths,
    current_branch,
    display_path,
    ensure_patchwork_repo,
    git_tree_clean,
    guard_ludos_branch_clean,
    is_ancestor,
    lock_path,
    locked_sha,
    patch_file_path,
    patch_sources,
    patchwork_dir as resolve_patchwork_dir,
    read_lock,
    render_patch_ref,
    reset_patchwork,
    rev_parse,
    set_locked_sha,
    spec_source_path,
    write_lock,
    write_patch_series,
)


DEFAULT_CACHE_DIR = Path("cache")
DIST_GIT_CACHE = "dist-git"
UPSTREAM_SHA = "upstream-sha"
SHORT_SHA_LENGTH = 12


@dataclass(frozen=True)
class CardUpdateResult:
    initialized: int = 0
    skipped: int = 0
    updated: int = 0
    declined: int = 0


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


@dataclass(frozen=True)
class CardUpdateTarget:
    card: Card
    env: dict[str, str]


def _confirm_update(label: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return confirm(f"Update {label}")


def update_targets(
    targets: tuple[Path, ...],
    cache_dir: Path | None = None,
    patchwork_dir: Path | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    card: str | None = None,
) -> int:
    cache_dir = _cache_dir(cache_dir)
    patchwork_dir = _patchwork_dir(patchwork_dir)
    cards = _target_cards(targets, card=card)
    totals = CardUpdateResult()
    for target in cards:
        result = update_card(
            target.card,
            cache_dir,
            patchwork_dir,
            dry_run=dry_run,
            assume_yes=assume_yes,
            env=target.env,
        )
        totals = CardUpdateResult(
            initialized=totals.initialized + result.initialized,
            skipped=totals.skipped + result.skipped,
            updated=totals.updated + result.updated,
            declined=totals.declined + result.declined,
        )

    summary = (
        f"{'Dry run complete' if dry_run else 'Update complete'}: "
        f"{totals.updated} updated, {totals.initialized} initialized, "
        f"{totals.skipped} unchanged"
    )
    if totals.declined:
        summary += f", {totals.declined} declined"
    log(summary)
    return 0


def update_card(
    card: Card,
    cache_dir: Path,
    patchwork_dir: Path,
    *,
    dry_run: bool = False,
    assume_yes: bool = False,
    env: dict[str, str] | None = None,
) -> CardUpdateResult:
    card_source = _card_source(card)
    sources = _upstream_sources(card, env=env)
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
            action = (
                "Would initialize"
                if dry_run
                else "Upstream lock needs initialization"
            )
            log(f"{action} upstream lock for {source.key}: {upstream_head.sha}")
            if not dry_run and not _confirm_update(
                f"{card_label}:{source.key}",
                assume_yes=assume_yes,
            ):
                result = CardUpdateResult(
                    initialized=result.initialized,
                    skipped=result.skipped,
                    updated=result.updated,
                    declined=result.declined + 1,
                )
                continue
            if not dry_run:
                _set_locked_sha(lock_data, source.key, UPSTREAM_SHA, upstream_head.sha)
                _write_lock(lock_path, lock_data)
            result = CardUpdateResult(
                initialized=result.initialized + 1,
                skipped=result.skipped,
                updated=result.updated,
                declined=result.declined,
            )
            continue

        if old_sha == upstream_head.sha:
            log(f"No updates for '{upstream_head.label}' ('{_short_sha(upstream_head.sha)}')")
            result = CardUpdateResult(
                initialized=result.initialized,
                skipped=result.skipped + 1,
                updated=result.updated,
                declined=result.declined,
            )
            continue

        action = "Would merge" if dry_run else "Update available"
        log(
            f"{action} for '{source.key}' to '{upstream_head.label}' "
            f"({_short_sha(old_sha)}...{_short_sha(upstream_head.sha)}):\n"
            f"Commits:\n"
            f"{_commit_summary(repo_dir, old_sha, upstream_head.sha)}"
        )
        if not dry_run and not _confirm_update(
            f"{card_label}:{source.key}",
            assume_yes=assume_yes,
        ):
            result = CardUpdateResult(
                initialized=result.initialized,
                skipped=result.skipped,
                updated=result.updated,
                declined=result.declined + 1,
            )
            continue
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
            declined=result.declined,
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
            assume_yes=assume_yes,
        )
        result = CardUpdateResult(
            initialized=result.initialized + patch_result.initialized,
            skipped=result.skipped + patch_result.skipped,
            updated=result.updated + patch_result.updated,
            declined=result.declined + patch_result.declined,
        )

    return result


def _target_cards(
    targets: tuple[Path, ...],
    *,
    card: str | None = None,
) -> tuple[CardUpdateTarget, ...]:
    if card is not None and len(targets) != 1:
        raise ConfigError("targeted card updates require exactly one manifest")

    cards: list[CardUpdateTarget] = []
    seen: set[Path] = set()
    for target in targets:
        for update_target in _cards_for_target(target, card=card):
            source = _card_source(update_target.card).resolve()
            if source in seen:
                continue
            seen.add(source)
            cards.append(update_target)
    return tuple(cards)


def _cards_for_target(
    target: Path,
    *,
    card: str | None = None,
) -> tuple[CardUpdateTarget, ...]:
    target = target.expanduser().resolve()
    if _is_manifest(target):
        return _manifest_update_targets(target, card=card)

    if card is not None:
        raise ConfigError("--card requires a manifest target")

    return (CardUpdateTarget(card=Card.from_file(target), env={}),)


def _manifest_update_targets(
    manifest_path: Path,
    *,
    card: str | None,
) -> tuple[CardUpdateTarget, ...]:
    validation = validate_manifest(manifest_path)
    if validation.missing_bootstrap:
        raise ConfigError(
            f"{manifest_path}: missing bootstrap card: {validation.missing_bootstrap}"
        )
    if validation.missing_repos:
        missing = ", ".join(validation.missing_repos)
        raise ConfigError(f"{manifest_path}: missing repository definitions: {missing}")
    if validation.missing_cards:
        missing = ", ".join(validation.missing_cards)
        raise ConfigError(f"{manifest_path}: missing card definitions: {missing}")

    root_dir = manifest_path.parent
    manifest_env = _manifest_env(
        manifest_path,
        validation.manifest.env,
        validation.manifest.releasever,
        validation.manifest.distro,
    )
    selected_source = (
        _resolve_card_path(card, root_dir, None).resolve()
        if card is not None
        else None
    )

    updates: list[CardUpdateTarget] = []
    if validation.bootstrap is not None and selected_source is None:
        updates.append(
            CardUpdateTarget(
                card=validation.bootstrap,
                env=_card_env(manifest_env, validation.bootstrap.env),
            )
        )

    inherited_env = dict(manifest_env)
    card_entries = sorted(
        enumerate(validation.cards),
        key=lambda entry: (entry[1].priority, entry[0]),
    )
    for _insertion_order, update_card in card_entries:
        card_env = _card_env(inherited_env, update_card.env)
        inherited_env.update(card_env)
        if selected_source is not None:
            if (
                update_card.source is None
                or update_card.source.resolve() != selected_source
            ):
                continue
        updates.append(CardUpdateTarget(card=update_card, env=card_env))

    if selected_source is not None and not updates:
        raise ConfigError(f"{manifest_path}: card not listed in manifest: {card}")

    return tuple(updates)


def _manifest_env(
    manifest_path: Path,
    manifest_values: dict[str, str | int],
    releasever_value: str,
    distro_value: str,
) -> dict[str, str]:
    root_dir = manifest_path.parent
    env = {key: str(value) for key, value in manifest_values.items()}
    env.update(_load_dotenv(root_dir / ".env"))
    releasever = _cache_name(
        _substitute_variables(releasever_value, env),
        "releasever",
    )
    env["releasever"] = releasever
    env["arch"] = _cache_name(
        _substitute_variables(str(env.get("arch", "")), env),
        "arch",
    )
    env["distro"] = _cache_name(
        _substitute_variables(distro_value, env),
        "distro",
    )
    return env


def _card_env(
    manifest_env: dict[str, str],
    card_values: dict[str, str | int],
) -> dict[str, str]:
    values = dict(manifest_env)
    for key, value in card_values.items():
        expression = str(value)
        if expression == f"${key}" and key in values:
            continue
        values[key] = _substitute_variables(expression, values)
    keys = ("arch", "releasever", *card_values)
    return {key: values[key] for key in keys if key in values}


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{path}:{line_number}: invalid environment key '{key}'")
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _cache_name(value: str, description: str) -> str:
    if "/" in value or value in ("", ".", ".."):
        raise ConfigError(f"invalid {description} cache name '{value}'")
    return value


def _substitute_variables(value: str, variables: dict[str, str]) -> str:
    for key, replacement in variables.items():
        value = value.replace(f"${key}", replacement)
    return value


def _is_manifest(path: Path) -> bool:
    data = _load_mapping(path)
    manifest_keys = {"releasever", "distro", "orchestrator", "bootstrap", "cards"}
    return manifest_keys.issubset(data)


def _upstream_sources(
    card: Card,
    *,
    env: dict[str, str] | None = None,
) -> tuple[UpstreamSource, ...]:
    env = env or {}
    card_source = _card_source(card)
    card_base = _card_base_dir(card_source)
    candidates: list[tuple[SpecBuild, Path, Path, str]] = []
    base_key_counts: dict[str, int] = {}
    seen: set[str] = set()
    for spec in card.specs:
        if spec.upstream is None:
            continue
        upstream = _expand_upstream_ref(spec.upstream, env)
        if upstream.type != "dist-git":
            raise ConfigError(
                f"{card_source}: unsupported upstream type "
                f"'{upstream.type}' for spec '{spec.spec}'"
            )
        spec_path = _spec_source_path(card_source, card_base, spec)
        source_dir = spec_path.parent
        base_key = _upstream_source_base_key(card_base, source_dir)
        base_key_counts[base_key] = base_key_counts.get(base_key, 0) + 1
        expanded_spec = replace(spec, upstream=upstream)
        candidates.append((expanded_spec, spec_path, source_dir, base_key))

    sources: list[UpstreamSource] = []
    for spec, spec_path, source_dir, base_key in candidates:
        key = (
            _upstream_source_spec_key(card_base, spec_path)
            if base_key_counts[base_key] > 1
            else base_key
        )
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


def _expand_upstream_ref(upstream: UpstreamRef, env: dict[str, str]) -> UpstreamRef:
    return replace(
        upstream,
        url=_substitute_variables(upstream.url, env),
        branch=_substitute_variables(upstream.branch, env),
        ref=_substitute_variables(upstream.ref, env),
        subdir=_substitute_variables(upstream.subdir, env),
    )


def _upstream_source_base_key(card_base: Path, source_dir: Path) -> str:
    key = source_dir.relative_to(card_base).as_posix()
    if key == ".":
        return card_base.name
    return key


def _upstream_source_spec_key(card_base: Path, spec_path: Path) -> str:
    return spec_path.relative_to(card_base).with_suffix("").as_posix()


def _patch_sources(card: Card) -> tuple[PatchSource, ...]:
    return patch_sources(card)


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
    _rev_parse(repo_dir, new_sha)
    _run_git(repo_dir, ["checkout", "--detach", old_sha], capture=True)
    upstream_dir = _upstream_worktree_dir(repo_dir, source.upstream)
    if source.spec.files:
        _overlay_spec_files(upstream_dir, source)
    else:
        _replace_worktree_contents(upstream_dir, source.source_dir)
    _run_git(repo_dir, ["add", "-A"])
    local_sha = old_sha
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
        local_sha = _rev_parse(repo_dir, "HEAD")

    _reset_worktree(repo_dir)
    _run_git(repo_dir, ["checkout", "-B", LUDOS_BRANCH, new_sha], capture=True)
    if local_sha == old_sha:
        return tuple()

    merge = _run_git(
        repo_dir,
        [
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "merge",
            "--no-edit",
            local_sha,
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
        return _source_relative_conflict_paths(
            repo_dir,
            source,
            conflicts.stdout.splitlines(),
        )
    raise ConfigError(
        f"{repo_dir}: git merge failed with exit status {merge.returncode}"
    )


def _upstream_worktree_dir(repo_dir: Path, upstream: UpstreamRef) -> Path:
    if not upstream.subdir:
        return repo_dir
    subdir = Path(upstream.subdir)
    worktree_dir = (repo_dir / subdir).resolve()
    try:
        worktree_dir.relative_to(repo_dir.resolve())
    except ValueError as exc:
        raise ConfigError(f"{repo_dir}: upstream subdir escapes repository") from exc
    if not worktree_dir.is_dir():
        raise ConfigError(
            f"{repo_dir}: upstream subdir does not exist: {upstream.subdir}"
        )
    return worktree_dir


def _source_relative_conflict_paths(
    repo_dir: Path,
    source: UpstreamSource,
    conflict_paths: list[str],
) -> tuple[str, ...]:
    if not source.upstream.subdir:
        return tuple(conflict_paths)

    subdir = Path(source.upstream.subdir)
    relative_paths = []
    for conflict_path in conflict_paths:
        path = Path(conflict_path)
        try:
            relative_paths.append(path.relative_to(subdir).as_posix())
        except ValueError as exc:
            raise ConfigError(
                f"{repo_dir}: merge conflict outside upstream subdir "
                f"'{source.upstream.subdir}': {conflict_path}"
            ) from exc
    return tuple(relative_paths)


def _update_patch_source(
    *,
    source: PatchSource,
    card_label: str,
    card_source: Path,
    patchwork_dir: Path,
    lock_path: Path,
    lock_data: dict[str, Any],
    dry_run: bool,
    assume_yes: bool,
) -> CardUpdateResult:
    repo_dir = ensure_patchwork_repo(patchwork_dir, card_label, source)
    ref = render_patch_ref(source, card_source)
    new_sha = rev_parse(repo_dir, ref)
    old_sha = _locked_sha(lock_data, source.key, PATCH_SHA)
    patch_file = patch_file_path(card_source, source)

    if not old_sha:
        action = "Would initialize" if dry_run else "Patch lock needs initialization"
        log(f"{action} patch lock for {source.key}: {new_sha}")
        if not dry_run and not _confirm_update(
            f"{card_label}:{source.key}",
            assume_yes=assume_yes,
        ):
            return CardUpdateResult(declined=1)
        if not dry_run:
            _set_locked_sha(lock_data, source.key, PATCH_SHA, new_sha)
            _write_lock(lock_path, lock_data)
        return CardUpdateResult(initialized=1)

    if old_sha == new_sha:
        log(f"No patch updates for '{ref}' ('{_short_sha(new_sha)}')")
        return CardUpdateResult(skipped=1)

    if not dry_run:
        log(
            f"Patch update available for '{card_label}:{source.key}' "
            f"onto '{ref}' ({_short_sha(old_sha)}...{_short_sha(new_sha)})"
        )
    if not dry_run and not _confirm_update(
        f"{card_label}:{source.key}",
        assume_yes=assume_yes,
    ):
        return CardUpdateResult(declined=1)

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

    guard_ludos_branch_clean(repo_dir)
    rev_parse(repo_dir, old_sha)
    log(
        f"Rebasing patch series for '{card_label}:{source.key}' "
        f"onto '{ref}' ({_short_sha(old_sha)}...{_short_sha(new_sha)})"
    )
    reset_patchwork(repo_dir)
    _run_git(repo_dir, ["checkout", "-B", LUDOS_BRANCH, old_sha], capture=True)
    am_code = apply_patch_series(repo_dir, patch_file)
    if am_code != 0:
        raise ConfigError(
            f"git am failed for '{card_label}:{source.key}' while applying in {_display_path(repo_dir)}"
        )

    rebase_code, _rebase_output = _run_git_streamed(
        repo_dir,
        ["rebase", "--onto", new_sha, old_sha],
    )
    if rebase_code != 0:
        conflicts = conflicted_paths(repo_dir)
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
        write_patch_series(repo_dir, new_sha, patch_file)
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
    if current_branch(repo_dir) != LUDOS_BRANCH:
        return False
    if not git_tree_clean(repo_dir):
        raise ConfigError(
            f"{repo_dir}: patch rebase is still dirty. "
            "Resolve conflicts or clean the ludos branch before running update again."
        )
    if not is_ancestor(repo_dir, new_sha, "HEAD"):
        return False

    action = "Would apply" if dry_run else "Applying"
    log(
        f"{action} clean rebase for '{card_label}:{source.key}' "
        f"onto '{ref}' ('{_short_sha(new_sha)}')"
    )
    if not dry_run:
        write_patch_series(repo_dir, new_sha, patch_file)
        _set_locked_sha(lock_data, source.key, PATCH_SHA, new_sha)
        _write_lock(lock_path, lock_data)
    return True


def _conflict_summary(
    repo_dir: Path,
    source: UpstreamSource,
    conflict_paths: tuple[str, ...],
    *,
    dry_run: bool,
) -> str:
    base_dir = (
        _upstream_worktree_dir(repo_dir, source.upstream)
        if dry_run
        else source.source_dir
    )
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
    upstream_dir = _upstream_worktree_dir(repo_dir, source.upstream)
    if source.spec.files:
        _copy_merged_spec_files(upstream_dir, source)
    else:
        _copy_worktree_to_source(upstream_dir, source.source_dir)


def _overlay_spec_files(upstream_dir: Path, source: UpstreamSource) -> None:
    spec_name = Path(source.spec.spec).name
    shutil.copy2(source.source_dir / spec_name, upstream_dir / spec_name)
    for pattern in source.spec.files:
        matches = sorted(source.source_dir.glob(pattern))
        if not matches:
            continue
        _remove_matches(upstream_dir, pattern)
        for path in matches:
            _copy_relative_path(path, source.source_dir, upstream_dir)


def _copy_merged_spec_files(upstream_dir: Path, source: UpstreamSource) -> None:
    spec_name = Path(source.spec.spec).name
    shutil.copy2(upstream_dir / spec_name, source.source_dir / spec_name)
    for pattern in source.spec.files:
        _remove_matches(source.source_dir, pattern)
        for path in sorted(upstream_dir.glob(pattern)):
            _copy_relative_path(path, upstream_dir, source.source_dir)


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
    return read_lock(path)


def _write_lock(path: Path, data: dict[str, Any]) -> None:
    write_lock(path, data)


def _locked_sha(data: dict[str, Any], key: str, field: str) -> str:
    return locked_sha(data, key, field)


def _set_locked_sha(data: dict[str, Any], key: str, field: str, sha: str) -> None:
    set_locked_sha(data, key, field, sha)


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
    return spec_source_path(card_source, card_base, spec)


def _card_source(card: Card) -> Path:
    return card_source(card)


def _card_base_dir(source: Path) -> Path:
    return card_base_dir(source)


def _lock_path(card_source: Path) -> Path:
    return lock_path(card_source)


def _card_label(card_source: Path) -> str:
    return card_label(card_source)


def _cache_dir(cache_dir: Path | None) -> Path:
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR
    return cache_dir.expanduser().resolve()


def _patchwork_dir(patchwork_dir: Path | None) -> Path:
    return resolve_patchwork_dir(patchwork_dir)


def _short_sha(sha: str) -> str:
    return sha[:SHORT_SHA_LENGTH]


def _upstream_branch_label(ref: str) -> str:
    prefix = "refs/remotes/upstream/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return ref


def _display_path(path: Path) -> str:
    return display_path(path)

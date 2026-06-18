from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..model import Card, ConfigError, PatchRef, SpecBuild
from ..model import _resolve_card_path as _resolve_model_card_path
from . import patch as patch_helpers
from ..logging import log


DEFAULT_PATCH_FILE = "overrides.patch"
DEFAULT_PATCH_REF = "${spec:Version}"


class IndentedSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


@dataclass(frozen=True)
class PatchTarget:
    card: Card
    card_source: Path
    card_label: str
    source: patch_helpers.PatchSource
    patch_sha: str


def patch_target(
    action: str,
    target: str,
    *,
    patchwork_dir: Path | None = None,
    url: str = "",
    file: str = DEFAULT_PATCH_FILE,
    ref: str = DEFAULT_PATCH_REF,
    name: str = "",
) -> int:
    if action == "checkout":
        return checkout_patch(target, patchwork_dir=patchwork_dir)
    if action == "apply":
        return apply_patch(target, patchwork_dir=patchwork_dir)
    if action == "init":
        return init_patch(
            target,
            url,
            patchwork_dir=patchwork_dir,
            file=file,
            ref=ref,
            name=name,
        )
    raise ConfigError(f"unsupported patch action: {action}")


def init_patch(
    target: str,
    url: str,
    *,
    patchwork_dir: Path | None = None,
    file: str = DEFAULT_PATCH_FILE,
    ref: str = DEFAULT_PATCH_REF,
    name: str = "",
) -> int:
    if not url.strip():
        raise ConfigError("patch init requires a git URL")

    patchwork_base = patch_helpers.patchwork_dir(patchwork_dir)
    resolved = _resolve_init_target(target, url, file, ref, name)
    repo_dir = patch_helpers.ensure_patchwork_repo(
        patchwork_base,
        resolved.card_label,
        resolved.source,
    )
    rendered_ref = patch_helpers.render_patch_ref(resolved.source, resolved.card_source)
    patch_sha = patch_helpers.rev_parse(repo_dir, rendered_ref)
    patch_helpers.guard_ludos_branch_clean(repo_dir)
    patch_helpers.guard_worktree_clean(repo_dir)
    patch_helpers.reset_patchwork(repo_dir)
    patch_helpers.run_git(
        repo_dir,
        ["checkout", "-B", patch_helpers.LUDOS_BRANCH, patch_sha],
        capture=True,
    )

    patch_file = patch_helpers.patch_file_path(
        resolved.card_source,
        resolved.source,
        require_exists=False,
    )
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.touch(exist_ok=True)

    _write_card_data(resolved.card_source, resolved.card_data)
    lock_path = patch_helpers.lock_path(resolved.card_source)
    lock_data = patch_helpers.read_lock(lock_path)
    patch_helpers.set_locked_sha(
        lock_data,
        resolved.source.key,
        patch_helpers.PATCH_SHA,
        patch_sha,
    )
    patch_helpers.write_lock(lock_path, lock_data)

    log(
        f"Initialized patchwork for '{resolved.card_label}:{resolved.source.key}' "
        f"at '{patch_helpers.short_sha(patch_sha)}'"
    )
    return 0


def checkout_patch(target: str, *, patchwork_dir: Path | None = None) -> int:
    patchwork_base = patch_helpers.patchwork_dir(patchwork_dir)
    resolved = _resolve_target(target)
    repo_dir = patch_helpers.ensure_patchwork_repo(
        patchwork_base,
        resolved.card_label,
        resolved.source,
    )
    patch_file = patch_helpers.patch_file_path(resolved.card_source, resolved.source)
    patch_helpers.rev_parse(repo_dir, resolved.patch_sha)
    patch_helpers.guard_ludos_branch_clean(repo_dir)
    patch_helpers.guard_worktree_clean(repo_dir)
    patch_helpers.reset_patchwork(repo_dir)
    patch_helpers.run_git(
        repo_dir,
        ["checkout", "-B", patch_helpers.LUDOS_BRANCH, resolved.patch_sha],
        capture=True,
    )
    am_code = patch_helpers.apply_patch_series(repo_dir, patch_file)
    if am_code != 0:
        conflicts = patch_helpers.conflicted_paths(repo_dir)
        conflict_text = (
            "\nConflicts:\n"
            + "\n".join(
                f" - {patch_helpers.display_path(repo_dir / path)}" for path in conflicts
            )
            if conflicts
            else ""
        )
        raise ConfigError(
            f"git am failed for '{resolved.card_label}:{resolved.source.key}' "
            f"while applying in {patch_helpers.display_path(repo_dir)}"
            f"{conflict_text}"
        )

    log(
        f"Checked out '{resolved.card_label}:{resolved.source.key}' on "
        f"'{patch_helpers.LUDOS_BRANCH}' from "
        f"'{patch_helpers.short_sha(resolved.patch_sha)}' "
        f"on {patch_helpers.display_path(repo_dir)}"
    )
    return 0


def apply_patch(target: str, *, patchwork_dir: Path | None = None) -> int:
    patchwork_base = patch_helpers.patchwork_dir(patchwork_dir)
    resolved = _resolve_target(target)
    repo_dir = patch_helpers.ensure_patchwork_repo(
        patchwork_base,
        resolved.card_label,
        resolved.source,
    )
    branch = f"refs/heads/{patch_helpers.LUDOS_BRANCH}"
    try:
        patch_helpers.rev_parse(repo_dir, branch)
    except ConfigError as exc:
        raise ConfigError(
            f"{repo_dir}: missing '{patch_helpers.LUDOS_BRANCH}' patchwork branch "
            f"for '{resolved.card_label}:{resolved.source.key}'"
        ) from exc
    patch_helpers.rev_parse(repo_dir, resolved.patch_sha)
    if not patch_helpers.is_ancestor(repo_dir, resolved.patch_sha, branch):
        raise ConfigError(
            f"{repo_dir}: '{patch_helpers.LUDOS_BRANCH}' is not based on "
            f"patch-sha '{patch_helpers.short_sha(resolved.patch_sha)}'"
        )
    if (
        patch_helpers.current_branch(repo_dir) == patch_helpers.LUDOS_BRANCH
        and not patch_helpers.git_tree_clean(repo_dir)
    ):
        raise ConfigError(
            f"{repo_dir}: '{patch_helpers.LUDOS_BRANCH}' has uncommitted changes. "
            "Commit or clean them before updating the patch file."
        )

    patch_file = patch_helpers.patch_file_path(
        resolved.card_source,
        resolved.source,
        require_exists=False,
    )
    patch_helpers.write_patch_series(
        repo_dir,
        resolved.patch_sha,
        patch_file,
        revision=branch,
    )
    log(
        f"Updated {patch_helpers.display_path(patch_file)} from "
        f"'{resolved.card_label}:{resolved.source.key}'"
    )
    return 0


@dataclass(frozen=True)
class InitPatchTarget:
    card_data: dict[str, Any]
    card_source: Path
    card_label: str
    source: patch_helpers.PatchSource


def _resolve_init_target(
    target: str,
    url: str,
    file: str,
    ref: str,
    name: str,
) -> InitPatchTarget:
    card_text, separator, spec_key = target.rpartition(":")
    if not separator or not card_text or not spec_key:
        raise ConfigError("patch target must be '<card>:<spec>'")

    card_path = _resolve_card_path(card_text)
    card = Card.from_file(card_path)
    card_source = patch_helpers.card_source(card)
    card_data = _load_card_data(card_source)
    spec_entry, spec_build = _resolve_card_spec(card, card_data, spec_key, card_source)
    if "patch" in spec_entry:
        raise ConfigError(f"{card_source}: spec '{spec_build.spec}' already has patch")

    patch = PatchRef(type="git", url=url, ref=ref, file=file, name=name)
    source = _patch_source_for_spec(card_source, spec_build, patch)
    _guard_unique_patch_source(card, source)

    spec_entry["patch"] = {
        "type": patch.type,
        "url": patch.url,
        "ref": patch.ref,
        "file": patch.file,
    }
    if patch.name:
        spec_entry["patch"]["name"] = patch.name
    return InitPatchTarget(
        card_data=card_data,
        card_source=card_source,
        card_label=patch_helpers.card_label(card_source),
        source=source,
    )


def _load_card_data(card_source: Path) -> dict[str, Any]:
    data = patch_helpers.load_mapping(card_source)
    if data.get("version") != 1:
        raise ConfigError(f"{card_source}: 'version' must be 1")
    return data


def _resolve_card_spec(
    card: Card,
    card_data: dict[str, Any],
    spec_key: str,
    card_source: Path,
) -> tuple[dict[str, Any], SpecBuild]:
    spec_entries = card_data.get("specs")
    if not isinstance(spec_entries, list):
        raise ConfigError(f"{card_source}: card has no specs")

    matches: list[tuple[dict[str, Any], SpecBuild]] = []
    for index, spec in enumerate(card.specs):
        if index >= len(spec_entries):
            break
        entry = spec_entries[index]
        if not isinstance(entry, dict):
            raise ConfigError(f"{card_source}: 'specs[{index}]' must be a mapping")
        spec_path = Path(spec.spec)
        source = _patch_source_for_spec(
            card_source,
            spec,
            PatchRef(type="git", url="", ref=DEFAULT_PATCH_REF, file=DEFAULT_PATCH_FILE),
        )
        if spec_key in (source.key, spec.spec, spec_path.stem, spec_path.name):
            matches.append((entry, spec))

    if len(matches) == 1:
        return matches[0]
    available = ", ".join(spec.spec for spec in card.specs)
    if not matches:
        raise ConfigError(
            f"{card_source}: spec '{spec_key}' not found. Available: {available}"
        )
    raise ConfigError(f"{card_source}: ambiguous spec '{spec_key}'")


def _patch_source_for_spec(
    card_source: Path,
    spec: SpecBuild,
    patch: PatchRef,
) -> patch_helpers.PatchSource:
    card_base = patch_helpers.card_base_dir(card_source)
    spec_path = patch_helpers.spec_source_path(card_source, card_base, spec)
    source_dir = spec_path.parent
    key = patch_helpers.patch_source_key(card_base, source_dir, patch)
    return patch_helpers.PatchSource(
        key=key,
        source_dir=source_dir,
        spec=SpecBuild(
            spec=spec.spec,
            packages=spec.packages,
            replace=spec.replace,
            files=spec.files,
            hash_revision=spec.hash_revision,
            upstream=spec.upstream,
            patch=patch,
        ),
        patch=patch,
    )


def _guard_unique_patch_source(
    card: Card,
    initialized_source: patch_helpers.PatchSource,
) -> None:
    for source in patch_helpers.patch_sources(card):
        if source.key == initialized_source.key:
            raise ConfigError(
                f"{patch_helpers.card_source(card)}: duplicate patch source "
                f"'{initialized_source.key}'"
            )


def _write_card_data(card_source: Path, card_data: dict[str, Any]) -> None:
    card_source.write_text(
        yaml.dump(card_data, Dumper=IndentedSafeDumper, sort_keys=False),
        encoding="utf-8",
    )


def _resolve_target(target: str) -> PatchTarget:
    card_text, separator, spec_key = target.rpartition(":")
    if not separator or not card_text or not spec_key:
        raise ConfigError("patch target must be '<card>:<spec>'")

    card_path = _resolve_card_path(card_text)
    card = Card.from_file(card_path)
    card_source = patch_helpers.card_source(card)
    card_label = patch_helpers.card_label(card_source)
    source = _resolve_patch_source(card, spec_key, card_source)
    lock_data = patch_helpers.read_lock(patch_helpers.lock_path(card_source))
    patch_sha = patch_helpers.locked_sha(
        lock_data,
        source.key,
        patch_helpers.PATCH_SHA,
    )
    if not patch_sha:
        raise ConfigError(
            f"{patch_helpers.lock_path(card_source)}: missing patch-sha for '{source.key}'"
        )
    return PatchTarget(
        card=card,
        card_source=card_source,
        card_label=card_label,
        source=source,
        patch_sha=patch_sha,
    )


def _resolve_card_path(card_text: str) -> Path:
    raw = Path(card_text).expanduser()
    base_paths = [raw]
    if not raw.is_absolute() and (not raw.parts or raw.parts[0] != "cards"):
        base_paths.append(Path("cards") / raw)

    existing = tuple(
        dict.fromkeys(
            path.resolve()
            for path in (
                _resolve_model_card_path(path.as_posix(), Path.cwd())
                for path in base_paths
            )
            if path.is_file()
        )
    )
    if not existing:
        raise ConfigError(f"card not found for patch target: {card_text}")
    if len(existing) > 1:
        choices = ", ".join(patch_helpers.display_path(path) for path in existing)
        raise ConfigError(f"ambiguous card target '{card_text}': {choices}")
    return existing[0]


def _resolve_patch_source(
    card: Card,
    spec_key: str,
    card_source: Path,
) -> patch_helpers.PatchSource:
    sources = patch_helpers.patch_sources(card)
    if not sources:
        raise ConfigError(f"{card_source}: card has no git patch specs")

    matches = []
    for source in sources:
        spec_path = Path(source.spec.spec)
        if spec_key in (source.key, spec_path.stem, spec_path.name):
            matches.append(source)

    unique = tuple({source.key: source for source in matches}.values())
    if len(unique) == 1:
        return unique[0]
    available = ", ".join(source.key for source in sources)
    if not unique:
        raise ConfigError(
            f"{card_source}: patch spec '{spec_key}' not found. Available: {available}"
        )
    raise ConfigError(f"{card_source}: ambiguous patch spec '{spec_key}'")

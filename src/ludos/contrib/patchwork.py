from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import patch as patch_helpers
from ..logging import log
from ..model import Card, ConfigError


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
) -> int:
    if action == "checkout":
        return checkout_patch(target, patchwork_dir=patchwork_dir)
    if action == "apply":
        return apply_patch(target, patchwork_dir=patchwork_dir)
    raise ConfigError(f"unsupported patch action: {action}")


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
        f"'{patch_helpers.short_sha(resolved.patch_sha)}'"
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
    candidates = [
        raw,
        raw.with_suffix(".yml") if raw.suffix == "" else raw,
        Path("cards") / raw,
        (Path("cards") / raw).with_suffix(".yml") if raw.suffix == "" else Path("cards") / raw,
        Path("cards") / raw / "card.yml",
        Path("cards") / raw / "card.yaml",
    ]
    existing = tuple(
        dict.fromkeys(path.resolve() for path in candidates if path.is_file())
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

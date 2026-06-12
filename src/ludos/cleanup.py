from __future__ import annotations

import datetime as _datetime
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .build import _cache_name, _load_dotenv, _substitute_variables
from .logging import log
from .model import ConfigError, Manifest


CLEANUP_REPOSITORIES = ("repos", "cards", "builds", "builders")


@dataclass(frozen=True)
class CleanupTarget:
    ref: str
    display: str
    size_bytes: int
    image_id: str


def cleanup_local_images(
    *,
    version: str | None = None,
    local_prefix: str = "",
    manifests: tuple[Path, ...] = tuple(),
    dry_run: bool = False,
) -> int:
    podman = shutil.which("podman")
    if not podman:
        raise ConfigError("podman must be installed to clean up local images")

    clean_version = _cleanup_version(version)
    clean_local_prefix = _cleanup_local_prefix(local_prefix)
    manifest_targets = tuple(
        target
        for manifest in manifests
        for target in _manifest_cleanup_targets(manifest)
    )
    stale_images = _stale_local_images(
        podman, clean_version, clean_local_prefix, manifest_targets
    )
    if not stale_images:
        log(f"No stale local cache images found for version: {clean_version}")
        return 0

    action = "Would remove" if dry_run else "Removing"
    total_size = _estimated_total_size(stale_images)
    log(f"{action} {len(stale_images)} stale local cache images")
    log(f"Estimated total saved: {_format_bytes(total_size)}")
    for image in stale_images:
        display = f"{image.display} ({_format_bytes(image.size_bytes)})"
        if dry_run:
            log(f"Would remove image: {display}")
        else:
            log(f"Removing image: {display}")
            subprocess.run([podman, "rmi", image.ref], check=True)

    return 0


def _cleanup_version(value: str | None) -> str:
    if value is None:
        iso_today = _datetime.date.today().isocalendar()
        return f"{iso_today.year}-{iso_today.week:02d}"
    if "/" in value or value in ("", ".", ".."):
        raise ConfigError(f"invalid version cache name '{value}'")
    return value


def _cleanup_local_prefix(value: str) -> str:
    if "/" in value or ":" in value:
        raise ConfigError(f"invalid local_prefix '{value}'")
    return value


def _manifest_cleanup_targets(manifest_path: Path) -> tuple[str, ...]:
    manifest = Manifest.from_file(manifest_path)
    root_dir = manifest_path.resolve().parent
    image = _cache_name(manifest_path.resolve().stem, "image")
    manifest_env = {key: str(value) for key, value in manifest.env.items()}
    local_values = _load_dotenv(root_dir / ".env")
    local_prefix = local_values.pop("local_prefix", manifest.local_prefix)
    local_prefix = _cleanup_local_prefix(local_prefix)
    manifest_env.update(local_values)
    distro = _cache_name(
        _substitute_variables(manifest.distro, manifest_env),
        "distro",
    )
    current = f"localhost/{local_prefix}{image}:{distro}"
    log(f"Keeping manifest image: {current}")
    return (current,)


def _stale_local_images(
    podman: str,
    version: str,
    local_prefix: str,
    manifest_targets: tuple[str, ...] = tuple(),
) -> tuple[CleanupTarget, ...]:
    cache_repositories = {
        f"localhost/{local_prefix}{repository}" for repository in CLEANUP_REPOSITORIES
    }
    manifest_keep_refs = set(manifest_targets)
    manifest_repositories = {
        repository
        for target in manifest_targets
        if (parsed := _split_image_name(target)) is not None
        for repository, _tag in (parsed,)
    }
    current_suffix = f"-{version}"
    result = subprocess.run(
        [podman, "images", "--format", "json"],
        check=True,
        text=True,
        capture_output=True,
    )
    images = json.loads(result.stdout or "[]")
    stale: list[CleanupTarget] = []
    seen: set[str] = set()

    for image in images:
        image_id = image.get("Id")
        image_id = image_id if isinstance(image_id, str) else ""
        image_size = _image_size(image)
        names = _image_names(image)
        for name in names:
            parsed = _split_image_name(name)
            if parsed is None:
                continue
            repository, tag = parsed
            if _keep_named_image(
                name,
                repository,
                tag,
                cache_repositories,
                manifest_repositories,
                manifest_keep_refs,
                current_suffix,
            ):
                continue
            if name not in seen:
                stale.append(CleanupTarget(name, name, image_size, image_id))
                seen.add(name)

        if _is_stale_dangling_image(
            image,
            cache_repositories,
            manifest_repositories,
        ):
            if image_id and image_id not in seen:
                history = ", ".join(_image_history(image)) or "<unknown>"
                stale.append(
                    CleanupTarget(
                        image_id,
                        f"{image_id[:12]} ({history})",
                        image_size,
                        image_id,
                    )
                )
                seen.add(image_id)

    return tuple(stale)


def _image_size(image: dict[str, object]) -> int:
    size = image.get("Size", 0)
    if isinstance(size, int):
        return max(size, 0)
    if isinstance(size, float):
        return max(int(size), 0)
    return 0


def _estimated_total_size(images: tuple[CleanupTarget, ...]) -> int:
    total = 0
    seen_ids = set()
    for image in images:
        key = image.image_id or image.ref
        if key in seen_ids:
            continue
        total += image.size_bytes
        seen_ids.add(key)
    return total


def _format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


def _image_names(image: dict[str, object]) -> tuple[str, ...]:
    names = image.get("Names") or []
    if isinstance(names, str):
        return (names,)
    if isinstance(names, list):
        return tuple(name for name in names if isinstance(name, str))
    return tuple()


def _image_history(image: dict[str, object]) -> tuple[str, ...]:
    history = image.get("History") or []
    if isinstance(history, str):
        return (history,)
    if isinstance(history, list):
        return tuple(item for item in history if isinstance(item, str))
    return tuple()


def _keep_named_image(
    name: str,
    repository: str,
    tag: str,
    cache_repositories: set[str],
    manifest_repositories: set[str],
    manifest_keep_refs: set[str],
    current_suffix: str,
) -> bool:
    if repository in cache_repositories:
        return tag.endswith(current_suffix)
    if repository in manifest_repositories:
        return name in manifest_keep_refs
    return True


def _is_stale_dangling_image(
    image: dict[str, object],
    cache_repositories: set[str],
    manifest_repositories: set[str],
) -> bool:
    if _image_names(image) or not image.get("Dangling"):
        return False
    for history_name in _image_history(image):
        parsed = _split_image_name(history_name)
        if parsed is None:
            continue
        repository, _tag = parsed
        if repository in cache_repositories:
            return True
        if repository in manifest_repositories:
            return True
    return False


def _split_image_name(name: str) -> tuple[str, str] | None:
    if name in ("", "<none>", "<none>:<none>") or ":" not in name:
        return None
    repository, tag = name.rsplit(":", 1)
    if not repository or not tag or tag == "<none>":
        return None
    return repository, tag

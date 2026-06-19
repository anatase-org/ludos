from __future__ import annotations

import datetime as _datetime
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .build import _run_logged_command, resolve_manifest_images
from .logging import log
from .model import ConfigError


VERSIONED_CLEANUP_REPOSITORIES = ("orchestrator", "repos")
RESOLVED_CLEANUP_REPOSITORIES = ("cards", "builds", "builders", "installer")
CLEANUP_REPOSITORIES = (*VERSIONED_CLEANUP_REPOSITORIES, *RESOLVED_CLEANUP_REPOSITORIES)
LATEST_CLEANUP_REPOSITORIES = ("orchestrator", "installer")
INTERMEDIATE_CLEANUP_HINT = (
    "Run these to cleanup intermediates:\n"
    "  buildah rm --all\n"
    "  podman image prune --external"
)


@dataclass(frozen=True)
class CleanupTarget:
    ref: str
    display: str
    size_bytes: int
    image_id: str
    remove_containers: bool = True


@dataclass(frozen=True)
class BuildahContainer:
    id: str
    name: str


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
        for target in _manifest_cleanup_targets(manifest, clean_version)
    )
    stale_images = _stale_local_images(
        podman, clean_version, clean_local_prefix, manifest_targets
    )
    if not stale_images:
        log(f"No stale local cache images found for version: {clean_version}")
        _log_intermediate_cleanup_hint()
        return 0

    action = "Would remove" if dry_run else "Removing"
    log(f"{action} {len(stale_images)} stale local cache images")
    buildah = shutil.which("buildah")
    buildah_containers = _buildah_containers_by_image(buildah) if buildah else {}
    stale_containers = _stale_buildah_containers(stale_images, buildah_containers)
    if dry_run:
        for container in stale_containers:
            log(f"Would remove build container: {container.name}")
    elif buildah:
        for container in stale_containers:
            log(f"Removing build container: {container.name}")
            _run_logged_command(
                [buildah, "rm", container.id],
                "build container removal",
            )

    for image in stale_images:
        display = f"{image.display} ({_format_bytes(image.size_bytes)})"
        if dry_run:
            log(f"Would remove image: {display}")
        else:
            log(f"Removing image: {display}")
            _run_logged_command([podman, "rmi", image.ref], "image removal")

    _log_intermediate_cleanup_hint()
    return 0


def _log_intermediate_cleanup_hint() -> None:
    log(INTERMEDIATE_CLEANUP_HINT)


def _cleanup_version(value: str | None) -> str:
    if value is None:
        return _datetime.date.today().strftime("%Y%m%d")
    if "/" in value or value in ("", ".", ".."):
        raise ConfigError(f"invalid version cache name '{value}'")
    return value


def _cleanup_local_prefix(value: str) -> str:
    if "/" in value or ":" in value:
        raise ConfigError(f"invalid local_prefix '{value}'")
    return value


def _manifest_cleanup_targets(manifest_path: Path, version: str) -> tuple[str, ...]:
    result = resolve_manifest_images(manifest_path, cache_version=version)
    targets = (
        result.output_image,
        result.orchestrator,
        *result.repo_images,
        *result.package_images,
        *result.build_images,
        *result.builder_images,
    )
    log(f"Keeping manifest image: {result.output_image}")
    if result.package_images or result.build_images or result.builder_images:
        log(
            f"Keeping resolved cache images: "
            f"{len(result.package_images)} cards, "
            f"{len(result.build_images)} builds, "
            f"{len(result.builder_images)} builders"
        )
    return targets


def _stale_local_images(
    podman: str,
    version: str,
    local_prefix: str,
    manifest_targets: tuple[str, ...] = tuple(),
) -> tuple[CleanupTarget, ...]:
    versioned_cache_repositories = {
        f"localhost/{local_prefix}{repository}"
        for repository in VERSIONED_CLEANUP_REPOSITORIES
    }
    latest_cache_repositories = {
        f"localhost/{local_prefix}{repository}"
        for repository in LATEST_CLEANUP_REPOSITORIES
    }
    resolved_cache_repositories = {
        f"localhost/{local_prefix}{repository}"
        for repository in RESOLVED_CLEANUP_REPOSITORIES
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
                versioned_cache_repositories,
                latest_cache_repositories,
                resolved_cache_repositories,
                manifest_repositories,
                manifest_keep_refs,
                current_suffix,
            ):
                continue
            if name not in seen:
                stale.append(CleanupTarget(name, name, image_size, image_id))
                seen.add(name)

        if _is_manifest_dangling_image(image, manifest_keep_refs):
            if image_id and image_id not in seen:
                history = ", ".join(_image_history(image)) or "<unknown>"
                stale.append(
                    CleanupTarget(
                        image_id,
                        f"{image_id[:12]} ({history})",
                        image_size,
                        image_id,
                        remove_containers=False,
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


def _stale_buildah_containers(
    images: tuple[CleanupTarget, ...],
    containers_by_image: dict[str, tuple[BuildahContainer, ...]],
) -> tuple[BuildahContainer, ...]:
    containers: list[BuildahContainer] = []
    seen_ids = set()
    for image in images:
        if not image.remove_containers:
            continue
        for container in containers_by_image.get(image.image_id, ()):
            if container.id in seen_ids:
                continue
            containers.append(container)
            seen_ids.add(container.id)
    return tuple(containers)


def _buildah_containers_by_image(
    buildah: str,
) -> dict[str, tuple[BuildahContainer, ...]]:
    result = subprocess.run(
        [buildah, "containers", "--all", "--json"],
        check=True,
        text=True,
        capture_output=True,
    )
    containers = json.loads(result.stdout or "[]")
    by_image: dict[str, list[BuildahContainer]] = {}
    for container in containers:
        if not isinstance(container, dict):
            continue
        image_id = container.get("imageid")
        container_id = container.get("id")
        name = container.get("containername")
        if not (
            isinstance(image_id, str)
            and isinstance(container_id, str)
            and isinstance(name, str)
        ):
            continue
        if not image_id or not container_id:
            continue
        by_image.setdefault(image_id, []).append(BuildahContainer(container_id, name))
    return {image_id: tuple(items) for image_id, items in by_image.items()}


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
    versioned_cache_repositories: set[str],
    latest_cache_repositories: set[str],
    resolved_cache_repositories: set[str],
    manifest_repositories: set[str],
    manifest_keep_refs: set[str],
    current_suffix: str,
) -> bool:
    if name in manifest_keep_refs:
        return True
    if repository in latest_cache_repositories and tag == "latest":
        return True
    if repository in resolved_cache_repositories:
        return False
    if repository in versioned_cache_repositories:
        return tag.endswith(current_suffix)
    if repository in manifest_repositories:
        return name in manifest_keep_refs
    return True


def _is_manifest_dangling_image(
    image: dict[str, object], manifest_keep_refs: set[str]
) -> bool:
    return (
        not _image_names(image)
        and bool(image.get("Dangling"))
        and any(history in manifest_keep_refs for history in _image_history(image))
    )


def _split_image_name(name: str) -> tuple[str, str] | None:
    if name in ("", "<none>", "<none>:<none>") or ":" not in name:
        return None
    repository, tag = name.rsplit(":", 1)
    if not repository or not tag or tag == "<none>":
        return None
    return repository, tag

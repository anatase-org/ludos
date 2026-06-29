from __future__ import annotations

import datetime as _datetime
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..common import (
    _cache_name,
    _image_exists,
    _load_dotenv,
    _local_image,
    _local_prefix,
    _run_streamed_command,
    _substitute_variables,
)
from ..flatpaks import build_flatpak, build_flatpaks
from ..logging import log
from ..model import ConfigError, ManifestValidation, validate_manifest
from .registry import tree_shake_oci, upload_oci


@dataclass(frozen=True)
class FlatpakUploadContext:
    validation: ManifestValidation
    root_dir: Path
    distro: str
    local_prefix: str
    cache_dir: Path
    podman: str


@dataclass(frozen=True)
class FlatpakUploadTarget:
    path: Path
    name: str
    image: str
    export_dir: Path
    ref: str
    tag: str


def upload_flatpaks(
    manifest: Path,
    flatpaks: tuple[Path, ...],
    build: bool,
    cache_dir: Path | None = None,
) -> int:
    context = _resolve_flatpak_upload_context(manifest, cache_dir=cache_dir)
    targets = _upload_targets(context, flatpaks)
    results = _build_targets(
        manifest,
        targets,
        build,
        cache_dir,
        selected_all=not flatpaks,
    )
    for target in targets:
        image = results.get(target.name, target.image)
        if not build and not _image_exists(context.podman, image):
            raise ConfigError(f"flatpak image is not cached: {image}")
        _export_flatpak_image(context.podman, image, target)
        upload_oci(target.export_dir, target.ref, (target.tag,))
    return 0


def tree_shake_flatpaks(
    manifest: Path,
    flatpaks: tuple[Path, ...],
    *,
    dry_run: bool = False,
) -> int:
    context = _resolve_flatpak_upload_context(
        manifest,
        cache_dir=None,
        require_podman=False,
    )
    for target in _upload_targets(context, flatpaks):
        tree_shake_oci(target.ref, dry_run=dry_run)
    return 0


def _resolve_flatpak_upload_context(
    manifest: Path,
    *,
    cache_dir: Path | None,
    require_podman: bool = True,
) -> FlatpakUploadContext:
    manifest_path = manifest.expanduser().resolve()
    log(f"Validating manifest: {manifest}")
    validation = validate_manifest(manifest_path)
    if validation.missing_bootstrap:
        raise ConfigError(
            f"{manifest}: missing bootstrap card: {validation.missing_bootstrap}"
        )
    if validation.missing_repos:
        missing = ", ".join(validation.missing_repos)
        raise ConfigError(f"{manifest}: missing repository definitions: {missing}")
    if validation.missing_cards:
        missing = ", ".join(validation.missing_cards)
        raise ConfigError(f"{manifest}: missing card definitions: {missing}")
    if validation.missing_flatpaks:
        missing = ", ".join(validation.missing_flatpaks)
        raise ConfigError(f"{manifest}: missing flatpak definitions: {missing}")

    root_dir = manifest_path.parent
    manifest_env = {key: str(value) for key, value in validation.manifest.env.items()}
    local_values = _load_dotenv(root_dir / ".env")
    local_prefix = local_values.pop("local_prefix", validation.manifest.local_prefix)
    local_prefix = _local_prefix(local_prefix)
    manifest_env.update(local_values)
    manifest_env["version"] = _datetime.date.today().strftime("%Y%m%d")
    releasever = _cache_name(
        _substitute_variables(validation.manifest.releasever, manifest_env),
        "releasever",
    )
    manifest_env["releasever"] = releasever
    arch = _cache_name(
        _substitute_variables(str(manifest_env.get("arch", "")), manifest_env),
        "arch",
    )
    manifest_env["arch"] = arch
    manifest_env = {
        key: _substitute_variables(value, manifest_env)
        for key, value in manifest_env.items()
    }
    distro = _cache_name(
        _substitute_variables(validation.manifest.distro, manifest_env),
        "distro",
    )

    resolved_cache_dir = (
        root_dir / "cache" if cache_dir is None else cache_dir.expanduser().resolve()
    )
    podman = shutil.which("podman") if require_podman else ""
    if require_podman:
        if not podman:
            raise ConfigError("podman must be installed to upload flatpaks")
        log(f"Using Podman: {podman}")
    return FlatpakUploadContext(
        validation=validation,
        root_dir=root_dir,
        distro=distro,
        local_prefix=local_prefix,
        cache_dir=resolved_cache_dir,
        podman=podman,
    )


def _upload_targets(
    context: FlatpakUploadContext,
    flatpaks: tuple[Path, ...],
) -> tuple[FlatpakUploadTarget, ...]:
    selected = (
        flatpaks
        if flatpaks
        else tuple(Path(flatpak) for flatpak in context.validation.manifest.flatpaks)
    )
    if not selected:
        raise ConfigError("manifest 'flatpaks' must contain at least one item")
    targets = []
    for flatpak in selected:
        path = _flatpak_card_path(_manifest_flatpak_path(flatpak, context.root_dir))
        name = path.parent.resolve().name
        export_dir = context.cache_dir / "flatpaks" / f"{name}-{context.distro}"
        targets.append(
            FlatpakUploadTarget(
                path=path,
                name=name,
                image=_local_image(
                    context.local_prefix,
                    "flatpaks",
                    f"{context.distro}-{name}",
                ),
                export_dir=export_dir,
                ref=f"flatpaks/{name}",
                tag=context.distro,
            )
        )
    return tuple(targets)


def _build_targets(
    manifest: Path,
    targets: tuple[FlatpakUploadTarget, ...],
    build: bool,
    cache_dir: Path | None,
    *,
    selected_all: bool,
) -> dict[str, str]:
    if not build:
        return {}
    if not targets:
        return {}
    if selected_all:
        results = build_flatpaks(manifest, cache_dir=cache_dir)
        return {
            target.name: result.image
            for target, result in zip(targets, results, strict=True)
        }
    images = {}
    for target in targets:
        result = build_flatpak(manifest, target.path, cache_dir=cache_dir)
        images[target.name] = result.image
    return images


def _export_flatpak_image(
    podman: str,
    image: str,
    target: FlatpakUploadTarget,
) -> None:
    _remove_export_dir(target.export_dir)
    target.export_dir.mkdir(parents=True, exist_ok=True)
    log(f"Exporting flatpak OCI image: {target.export_dir}")
    command = [
        podman,
        "push",
        "--format",
        "oci",
        "--compression-format",
        "gzip",
        "--force-compression",
        image,
        f"oci:{target.export_dir}:{target.tag}",
    ]
    returncode, _output = _run_streamed_command(command)
    if returncode != 0:
        raise ConfigError(f"flatpak OCI export failed with exit status {returncode}")


def _remove_export_dir(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _manifest_flatpak_path(flatpak: Path, root_dir: Path) -> Path:
    if flatpak.is_absolute():
        return flatpak
    return root_dir / flatpak


def _flatpak_card_path(flatpak_path: Path) -> Path:
    path = flatpak_path.expanduser().resolve()
    if path.is_dir():
        yaml_path = path / "card.yaml"
        yml_path = path / "card.yml"
        if yaml_path.exists():
            return yaml_path
        if yml_path.exists():
            return yml_path
        raise ConfigError(f"{path}: missing card.yaml")
    if not path.exists():
        raise ConfigError(f"flatpak definition does not exist: {path}")
    return path

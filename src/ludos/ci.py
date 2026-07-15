from __future__ import annotations

import base64
import json
import lzma
import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import yaml

from .build import (
    BuildImagePlan,
    FileRef,
    OciImagePlan,
    PackageImagePlan,
    ResolvedBuildMetadata,
    _create_builder_image,
    _create_package_image,
    _download_exact_packages,
    _ensure_image,
    _image_tag,
    _metadata_with_final_image,
    _remove_image,
    _remove_tree,
    _require_buildah,
    _rpm_filename_nevra,
    build_build_images,
    build_final_manifest_images,
    resolve_build_manifest_context,
    resolve_build_manifests_from_contexts,
)
from .common import (
    ResolvedManifestContext,
    _apply_repo_priority as _apply_repo_priority_from_context,
    _create_orchestrator_image,
    _create_repo_image,
    _default_cache_version,
    _ensure_image as _ensure_context_image,
    _extract_image_paths,
    _image_exists as _local_image_exists,
    _remote_cache_image_exists,
    _require_buildah as _require_context_buildah,
    _run_streamed_command,
    _substitute_variables,
    resolve_manifest_context,
)
from .flatpaks import (
    FlatpakBuildPlan,
    FlatpakCard,
    _ensure_flatpak_images,
    _ensure_flatpak_rpm_builds,
    plan_manifest_flatpaks_with_context,
)
from .logging import log
from .model import ConfigError, FlatpakImagesConfig, Manifest, _spec_builds_tuple


DEFAULT_CI_CACHE_DIR = Path("cache")
DEFAULT_PREPARE_WORKERS = min(4, os.cpu_count() or 1)
DEFAULT_SEED_BUFFER_RATIO = 3
DEFAULT_VERSION_LABEL = "org.opencontainers.image.version"


class SeedDiskSpaceError(ConfigError):
    pass


class _LiteralString(str):
    pass


class _CiBuildManifestDumper(yaml.SafeDumper):
    pass


@dataclass(frozen=True)
class _PreparedFlatpakContext:
    podman: str
    buildah: str | None
    orchestrator: str
    ci_registry: str
    arch: str
    spec_source_cache_dir: Path
    ccache_dir: Path | None
    flatpak_images: FlatpakImagesConfig


def _represent_literal_string(
    dumper: yaml.SafeDumper,
    value: _LiteralString,
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


_CiBuildManifestDumper.add_representer(
    _LiteralString,
    _represent_literal_string,
)


def write_ci_env(
    manifest_path: Path,
    ref: str,
    *,
    label: str = DEFAULT_VERSION_LABEL,
) -> Path:
    manifest_path = manifest_path.expanduser().resolve()
    cache_version = _default_cache_version()
    tag = _manifest_tag(manifest_path, version=cache_version)
    labels = _inspect_remote_labels(ref)
    image_tag = labels.get(label)
    if image_tag is None:
        raise ConfigError(f"OCI image has no '{label}' label: {ref}")

    if image_tag == tag:
        dist = ".1"
    elif image_tag.startswith(tag):
        suffix = image_tag[len(tag) :]
        if not suffix.startswith(".") or not suffix[1:]:
            raise ConfigError(
                f"OCI image label '{label}' has invalid version suffix: {image_tag}"
            )
        try:
            dist = f".{int(suffix[1:]) + 1}"
        except ValueError as exc:
            raise ConfigError(
                f"OCI image label '{label}' has invalid version suffix: {image_tag}"
            ) from exc
    else:
        dist = ""

    output = manifest_path.parent / ".env"
    text = f"version={cache_version}\ndist={dist}\n"
    output.write_text(text, encoding="utf-8")
    log(f"Wrote CI environment: {output}\n{text}")
    return output


def _manifest_tag(manifest_path: Path, version: str | None = None) -> str:
    manifest = Manifest.from_file(manifest_path)
    if not manifest.tag:
        raise ConfigError(f"{manifest_path}: missing 'tag'")
    version = version or _default_cache_version()

    env = {key: str(value) for key, value in manifest.env.items()}
    env["version"] = version
    env["releasever"] = _substitute_variables(manifest.releasever, env)
    env = {
        key: _substitute_variables(value, env)
        for key, value in env.items()
    }
    return _substitute_variables(manifest.tag, env)


def _inspect_remote_labels(ref: str) -> dict[str, str]:
    skopeo = shutil.which("skopeo")
    if not skopeo:
        raise ConfigError("skopeo must be installed to inspect remote OCI images")
    transport_ref = ref if "://" in ref else f"docker://{ref}"
    result = subprocess.run(
        [skopeo, "inspect", "--no-tags", transport_ref],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ConfigError(f"failed to inspect remote OCI image: {ref}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"failed to inspect remote OCI image: {ref}") from exc
    labels = data.get("Labels") or {}
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def prepare_ci(
    manifest_paths: tuple[Path, ...],
    *,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    ccache: bool = True,
    full: bool = False,
    workers: int = DEFAULT_PREPARE_WORKERS,
) -> Path:
    if not manifest_paths:
        raise ConfigError("at least one manifest is required")
    if workers < 1:
        raise ConfigError("workers must be a positive integer")

    cache_root = _resolve_cache_root(manifest_paths, cache_dir)
    manifest_contexts: list[tuple[Path, ResolvedManifestContext]] = []
    for index, manifest_path in enumerate(manifest_paths):
        workspace = _ci_dnf_workspace(cache_root, index, manifest_path)
        _remove_tree(workspace, podman="podman")
        context = resolve_build_manifest_context(
            manifest_path,
            cache_dir=cache_root,
            cache_version=cache_version,
            cache_only=True,
            ccache=ccache,
            dnf_workspace_dir=workspace,
        )
        manifest_contexts.append((manifest_path, context))

    metadata = resolve_build_manifests_from_contexts(
        tuple(manifest_contexts),
        cache_only=False,
        workers=workers,
    )
    metadata = tuple(
        _metadata_with_final_image(item, mode="combined")
        for item in metadata
    )
    flatpaks = tuple(
        _flatpak_entry(manifest_path, context, plan)
        for manifest_path, context in manifest_contexts
        for plan in plan_manifest_flatpaks_with_context(
            context,
            manifest_path=manifest_path,
            cache_only=False,
            workers=workers,
        )
    )
    output = cache_root / "ci" / "build.yml"
    _build_output, encoded_output = _write_ci_build_manifest(
        output,
        manifest_contexts=tuple(manifest_contexts),
        metadata=metadata,
        flatpaks=flatpaks,
        full=full,
        workers=workers,
    )
    log(f"Wrote CI build manifest: {output}")
    log(
        f"Wrote encoded CI build manifest: {encoded_output} "
        f"({_size_kib(encoded_output)} KiB)"
    )
    return output


def init_ci(
    manifest_paths: tuple[Path, ...],
    *,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    recreate: bool = False,
) -> None:
    if not manifest_paths:
        raise ConfigError("at least one manifest is required")

    cache_root = _resolve_cache_root(manifest_paths, cache_dir)
    dnf_workspace_dirs: list[Path] = []
    remote_exists_by_image: dict[str, bool] = {}
    current_ci_registry = [""]

    def image_exists(podman: str, image: str, ci_registry: str) -> bool:
        current_ci_registry[0] = _require_ci_registry(ci_registry)
        remote_exists = _ci_remote_image_exists(podman, image, ci_registry)
        remote_exists_by_image[image] = remote_exists
        if _local_image_exists(podman, image):
            if not remote_exists:
                _push_ci_image(podman, image, ci_registry)
            return True
        if remote_exists and (not recreate or not _is_orchestrator_image(image)):
            return True
        return False

    def create_orchestrator_image(**kwargs: Any) -> None:
        _create_orchestrator_image(**kwargs)
        if not remote_exists_by_image.get(str(kwargs["image"]), False):
            _push_ci_image(
                str(kwargs["podman"]),
                str(kwargs["image"]),
                current_ci_registry[0],
            )

    def create_repo_image(**kwargs: Any) -> None:
        podman = str(kwargs["podman"])
        orchestrator = str(kwargs["orchestrator"])
        if (
            remote_exists_by_image.get(orchestrator, False)
            and not _local_image_exists(podman, orchestrator)
            and not _ensure_context_image(
                podman,
                orchestrator,
                current_ci_registry[0],
            )
        ):
            raise ConfigError(f"failed to pull CI orchestrator image: {orchestrator}")
        _create_repo_image(**kwargs)
        if not remote_exists_by_image.get(str(kwargs["image"]), False):
            _push_ci_image(
                str(kwargs["podman"]),
                str(kwargs["image"]),
                current_ci_registry[0],
            )

    def extract_paths(podman: str, image: str, paths: dict[str, Path]) -> None:
        if _local_image_exists(podman, image):
            _extract_image_paths(podman, image, paths)

    def apply_repo_priority(repo_file: Path, priority: int) -> None:
        if repo_file.exists():
            _apply_repo_priority_from_context(repo_file, priority)

    for manifest_path in manifest_paths:
        try:
            resolve_manifest_context(
                manifest_path,
                cache_dir=cache_root,
                cache_version=cache_version,
                cache_only=False,
                dnf_workspace_dirs=dnf_workspace_dirs,
                image_exists=image_exists,
                create_orchestrator_image=create_orchestrator_image,
                create_repo_image=create_repo_image,
                extract_image_paths=extract_paths,
                apply_repo_priority=apply_repo_priority,
                require_buildah=_require_context_buildah,
            )
        finally:
            for workspace in tuple(dnf_workspace_dirs):
                _remove_tree(workspace)
            dnf_workspace_dirs.clear()


def seed_ci(
    build_manifest: Path | None = None,
    *,
    cache_dir: Path | None = None,
    autoremove: bool = False,
    workers: int = DEFAULT_PREPARE_WORKERS,
    buffer_ratio: float | None = None,
) -> None:
    if workers < 1:
        raise ConfigError("workers must be a positive integer")
    if buffer_ratio is None:
        buffer_ratio = workers * DEFAULT_SEED_BUFFER_RATIO
    if not math.isfinite(buffer_ratio) or buffer_ratio <= 0:
        raise ConfigError("buffer ratio must be a positive finite number")
    build_manifest = build_manifest or _default_ci_build_manifest(cache_dir)
    entries = _read_seed_entries(build_manifest)
    rpm_files_by_image = _prepare_seed_rpms(entries, buffer_ratio=buffer_ratio)
    total = len(entries)
    progress_width = max(2, len(str(total)))
    progress_lock = Lock()
    progress_index = 0

    def seed(
        entry: tuple[str, ResolvedBuildMetadata, str, tuple[str, ...]],
    ) -> None:
        nonlocal progress_index
        section, manifest, image, _packages = entry
        with progress_lock:
            progress_index += 1
            progress = (
                f"({progress_index:0{progress_width}d}/"
                f"{total:0{progress_width}d})"
            )
            log(f"{progress} Creating {image} Image")
        _seed_image(
            manifest,
            image,
            rpm_files_by_image[image],
            builder=section == "builders",
            autoremove=autoremove,
        )

    if entries:
        with ThreadPoolExecutor(max_workers=min(workers, len(entries))) as executor:
            tuple(executor.map(seed, entries))


def build_ci(
    build_ids: tuple[str, ...],
    *,
    build_manifest: Path | None = None,
    builds: bool = False,
    images: bool = False,
    flatpaks: bool = False,
    autoremove: bool = False,
) -> None:
    if not build_ids and not (builds or images or flatpaks):
        raise ConfigError("at least one CI build ID or section flag is required")
    build_manifest = build_manifest or _default_ci_build_manifest(None)
    data = _read_ci_build_data(build_manifest)
    selected = _select_ci_builds(
        build_manifest,
        data,
        build_ids,
        builds=builds,
        images=images,
        flatpaks=flatpaks,
    )
    restored_contexts: set[tuple[str, str, tuple[str, ...]]] = set()
    for section in ("builds", "images", "flatpaks"):
        values = data[section]
        for build_id in selected[section]:
            entry = values[build_id]
            if section == "builds":
                _build_ci_package(
                    build_manifest,
                    build_id,
                    entry,
                    autoremove=autoremove,
                )
            elif section == "images":
                _build_ci_manifest_image(
                    build_manifest,
                    build_id,
                    entry,
                    restored_contexts=restored_contexts,
                    autoremove=autoremove,
                )
            else:
                _build_ci_flatpak(
                    build_manifest,
                    build_id,
                    entry,
                    restored_contexts=restored_contexts,
                    autoremove=autoremove,
                )


def _read_ci_build_data(build_manifest: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(build_manifest.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"CI build manifest not found: {build_manifest}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ConfigError(f"{build_manifest}: unsupported CI build manifest")
    for section in ("builds", "images", "flatpaks"):
        if not isinstance(data.get(section), dict):
            raise ConfigError(f"{build_manifest}: '{section}' must be a mapping")
    return data


def _select_ci_builds(
    build_manifest: Path,
    data: dict[str, Any],
    build_ids: tuple[str, ...],
    *,
    builds: bool,
    images: bool,
    flatpaks: bool,
) -> dict[str, list[str]]:
    sections = ("builds", "images", "flatpaks")
    selected = {section: [] for section in sections}
    selected_sets = {section: set() for section in sections}

    def add(section: str, build_id: str) -> None:
        if build_id not in selected_sets[section]:
            selected_sets[section].add(build_id)
            selected[section].append(build_id)

    for build_id in build_ids:
        if build_id == "0":
            continue
        matches = [section for section in sections if build_id in data[section]]
        if not matches:
            raise ConfigError(f"{build_manifest}: unknown CI build ID: {build_id}")
        if len(matches) > 1:
            raise ConfigError(
                f"{build_manifest}: ambiguous CI build ID '{build_id}' appears in: "
                + ", ".join(matches)
            )
        add(matches[0], build_id)

    for section, include_all in (
        ("builds", builds),
        ("images", images),
        ("flatpaks", flatpaks),
    ):
        if include_all:
            for build_id in data[section]:
                add(section, str(build_id))
    return selected


def _build_ci_package(
    build_manifest: Path,
    build_id: str,
    entry: object,
    *,
    autoremove: bool,
) -> None:
    if not isinstance(entry, dict):
        raise ConfigError(f"{build_manifest}: invalid builds entry '{build_id}'")
    metadata = _metadata_from_mapping(
        build_manifest,
        build_id,
        entry.get("metadata"),
    )
    if isinstance(entry.get("flatpak"), dict):
        context = _prepared_flatpak_context(metadata, entry["flatpak"])
        plan = _prepared_flatpak_plan(
            build_manifest,
            build_id,
            entry["flatpak"],
            metadata,
        )
        if not _ensure_image(
            context.podman,
            plan.builder_image,
            context.ci_registry,
        ):
            raise ConfigError(
                f"flatpak builder image is missing: {plan.builder_image}"
            )
        _ensure_flatpak_rpm_builds(context, (plan,), cache_only=False)
        image = plan.build_image
    else:
        image = str(entry.get("image", ""))
        if not image:
            raise ConfigError(f"{build_manifest}: invalid builds entry '{build_id}'")
        build_build_images(
            (metadata,),
            targets=(image,),
            cache_only=False,
        )
    _upload_ci_output(
        metadata.podman,
        image,
        metadata.ci_registry,
        autoremove=autoremove,
    )


def _build_ci_manifest_image(
    build_manifest: Path,
    build_id: str,
    entry: object,
    *,
    restored_contexts: set[tuple[str, str, tuple[str, ...]]],
    autoremove: bool,
) -> None:
    metadata = _metadata_from_seed_entry(build_manifest, build_id, entry)
    _restore_ci_build_context(
        metadata,
        restored_contexts,
        package_images=True,
        oci_images=True,
    )
    build_outputs = build_build_images((metadata,), cache_only=True)
    result = build_final_manifest_images(
        (metadata,),
        build_outputs=build_outputs,
        mode="combined",
        cache_only=False,
    )[0]
    if result.output_image != metadata.output_image:
        raise ConfigError(
            f"{build_manifest}: image '{build_id}' resolved to unexpected output "
            f"{result.output_image}"
        )
    _upload_ci_output(
        metadata.podman,
        result.output_image,
        metadata.ci_registry,
        autoremove=autoremove,
        aliases=(result.latest_image,),
    )


def _build_ci_flatpak(
    build_manifest: Path,
    build_id: str,
    entry: object,
    *,
    restored_contexts: set[tuple[str, str, tuple[str, ...]]],
    autoremove: bool,
) -> None:
    if not isinstance(entry, dict):
        raise ConfigError(f"{build_manifest}: invalid flatpaks entry '{build_id}'")
    metadata = _metadata_from_mapping(
        build_manifest,
        build_id,
        entry.get("build"),
    )
    _restore_ci_build_context(metadata, restored_contexts)
    context = _prepared_flatpak_context(metadata, entry)
    plan = _prepared_flatpak_plan(build_manifest, build_id, entry, metadata)
    plan = _ensure_flatpak_rpm_builds(
        context,
        (plan,),
        cache_only=True,
    )[0]
    result = _ensure_flatpak_images(
        context,
        (plan,),
        cache_only=False,
    )[0]
    _upload_ci_output(
        metadata.podman,
        result.image,
        metadata.ci_registry,
        autoremove=autoremove,
        aliases=(result.latest_image,),
    )


def _prepared_flatpak_context(
    metadata: ResolvedBuildMetadata,
    entry: dict[str, Any],
) -> _PreparedFlatpakContext:
    flatpak_images = entry.get("flatpak_images")
    if not isinstance(flatpak_images, dict):
        flatpak_images = {}
    return _PreparedFlatpakContext(
        podman=metadata.podman,
        buildah=metadata.buildah,
        orchestrator=metadata.orchestrator,
        ci_registry=metadata.ci_registry,
        arch=metadata.arch,
        spec_source_cache_dir=Path(metadata.spec_source_cache_dir),
        ccache_dir=(Path(metadata.ccache_dir) if metadata.ccache_dir else None),
        flatpak_images=FlatpakImagesConfig(
            uri=str(flatpak_images.get("uri", "")),
            s3=str(flatpak_images.get("s3", "")),
            overlay=str(flatpak_images.get("overlay", "")),
        ),
    )


def _prepared_flatpak_plan(
    build_manifest: Path,
    build_id: str,
    entry: dict[str, Any],
    metadata: ResolvedBuildMetadata,
) -> FlatpakBuildPlan:
    images = entry.get("images")
    paths = entry.get("paths")
    specs = entry.get("specs")
    if not isinstance(images, dict) or not isinstance(paths, dict) or not isinstance(specs, list):
        raise ConfigError(f"{build_manifest}: invalid flatpak build entry '{build_id}'")
    card_path = Path(str(entry.get("source", "")))
    if not card_path.is_absolute():
        card_path = Path(metadata.root_dir) / card_path
    card = FlatpakCard.from_file(card_path)
    return FlatpakBuildPlan(
        card_path=card_path,
        card=card,
        flatpak_dir=Path(str(paths.get("flatpak_dir", card_path.parent))),
        app_name=str(entry.get("app", "")),
        block=str(entry.get("block", "")),
        branch=str(entry.get("branch", "")),
        flatpak_arch=str(entry.get("arch", "")),
        app_ref=str(entry.get("ref", "")),
        output_image=str(images.get("output", "")),
        latest_image=str(images.get("latest", "")),
        substitution_env=_string_mapping(entry.get("substitution_env")),
        build_env=_string_mapping(entry.get("build_env")),
        specs=_spec_builds_tuple({"specs": specs}, "specs", build_manifest),
        prepare_script=str(entry.get("prepare_script", "")),
        spec_revisions=_tuple_pairs(entry.get("spec_revisions")),
        spec_build_dir=Path(str(paths.get("spec_build_dir", ""))),
        artifact_cache_dir=Path(str(paths.get("artifact_cache_dir", ""))),
        final_build_dir=Path(str(paths.get("final_build_dir", ""))),
        rpmbuild_defines=tuple(
            str(item) for item in entry.get("rpmbuild_defines", ())
        ),
        builder_packages=tuple(
            str(item) for item in entry.get("builder_packages", ())
        ),
        builder_image=str(images.get("builder", "")),
        build_image=str(images.get("build", "")),
        metadata=str(entry.get("metadata", "")),
    )


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _restore_ci_build_context(
    metadata: ResolvedBuildMetadata,
    restored_contexts: set[tuple[str, str, tuple[str, ...]]],
    *,
    package_images: bool = False,
    oci_images: bool = False,
) -> None:
    key = (metadata.podman, metadata.root_dir, metadata.repo_images)
    if key not in restored_contexts:
        if not _ensure_image(
            metadata.podman,
            metadata.orchestrator,
            metadata.ci_registry,
        ):
            raise ConfigError(f"CI orchestrator image is missing: {metadata.orchestrator}")
        paths = {
            "repos": Path(metadata.repo_dir),
            "cache": Path(metadata.dnf_cache_dir),
            "persist": Path(metadata.dnf_persist_dir),
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        Path(metadata.dnf_log_dir).mkdir(parents=True, exist_ok=True)
        for repo_image in metadata.repo_images:
            if not _ensure_image(metadata.podman, repo_image, metadata.ci_registry):
                raise ConfigError(f"CI repository image is missing: {repo_image}")
            _extract_image_paths(metadata.podman, repo_image, paths)
        restored_contexts.add(key)

    if package_images:
        for plan in metadata.package_images:
            if not _ensure_image(metadata.podman, plan.image, metadata.ci_registry):
                raise ConfigError(f"CI card package image is missing: {plan.image}")
    if oci_images:
        for plan in metadata.oci_images:
            subprocess.run([metadata.podman, "pull", plan.image], check=True)


def _upload_ci_output(
    podman: str,
    image: str,
    ci_registry: str,
    *,
    autoremove: bool,
    aliases: tuple[str, ...] = tuple(),
) -> None:
    _push_ci_image(podman, image, ci_registry)
    if not autoremove:
        return
    for alias in aliases:
        if alias and alias != image:
            _remove_image(podman, alias)
    _remove_image(podman, image)


def _prepare_seed_rpms(
    entries: tuple[tuple[str, ResolvedBuildMetadata, str, tuple[str, ...]], ...],
    *,
    buffer_ratio: float,
) -> dict[str, tuple[str, ...]]:
    rpm_files_by_image = {
        image: tuple(f"{_rpm_filename_nevra(package)}.rpm" for package in packages)
        for _section, _manifest, image, packages in entries
    }
    groups: dict[
        tuple[tuple[str, ...], Path],
        tuple[ResolvedBuildMetadata, dict[str, None]],
    ] = {}
    for _section, manifest, _image, packages in entries:
        key = (tuple(manifest.orchestrator_dnf_base), Path(manifest.package_dir))
        if key not in groups:
            groups[key] = (manifest, {})
        groups[key][1].update(dict.fromkeys(packages))

    download_batches = []
    missing_files_by_device: dict[int, dict[tuple[Path, str], int]] = {}
    disk_path_by_device: dict[int, Path] = {}
    planned_paths: set[tuple[Path, str]] = set()
    for (_dnf_base, package_dir), (manifest, package_map) in groups.items():
        package_dir.mkdir(parents=True, exist_ok=True)
        package_dir = package_dir.resolve()
        cached_files = {path.name for path in package_dir.rglob("*.rpm")}
        missing_packages = tuple(
            package
            for package in package_map
            if f"{_rpm_filename_nevra(package)}.rpm" not in cached_files
            and (package_dir, f"{_rpm_filename_nevra(package)}.rpm")
            not in planned_paths
        )
        if not missing_packages:
            continue
        sizes = _seed_rpm_download_sizes(
            list(manifest.orchestrator_dnf_base),
            missing_packages,
        )
        device = package_dir.stat().st_dev
        disk_path_by_device.setdefault(device, package_dir)
        device_files = missing_files_by_device.setdefault(device, {})
        for package in missing_packages:
            filename = f"{_rpm_filename_nevra(package)}.rpm"
            path_key = (package_dir, filename)
            planned_paths.add(path_key)
            device_files[path_key] = sizes[filename]
        download_batches.append((manifest, missing_packages))

    missing_count = sum(len(files) for files in missing_files_by_device.values())
    missing_bytes = sum(
        sum(files.values()) for files in missing_files_by_device.values()
    )
    log(
        f"Missing {missing_count} RPMs totaling {_format_seed_bytes(missing_bytes)}"
    )
    for device, files in missing_files_by_device.items():
        required = math.ceil(sum(files.values()) * buffer_ratio)
        disk_path = disk_path_by_device[device]
        available = shutil.disk_usage(disk_path).free
        if available < required:
            raise SeedDiskSpaceError(
                f"not enough disk space for seed RPMs in {disk_path}: "
                f"{_format_seed_bytes(available)} available, "
                f"{_format_seed_bytes(required)} required "
                f"({buffer_ratio:g}x buffer)"
            )

    if missing_count:
        operations = len(download_batches)
        operation_label = (
            "one operation"
            if operations == 1
            else f"{operations} repository-context operations"
        )
        log(f"Downloading {missing_count} RPMs in {operation_label}")
    for manifest, missing_packages in download_batches:
        _download_exact_packages(
            list(manifest.orchestrator_dnf_base),
            missing_packages,
            "/ludos/packages",
        )
    return rpm_files_by_image


def _seed_rpm_download_sizes(
    orchestrator_dnf_base: list[str],
    packages: tuple[str, ...],
) -> dict[str, int]:
    result = subprocess.run(
        [
            *orchestrator_dnf_base,
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=system_cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--disable-repo=*",
            "--enable-repo=*",
            "repoquery",
            "--queryformat",
            "%{location}\t%{downloadsize}\n",
            *packages,
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    sizes = {}
    for line in result.stdout.splitlines():
        fields = line.rsplit("\t", 1)
        if len(fields) != 2:
            continue
        filename = fields[0].rsplit("/", 1)[-1].strip()
        try:
            size = int(fields[1])
        except ValueError:
            continue
        if filename.endswith(".rpm"):
            sizes[filename] = size
    expected = {
        f"{_rpm_filename_nevra(package)}.rpm" for package in packages
    }
    missing = sorted(expected - sizes.keys())
    if missing:
        raise ConfigError(
            "repoquery did not return download sizes for: " + ", ".join(missing)
        )
    return {filename: sizes[filename] for filename in expected}


def _format_seed_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def _resolve_cache_root(
    manifest_paths: tuple[Path, ...],
    cache_dir: Path | None,
) -> Path:
    if cache_dir is not None:
        return cache_dir.expanduser().resolve()
    return (manifest_paths[0].resolve().parent / "cache").resolve()


def _ci_dnf_workspace(cache_root: Path, index: int, manifest_path: Path) -> Path:
    name = manifest_path.resolve().stem
    return cache_root / "ci" / "dnf" / f"{index}-{name}"


def _default_ci_build_manifest(cache_dir: Path | None) -> Path:
    cache_root = cache_dir.expanduser() if cache_dir is not None else DEFAULT_CI_CACHE_DIR
    return cache_root / "ci" / "build.yml"


def _require_ci_registry(ci_registry: str) -> str:
    registry = ci_registry.strip().rstrip("/")
    if not registry:
        raise ConfigError("ci.registry is required")
    return registry


def _ci_remote_image(ci_registry: str, image: str) -> str:
    registry = _require_ci_registry(ci_registry)
    if "@" in image or ":" not in image:
        raise ConfigError(f"image cannot be uploaded to CI registry: {image}")
    repository, tag = image.rsplit(":", 1)
    if not repository or not tag:
        raise ConfigError(f"image cannot be uploaded to CI registry: {image}")
    return f"{registry}/{repository}:{tag}"


def _ci_remote_image_exists(_podman: str, image: str, ci_registry: str) -> bool:
    remote = _ci_remote_image(ci_registry, image)
    return _remote_cache_image_exists(remote)


def _push_ci_image(podman: str, image: str, ci_registry: str) -> None:
    remote = _ci_remote_image(ci_registry, image)
    log(f"Uploading CI image: {remote}")
    returncode, _output = _run_streamed_command([podman, "push", image, remote])
    if returncode != 0:
        raise ConfigError(f"CI image upload failed with exit status {returncode}")


def _is_orchestrator_image(image: str) -> bool:
    repository, _tag = image.rsplit(":", 1)
    return repository.endswith("orchestrator")


def _seed_image(
    manifest: ResolvedBuildMetadata,
    image: str,
    rpm_files: tuple[str, ...],
    *,
    builder: bool,
    autoremove: bool = False,
) -> None:
    _require_ci_registry(manifest.ci_registry)
    if not _local_image_exists(manifest.podman, image):
        create = (
            _create_seed_builder_image
            if builder
            else _create_seed_package_image
        )
        create(manifest, image, rpm_files)
    _push_ci_image(manifest.podman, image, manifest.ci_registry)
    if autoremove:
        _remove_image(manifest.podman, image)


def _create_seed_package_image(
    manifest: ResolvedBuildMetadata,
    image: str,
    rpm_files: tuple[str, ...],
) -> None:
    _create_package_image(
        buildah=_require_buildah(manifest.buildah),
        image=image,
        package_dir=Path(manifest.package_dir),
        rpm_files=rpm_files,
    )


def _create_seed_builder_image(
    manifest: ResolvedBuildMetadata,
    image: str,
    rpm_files: tuple[str, ...],
) -> None:
    _create_builder_image(
        podman=manifest.podman,
        buildah=_require_buildah(manifest.buildah),
        orchestrator=manifest.orchestrator,
        root_dir=Path(manifest.root_dir),
        repo_dir=Path(manifest.repo_dir),
        dnf_cache_dir=Path(manifest.dnf_cache_dir),
        dnf_persist_dir=Path(manifest.dnf_persist_dir),
        dnf_log_dir=Path(manifest.dnf_log_dir),
        image=image,
        package_dir=Path(manifest.package_dir),
        rpm_files=rpm_files,
        releasever=manifest.releasever,
        quiet=True,
    )


def _read_seed_entries(
    build_manifest: Path,
) -> tuple[tuple[str, ResolvedBuildMetadata, str, tuple[str, ...]], ...]:
    data = yaml.safe_load(build_manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ConfigError(f"{build_manifest}: unsupported CI build manifest")
    entries = []
    for section in ("cards", "builders"):
        values = data.get(section)
        if not isinstance(values, dict):
            raise ConfigError(f"{build_manifest}: '{section}' must be a mapping")
        for key, value in values.items():
            if not isinstance(value, dict):
                raise ConfigError(f"{build_manifest}: invalid {section} entry '{key}'")
            metadata = value.get("metadata")
            image = str(value.get("image", ""))
            if not isinstance(metadata, dict) or not image:
                raise ConfigError(f"{build_manifest}: invalid {section} entry '{key}'")
            manifest = _metadata_from_seed_entry(
                build_manifest,
                key,
                {"build": metadata},
            )
            packages = tuple(
                str(package) for package in value.get("packages", ())
            )
            entries.append((section, manifest, image, packages))
    return tuple(entries)


def _metadata_from_seed_entry(
    build_manifest: Path,
    key: object,
    value: object,
) -> ResolvedBuildMetadata:
    if not isinstance(value, dict) or not isinstance(value.get("build"), dict):
        raise ConfigError(f"{build_manifest}: image '{key}' is missing build metadata")
    return _metadata_from_mapping(build_manifest, key, value["build"])


def _metadata_from_mapping(
    build_manifest: Path,
    key: object,
    build: object,
) -> ResolvedBuildMetadata:
    if not isinstance(build, dict):
        raise ConfigError(f"{build_manifest}: image '{key}' is missing build metadata")
    return ResolvedBuildMetadata(
        image=str(build.get("image", "")),
        distro=str(build.get("distro", "")),
        releasever=str(build.get("releasever", "")),
        arch=str(build.get("arch", "")),
        root_dir=str(build.get("root_dir", "")),
        local_prefix=str(build.get("local_prefix", "")),
        orchestrator=str(build.get("orchestrator", "")),
        output_image=str(build.get("output_image", "")),
        manifest_labels=_tuple_pairs(build.get("manifest_labels")),
        manifest_env=_tuple_pairs(build.get("manifest_env")),
        requested_packages=tuple(str(item) for item in build.get("requested_packages", ())),
        resolved_packages=tuple(str(item) for item in build.get("resolved_packages", ())),
        common_packages=tuple(str(item) for item in build.get("common_packages", ())),
        bootstrap_packages=tuple(str(item) for item in build.get("bootstrap_packages", ())),
        card_order=tuple(str(item) for item in build.get("card_order", ())),
        card_packages=_tuple_string_blocks(build.get("card_packages")),
        card_resolutions=_tuple_string_blocks(build.get("card_resolutions")),
        package_ids=_tuple_triples(build.get("package_ids")),
        package_images=_package_image_plans(build.get("package_images")),
        build_images=_build_image_plans(build.get("build_images")),
        oci_images=_oci_image_plans(build.get("oci_images")),
        package_dir=str(build.get("package_dir", "")),
        repo_dir=str(build.get("repo_dir", "")),
        cache_dir=str(build.get("cache_dir", "")),
        build_dir=str(build.get("build_dir", "")),
        card_build_dir=str(build.get("card_build_dir", "")),
        spec_source_cache_dir=str(build.get("spec_source_cache_dir", "")),
        build_artifact_cache_dir=str(build.get("build_artifact_cache_dir", "")),
        ccache_dir=(
            None
            if build.get("ccache_dir") is None
            else str(build.get("ccache_dir"))
        ),
        dnf_workspace_dir=str(build.get("dnf_workspace_dir", "")),
        dnf_cache_dir=str(build.get("dnf_cache_dir", "")),
        dnf_persist_dir=str(build.get("dnf_persist_dir", "")),
        dnf_log_dir=str(build.get("dnf_log_dir", "")),
        dnf_resolve_dir=str(build.get("dnf_resolve_dir", "")),
        podman=str(build.get("podman", "podman")),
        buildah=(
            None
            if build.get("buildah") is None
            else str(build.get("buildah"))
        ),
        cache_version=str(build.get("cache_version", "")),
        repo_images=tuple(str(item) for item in build.get("repo_images", ())),
        orchestrator_dnf_base=tuple(
            str(item) for item in build.get("orchestrator_dnf_base", ())
        ),
        package_blocks=_tuple_string_blocks(build.get("package_blocks")),
        card_file_sets=_card_file_sets(build.get("card_file_sets")),
        postprocess_blocks=_tuple_pairs(build.get("postprocess_blocks")),
        card_envs=_tuple_pair_blocks(build.get("card_envs")),
        card_sources=_tuple_pairs(build.get("card_sources")),
        card_prepare_scripts=_tuple_pairs(build.get("card_prepare_scripts")),
        card_builds=_tuple_pairs(build.get("card_builds")),
        card_specs=_card_specs(
            build.get("card_specs"),
            build_manifest,
        ),
        spec_source_revisions=_tuple_triples(build.get("spec_source_revisions")),
        latest_image=str(build.get("latest_image", "")),
        ci_registry=str(build.get("ci_registry", "")),
    )


def _tuple_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        return tuple()
    return tuple((str(item[0]), str(item[1])) for item in value if _is_pair(item))


def _tuple_triples(value: object) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(value, list):
        return tuple()
    return tuple(
        (str(item[0]), str(item[1]), str(item[2]))
        for item in value
        if isinstance(item, list) and len(item) == 3
    )


def _tuple_string_blocks(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, list):
        return tuple()
    return tuple(
        (str(item[0]), tuple(str(child) for child in item[1]))
        for item in value
        if _is_pair(item) and isinstance(item[1], list)
    )


def _tuple_pair_blocks(
    value: object,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    if not isinstance(value, list):
        return tuple()
    return tuple(
        (str(item[0]), _tuple_pairs(item[1]))
        for item in value
        if _is_pair(item)
    )


def _card_file_sets(
    value: object,
) -> tuple[tuple[str, str, tuple[FileRef, ...]], ...]:
    if not isinstance(value, list):
        return tuple()
    result = []
    for item in value:
        if not isinstance(item, list) or len(item) != 3:
            continue
        refs = item[2]
        if not isinstance(refs, list):
            continue
        result.append(
            (
                str(item[0]),
                str(item[1]),
                tuple(
                    FileRef(
                        original=str(ref.get("original", "")),
                        source=str(ref.get("source", "")),
                        target=str(ref.get("target", "")),
                    )
                    for ref in refs
                    if isinstance(ref, dict)
                ),
            )
        )
    return tuple(result)


def _card_specs(
    value: object,
    build_manifest: Path,
) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    if not isinstance(value, list):
        return tuple()
    result = []
    for item in value:
        if not _is_pair(item) or not isinstance(item[1], list):
            continue
        result.append(
            (
                str(item[0]),
                _spec_builds_tuple({"specs": item[1]}, "specs", build_manifest),
            )
        )
    return tuple(result)


def _is_pair(value: object) -> bool:
    return isinstance(value, list) and len(value) == 2


def _package_image_plans(value: object) -> tuple[PackageImagePlan, ...]:
    if not isinstance(value, dict):
        return tuple()
    return tuple(
        PackageImagePlan(
            block=str(item.get("block", "")),
            packages=tuple(str(package) for package in item.get("packages", ())),
            image=str(item.get("image", "")),
        )
        for item in value.values()
        if isinstance(item, dict)
    )


def _build_image_plans(value: object) -> tuple[BuildImagePlan, ...]:
    if not isinstance(value, dict):
        return tuple()
    return tuple(
        BuildImagePlan(
            block=str(item.get("block", "")),
            image=str(item.get("image", "")),
            builder_image=str(item.get("builder_image", "")),
            builder_packages=tuple(
                str(package) for package in item.get("builder_packages", ())
            ),
            declared_package_ids=_tuple_pairs(item.get("declared_package_ids")),
        )
        for item in value.values()
        if isinstance(item, dict)
    )


def _oci_image_plans(value: object) -> tuple[OciImagePlan, ...]:
    if not isinstance(value, dict):
        return tuple()
    return tuple(
        OciImagePlan(
            block=str(item.get("block", "")),
            name=str(item.get("name", "")),
            image=str(item.get("tagged_image", item.get("image", ""))),
            digest=str(item.get("digest", "")),
            packages=tuple(str(package) for package in item.get("packages", ())),
            declared_package_ids=_tuple_pairs(item.get("declared_package_ids")),
        )
        for item in value.values()
        if isinstance(item, dict)
    )


def _write_ci_build_manifest(
    output: Path,
    *,
    manifest_contexts: tuple[tuple[Path, ResolvedManifestContext], ...],
    metadata: tuple[ResolvedBuildMetadata, ...],
    flatpaks: tuple[dict[str, Any], ...],
    full: bool = False,
    workers: int = DEFAULT_PREPARE_WORKERS,
) -> tuple[Path, Path]:
    log("Checking current registry for image existence")
    metadata_by_manifest = {
        str(manifest_path): _build_entry(manifest_metadata)
        for (manifest_path, _context), manifest_metadata in zip(
            manifest_contexts,
            metadata,
        )
    }
    included_metadata = tuple(
        (manifest_path, manifest_metadata)
        for (manifest_path, _context), manifest_metadata in zip(
            manifest_contexts,
            metadata,
        )
        if full
        or not _logged_ci_remote_image_exists(
            manifest_metadata.podman,
            manifest_metadata.output_image,
            getattr(manifest_metadata, "ci_registry", ""),
        )
    )
    contexts_by_manifest = {
        str(manifest_path): context
        for manifest_path, context in manifest_contexts
    }
    included_flatpaks = tuple(
        entry
        for entry in flatpaks
        if full
        or not _logged_ci_remote_image_exists(
            contexts_by_manifest[str(entry["manifest"])].podman,
            str(entry["images"]["output"]),
            getattr(
                contexts_by_manifest[str(entry["manifest"])],
                "ci_registry",
                "",
            ),
        )
    )
    cards, builders, builds = _missing_ci_dependency_images(
        manifest_contexts=manifest_contexts,
        metadata=tuple(
            (manifest_path, manifest_metadata)
            for (manifest_path, _context), manifest_metadata in zip(
                manifest_contexts,
                metadata,
            )
        ),
        flatpaks=flatpaks,
        metadata_by_manifest=metadata_by_manifest,
        workers=workers,
    )
    image_entries = {
        _image_id(manifest_metadata.output_image): _image_entry(
            manifest_path,
            metadata_by_manifest[str(manifest_path)],
        )
        for manifest_path, manifest_metadata in included_metadata
    }
    flatpak_entries = {}
    for entry in included_flatpaks:
        manifest_path = str(entry["manifest"])
        flatpak_entry = dict(entry)
        flatpak_entry["build"] = metadata_by_manifest[manifest_path]
        flatpak_entry["flatpak_images"] = _to_plain(
            getattr(
                contexts_by_manifest[manifest_path],
                "flatpak_images",
                FlatpakImagesConfig(),
            )
        )
        flatpak_entries[_image_id(entry["images"]["output"])] = flatpak_entry
    payload = {
        "version": 1,
        "cards": cards,
        "builders": builders,
        "builds": builds,
        "images": image_entries,
        "flatpaks": flatpak_entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.dump(payload, Dumper=_CiBuildManifestDumper, sort_keys=False).encode(
        "utf-8"
    )
    tmp = output.with_name(f"{output.name}.tmp")
    tmp.write_bytes(body)
    tmp.replace(output)
    encoded = base64.b64encode(lzma.compress(body, format=lzma.FORMAT_XZ))
    encoded_output = output.with_suffix(f"{output.suffix}.encoded")
    encoded_tmp = encoded_output.with_name(f"{encoded_output.name}.tmp")
    encoded_tmp.write_bytes(encoded)
    encoded_tmp.replace(encoded_output)
    return output, encoded_output


def _missing_ci_dependency_images(
    *,
    manifest_contexts: tuple[tuple[Path, ResolvedManifestContext], ...],
    metadata: tuple[tuple[Path, ResolvedBuildMetadata], ...],
    flatpaks: tuple[dict[str, Any], ...],
    metadata_by_manifest: dict[str, dict[str, Any]],
    workers: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidates: dict[str, dict[str, tuple[str, str, dict[str, Any]]]] = {
        "cards": {},
        "builders": {},
        "builds": {},
    }
    def add(
        section: str,
        podman: str,
        ci_registry: str,
        image: str,
        entry: dict[str, Any],
    ) -> None:
        candidates[section].setdefault(image, (podman, ci_registry, entry))

    for manifest_path, manifest in metadata:
        manifest_metadata = metadata_by_manifest[str(manifest_path)]
        for plan in manifest.package_images:
            if plan.packages:
                card_entry = _to_plain(plan)
                card_entry["manifest"] = str(manifest_path)
                card_entry["metadata"] = manifest_metadata
                add(
                    "cards",
                    manifest.podman,
                    manifest.ci_registry,
                    plan.image,
                    card_entry,
                )
        for plan in manifest.build_images:
            build_entry = _to_plain(plan)
            build_entry["manifest"] = str(manifest_path)
            build_entry["metadata"] = manifest_metadata
            builder_entry = dict(build_entry)
            builder_entry["image"] = plan.builder_image
            builder_entry["build_image"] = plan.image
            builder_entry["packages"] = list(plan.builder_packages)
            add(
                "builders",
                manifest.podman,
                manifest.ci_registry,
                plan.builder_image,
                builder_entry,
            )
            add(
                "builds",
                manifest.podman,
                manifest.ci_registry,
                plan.image,
                build_entry,
            )

    contexts_by_manifest = {
        str(manifest_path): context
        for manifest_path, context in manifest_contexts
    }
    for entry in flatpaks:
        context = contexts_by_manifest[str(entry["manifest"])]
        ci_registry = getattr(context, "ci_registry", "")
        builder_image = str(entry["images"]["builder"])
        build_image = str(entry["images"]["build"])
        flatpak_metadata = _to_plain(entry)
        manifest_metadata = metadata_by_manifest[str(entry["manifest"])]
        add(
            "builders",
            context.podman,
            ci_registry,
            builder_image,
            {
                "image": builder_image,
                "packages": list(entry["builder_packages"]),
                "metadata": manifest_metadata,
                "flatpak": flatpak_metadata,
            },
        )
        add(
            "builds",
            context.podman,
            ci_registry,
            build_image,
            {
                "image": build_image,
                "metadata": manifest_metadata,
                "flatpak": flatpak_metadata,
            },
        )

    checks = tuple(
        (podman, image, ci_registry)
        for images in candidates.values()
        for image, (podman, ci_registry, _entry) in images.items()
    )

    def exists(check: tuple[str, str, str]) -> tuple[tuple[str, str, str], bool]:
        return check, _logged_ci_remote_image_exists(check[0], check[1], check[2])

    exists_by_check: dict[tuple[str, str, str], bool] = {}
    if checks:
        with ThreadPoolExecutor(max_workers=min(workers, len(checks))) as executor:
            exists_by_check.update(executor.map(exists, checks))

    def missing(section: str) -> dict[str, Any]:
        return {
            _image_id(image): entry
            for image, (podman, ci_registry, entry) in candidates[section].items()
            if not exists_by_check[(podman, image, ci_registry)]
        }

    return missing("cards"), missing("builders"), missing("builds")


def _logged_ci_remote_image_exists(
    podman: str,
    image: str,
    ci_registry: str,
) -> bool:
    exists = _ci_remote_image_exists(podman, image, ci_registry)
    action = "Reusing" if exists else "Creating"
    log(f"{action} {image} Image")
    return exists


def _size_kib(path: Path) -> int:
    return (path.stat().st_size + 1023) // 1024


def _image_entry(
    manifest_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "path": str(manifest_path),
        "build": metadata,
    }


def _build_entry(metadata: ResolvedBuildMetadata) -> dict[str, Any]:
    build = _to_plain(
        metadata,
        omit_fields=frozenset(
            {
                "requested_packages",
                "resolved_packages",
            }
        ),
    )
    build["package_images"] = _image_plan_mapping(metadata.package_images)
    build["build_images"] = _image_plan_mapping(metadata.build_images)
    build["oci_images"] = _oci_image_plan_mapping(metadata.oci_images)
    return build


def _image_plan_mapping(plans: tuple[Any, ...]) -> dict[str, Any]:
    return {
        _image_id(plan.image): _to_plain(plan)
        for plan in plans
    }


def _oci_image_plan_mapping(plans: tuple[Any, ...]) -> dict[str, Any]:
    return {
        _oci_image_id(plan): _pinned_oci_image_plan(plan)
        for plan in plans
    }


def _oci_image_id(plan: Any) -> str:
    return f"{plan.name}-{_image_id(plan.image)}"


def _pinned_oci_image_plan(plan: Any) -> dict[str, Any]:
    entry = _to_plain(plan)
    entry["tagged_image"] = plan.image
    entry["image"] = _pinned_image(plan.image, plan.digest)
    return entry


def _pinned_image(image: str, digest: str) -> str:
    if not digest:
        return image
    if "@" in image:
        return image
    return f"{image}@{digest}"


def _image_id(image: str) -> str:
    return _image_tag(image)


def _flatpak_entry(
    manifest_path: Path,
    context: ResolvedManifestContext,
    plan: FlatpakBuildPlan,
) -> dict[str, Any]:
    return {
        "manifest": str(manifest_path),
        "source": _display_path(plan.card_path, context.root_dir),
        "app": plan.app_name,
        "block": plan.block,
        "ref": plan.app_ref,
        "branch": plan.branch,
        "arch": plan.flatpak_arch,
        "images": {
            "output": plan.output_image,
            "latest": plan.latest_image,
            "build": plan.build_image,
            "builder": plan.builder_image,
        },
        "paths": {
            "flatpak_dir": str(plan.flatpak_dir),
            "spec_build_dir": str(plan.spec_build_dir),
            "artifact_cache_dir": str(plan.artifact_cache_dir),
            "final_build_dir": str(plan.final_build_dir),
        },
        "build_env": _to_plain(plan.build_env),
        "substitution_env": _to_plain(plan.substitution_env),
        "specs": _to_plain(plan.specs),
        "spec_revisions": _to_plain(plan.spec_revisions),
        "prepare_script": plan.prepare_script,
        "builder_packages": list(plan.builder_packages),
        "rpmbuild_defines": list(plan.rpmbuild_defines),
        "metadata": _to_plain(plan.metadata),
    }


def _display_path(path: Path, root_dir: Path) -> str:
    try:
        return path.resolve().relative_to(root_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _to_plain(value: Any, *, omit_fields: frozenset[str] = frozenset()) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _to_plain(getattr(value, field.name))
            for field in fields(value)
            if field.name not in omit_fields
        }
    if isinstance(value, dict):
        return {
            key: _to_plain(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (tuple, list)):
        return [_to_plain(item) for item in value]
    if isinstance(value, str) and "\n" in value:
        return _LiteralString(value)
    return value


def _cleanup_contexts(
    manifest_contexts: tuple[tuple[Path, ResolvedManifestContext], ...],
) -> None:
    for _manifest_path, context in manifest_contexts:
        _remove_tree(context.dnf_workspace_dir, podman=context.podman)

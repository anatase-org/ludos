from __future__ import annotations

import base64
import datetime as _datetime
import hashlib
import io
import json
import shutil
import struct
import tarfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from ..common import (
    _cache_name,
    _image_exists,
    _load_dotenv,
    _local_image,
    _local_prefix,
    _run_streamed_command,
    _substitute_variables,
)
from ..flatpaks import (
    DEFAULT_FLATPAK_SDK,
    build_flatpak,
    build_flatpaks,
    _flatpak_arch,
)
from ..logging import log
from ..model import ConfigError, ManifestRuntime, ManifestValidation, validate_manifest
from .registry import tree_shake_oci, update_flatpak_static_index, upload_oci


OCI_ARCHES = {
    "x86_64": "amd64",
    "aarch64": "arm64",
}
DEFAULT_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
DEFAULT_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
DEFAULT_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
DUMMY_RUNTIME_TIMESTAMP = "0"


@dataclass(frozen=True)
class FlatpakUploadContext:
    validation: ManifestValidation
    root_dir: Path
    distro: str
    arch: str
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


def update_flatpak_index(manifest: Path) -> int:
    context = _resolve_flatpak_upload_context(
        manifest,
        cache_dir=None,
        require_podman=False,
    )
    return update_flatpak_static_index(context.distro)


def upload_dummy_runtime(
    manifest: Path,
    cache_dir: Path | None = None,
) -> int:
    context = _resolve_flatpak_upload_context(
        manifest,
        cache_dir=cache_dir,
        require_podman=False,
        require_flatpaks=False,
    )
    runtime = _require_runtime_config(context.validation.manifest.runtime, manifest)
    flatpak_arch = _flatpak_arch(context.arch)
    runtime_ref = f"runtime/{runtime.id}/{flatpak_arch}/{runtime.branch}"
    layout_dir = context.cache_dir / "flatpaks" / f"{runtime.repo}-{context.distro}"
    _write_dummy_runtime_oci_layout(
        layout_dir,
        runtime=runtime,
        runtime_ref=runtime_ref,
        flatpak_arch=flatpak_arch,
        oci_arch=_oci_arch(context.arch),
        author=_dummy_runtime_author(runtime),
    )
    upload_oci(layout_dir, f"flatpaks/{runtime.repo}", (context.distro,))
    return update_flatpak_static_index(context.distro)


def _resolve_flatpak_upload_context(
    manifest: Path,
    *,
    cache_dir: Path | None,
    require_podman: bool = True,
    require_flatpaks: bool = True,
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
    if require_flatpaks and validation.missing_flatpaks:
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
        arch=arch,
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


def _require_runtime_config(
    runtime: ManifestRuntime | None,
    manifest: Path,
) -> ManifestRuntime:
    if runtime is None:
        raise ConfigError(f"{manifest}: 'runtime' is required to upload dummy runtime")
    return runtime


def _oci_arch(arch: str) -> str:
    try:
        return OCI_ARCHES[arch]
    except KeyError as exc:
        raise ConfigError(f"flatpak runtime upload does not support architecture: {arch}") from exc


def _write_dummy_runtime_oci_layout(
    layout_dir: Path,
    *,
    runtime: ManifestRuntime,
    runtime_ref: str,
    flatpak_arch: str,
    oci_arch: str,
    author: str,
) -> None:
    _remove_export_dir(layout_dir)
    blobs_dir = layout_dir / "blobs" / "sha256"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    (layout_dir / "oci-layout").write_text(
        json.dumps({"imageLayoutVersion": "1.0.0"}) + "\n",
        encoding="utf-8",
    )

    metadata = _dummy_runtime_metadata(runtime, flatpak_arch)
    layer_bytes = _dummy_runtime_layer(metadata)
    layer = _write_oci_blob(blobs_dir, layer_bytes)
    diff_id = layer["digest"]
    labels = _dummy_runtime_labels(
        runtime=runtime,
        runtime_ref=runtime_ref,
        flatpak_arch=flatpak_arch,
        metadata=metadata,
        timestamp=DUMMY_RUNTIME_TIMESTAMP,
        download_size=1,
        installed_size=1,
        author=author,
    )
    config = {
        "architecture": oci_arch,
        "os": "linux",
        "config": {
            "Labels": labels,
            "Annotations": {},
        },
        "rootfs": {
            "type": "layers",
            "diff_ids": [diff_id],
        },
        "history": [
            {
                "created_by": "ludos registry flatpak init-dummy-runtime",
                "comment": runtime_ref,
            }
        ],
    }
    config_blob = _write_oci_blob(blobs_dir, _json_bytes(config))
    manifest = {
        "schemaVersion": 2,
        "mediaType": DEFAULT_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": DEFAULT_CONFIG_MEDIA_TYPE,
            "digest": config_blob["digest"],
            "size": config_blob["size"],
        },
        "layers": [
            {
                "mediaType": DEFAULT_LAYER_MEDIA_TYPE,
                "digest": layer["digest"],
                "size": layer["size"],
            }
        ],
        "annotations": {
            "org.opencontainers.image.ref.name": runtime_ref,
        },
    }
    manifest_blob = _write_oci_blob(blobs_dir, _json_bytes(manifest))
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": DEFAULT_MANIFEST_MEDIA_TYPE,
                "digest": manifest_blob["digest"],
                "size": manifest_blob["size"],
                "annotations": {
                    "org.opencontainers.image.ref.name": runtime_ref,
                },
            }
        ],
    }
    (layout_dir / "index.json").write_bytes(_json_bytes(index))
    log(f"Wrote dummy flatpak runtime OCI layout: {layout_dir}")


def _dummy_runtime_metadata(runtime: ManifestRuntime, flatpak_arch: str) -> str:
    return (
        "[Runtime]\n"
        f"name={runtime.id}\n"
        f"runtime={runtime.id}/{flatpak_arch}/{runtime.branch}\n"
        f"sdk={DEFAULT_FLATPAK_SDK}/{flatpak_arch}/{runtime.branch}\n"
    )


def _dummy_runtime_labels(
    *,
    runtime: ManifestRuntime,
    runtime_ref: str,
    flatpak_arch: str,
    metadata: str,
    timestamp: str,
    download_size: int,
    installed_size: int,
    author: str,
) -> dict[str, str]:
    metadata_variant = base64.b64encode(metadata.encode() + b"\0\0s").decode()
    runtime_ref_variant = base64.b64encode(runtime_ref.encode() + b"\0\0s").decode()
    ref_binding = bytearray(runtime_ref.encode() + b"\0")
    if len(ref_binding) > 255:
        raise ConfigError("flatpak runtime ref is too large for commit metadata")
    ref_binding.append(len(ref_binding))
    ref_binding.extend(b"\0as")
    ref_binding_variant = base64.b64encode(bytes(ref_binding)).decode()
    collection_binding_variant = base64.b64encode(b"\0\0s").decode()
    download_size_variant = base64.b64encode(
        struct.pack(">Q", download_size) + b"\0t"
    ).decode()
    installed_size_variant = base64.b64encode(
        struct.pack(">Q", installed_size) + b"\0t"
    ).decode()
    labels = {
        "org.flatpak.ref": runtime_ref,
        "org.flatpak.metadata": metadata,
        "org.flatpak.commit-metadata.xa.metadata": metadata_variant,
        "org.flatpak.commit-metadata.xa.ref": runtime_ref_variant,
        "org.flatpak.commit-metadata.ostree.ref-binding": ref_binding_variant,
        "org.flatpak.commit-metadata.ostree.collection-binding": collection_binding_variant,
        "org.flatpak.commit-metadata.xa.download-size": download_size_variant,
        "org.flatpak.commit-metadata.xa.installed-size": installed_size_variant,
        "org.flatpak.download-size": str(download_size),
        "org.flatpak.installed-size": str(installed_size),
        "org.flatpak.timestamp": timestamp,
        "org.opencontainers.image.ref.name": runtime_ref,
        "org.anatase.flatpak.branch": runtime.branch,
        "org.anatase.flatpak.arch": flatpak_arch,
    }
    if runtime.title:
        labels["org.flatpak.subject"] = runtime.title
        labels["org.opencontainers.image.title"] = runtime.title
    if runtime.description:
        labels["org.flatpak.body"] = runtime.description
        labels["org.opencontainers.image.description"] = runtime.description
    if runtime.license:
        labels["org.opencontainers.image.licenses"] = runtime.license
        labels["org.freedesktop.appstream.appdata"] = _dummy_runtime_appstream(
            runtime,
            runtime_ref,
            author,
        )
    if author:
        labels["org.opencontainers.image.authors"] = author
        labels["org.opencontainers.image.vendor"] = author
    return labels


def _dummy_runtime_author(runtime: ManifestRuntime) -> str:
    return runtime.author or runtime.title or runtime.id


def _dummy_runtime_appstream(
    runtime: ManifestRuntime,
    runtime_ref: str,
    author: str,
) -> str:
    name = runtime.title or runtime.id
    summary = runtime.description or name
    escaped_author = escape(author)
    return _compact_xml(
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<components version=\"1.0\">\n"
        "  <component type=\"runtime\">\n"
        f"    <id>{escape(runtime.id)}</id>\n"
        f"    <bundle type=\"flatpak\">{escape(runtime_ref)}</bundle>\n"
        f"    <name>{escape(name)}</name>\n"
        f"    <summary>{escape(summary)}</summary>\n"
        f"    <project_group>{escaped_author}</project_group>\n"
        "    <developer>\n"
        f"      <name>{escaped_author}</name>\n"
        "    </developer>\n"
        f"    <developer_name>{escaped_author}</developer_name>\n"
        "    <metadata_license>CC0-1.0</metadata_license>\n"
        f"    <project_license>{escape(runtime.license)}</project_license>\n"
        "  </component>\n"
        "</components>\n"
    )


def _compact_xml(value: str) -> str:
    return " ".join(line.strip() for line in value.splitlines())


def _dummy_runtime_layer(metadata: str) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as tar:
        files_info = tarfile.TarInfo("files")
        files_info.type = tarfile.DIRTYPE
        files_info.mode = 0o755
        _stabilize_tar_info(files_info)
        tar.addfile(files_info)
        metadata_bytes = metadata.encode("utf-8")
        metadata_info = tarfile.TarInfo("metadata")
        metadata_info.size = len(metadata_bytes)
        metadata_info.mode = 0o644
        _stabilize_tar_info(metadata_info)
        tar.addfile(metadata_info, io.BytesIO(metadata_bytes))
    return stream.getvalue()


def _stabilize_tar_info(info: tarfile.TarInfo) -> None:
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""

def _json_bytes(data: object) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _write_oci_blob(blobs_dir: Path, data: bytes) -> dict[str, object]:
    digest = hashlib.sha256(data).hexdigest()
    (blobs_dir / digest).write_bytes(data)
    return {"digest": f"sha256:{digest}", "size": len(data)}


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

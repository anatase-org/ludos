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
from typing import Any, Mapping
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
    _flatpak_appstream_labels_with_remote_icon,
)
from ..logging import log
from ..model import (
    ConfigError,
    FlatpakImagesConfig,
    ManifestRuntime,
    ManifestValidation,
    Project,
    validate_manifest,
)
from .common import (
    REGISTRY_SHORT_CACHE_CONTROL,
    _create_s3_client,
    _s3_config_from_env,
)
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
    flatpak_images: FlatpakImagesConfig


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
    *,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
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
        labels = _prepare_exported_flatpak_metadata(context, target)
        upload_oci(target.export_dir, target.ref, (target.tag,))
        _upload_flatpak_icon(context, labels, environ=environ, client=client)
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
    flatpak_images = _project_flatpak_images(root_dir)
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
        flatpak_images=flatpak_images,
    )


def _project_flatpak_images(root_dir: Path) -> FlatpakImagesConfig:
    project_config = root_dir / "ludos.yml"
    if not project_config.exists():
        return FlatpakImagesConfig()
    return Project.from_file(project_config).flatpak_images


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


def _prepare_exported_flatpak_metadata(
    context: FlatpakUploadContext,
    target: FlatpakUploadTarget,
) -> dict[str, str]:
    if not context.flatpak_images.uri and not context.flatpak_images.s3:
        return {}

    state = _read_exported_flatpak_state(target.export_dir)
    labels = state["labels"]
    if context.flatpak_images.uri:
        app_id = _flatpak_ref_app_id(labels.get("org.flatpak.ref", ""))
        labels = _flatpak_appstream_labels_with_remote_icon(
            labels,
            app_id,
            context.flatpak_images.uri,
        )
        if labels != state["labels"]:
            _write_exported_flatpak_state(target.export_dir, state, labels)
    return labels


def _read_exported_flatpak_state(export_dir: Path) -> dict[str, Any]:
    blobs_dir = export_dir / "blobs" / "sha256"
    index = _read_json_file(export_dir / "index.json", "flatpak OCI index")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ConfigError(f"{export_dir}: flatpak OCI index must contain one manifest")
    manifest_desc = manifests[0]
    manifest_digest = _descriptor_digest(manifest_desc, "flatpak OCI manifest")
    manifest = _read_json_file(
        blobs_dir / manifest_digest.removeprefix("sha256:"),
        "flatpak OCI manifest",
    )
    config_desc = manifest.get("config")
    config_digest = _descriptor_digest(config_desc, "flatpak OCI config")
    config = _read_json_file(
        blobs_dir / config_digest.removeprefix("sha256:"),
        "flatpak OCI config",
    )
    config_data = config.get("config")
    if not isinstance(config_data, dict):
        raise ConfigError(f"{export_dir}: flatpak OCI config.config must be a mapping")
    labels = config_data.get("Labels")
    if labels is None:
        labels = {}
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in labels.items()
    ):
        raise ConfigError(f"{export_dir}: flatpak OCI config labels must be strings")
    return {
        "index": index,
        "manifest": manifest,
        "config": config,
        "labels": dict(labels),
    }


def _write_exported_flatpak_state(
    export_dir: Path,
    state: dict[str, Any],
    labels: dict[str, str],
) -> None:
    blobs_dir = export_dir / "blobs" / "sha256"
    config = state["config"]
    config.setdefault("config", {})["Labels"] = labels
    config_blob = _write_oci_blob(blobs_dir, _json_bytes(config))

    manifest = state["manifest"]
    manifest["config"]["digest"] = config_blob["digest"]
    manifest["config"]["size"] = config_blob["size"]
    manifest_blob = _write_oci_blob(blobs_dir, _json_bytes(manifest))

    index = state["index"]
    index["manifests"][0]["digest"] = manifest_blob["digest"]
    index["manifests"][0]["size"] = manifest_blob["size"]
    (export_dir / "index.json").write_bytes(_json_bytes(index))


def _read_json_file(path: Path, what: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"invalid {what}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{what} must be a JSON object")
    return data


def _descriptor_digest(value: object, what: str) -> str:
    if not isinstance(value, dict):
        raise ConfigError(f"{what} descriptor must be a JSON object")
    digest = value.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ConfigError(f"{what} descriptor is missing sha256 digest")
    return digest


def _flatpak_ref_app_id(ref: str) -> str:
    parts = ref.split("/")
    if len(parts) != 4 or parts[0] != "app" or not parts[1]:
        raise ConfigError(f"invalid flatpak app ref: {ref}")
    return parts[1]


def _upload_flatpak_icon(
    context: FlatpakUploadContext,
    labels: dict[str, str],
    *,
    environ: Mapping[str, str] | None,
    client: Any | None,
) -> None:
    if not context.flatpak_images.s3:
        return
    icon_label = labels.get("org.freedesktop.appstream.icon-128")
    if not icon_label:
        return
    app_id = _flatpak_ref_app_id(labels.get("org.flatpak.ref", ""))
    icon = _decode_png_data_url(icon_label)
    if context.flatpak_images.overlay:
        overlay = _flatpak_image_overlay_path(
            context.root_dir,
            context.flatpak_images.overlay,
        )
        icon = _overlay_icon(
            icon,
            overlay,
        )

    config = _s3_config_from_env(environ)
    s3 = client if client is not None else _create_s3_client(config, environ)
    key = _join_s3_key(context.flatpak_images.s3, "128x128", f"{app_id}.png")
    log(f"Uploading flatpak icon: {key}")
    try:
        s3.put_object(
            Bucket=config.bucket,
            Key=key,
            Body=icon,
            ContentType="image/png",
            CacheControl=REGISTRY_SHORT_CACHE_CONTROL,
        )
    except Exception as exc:
        raise ConfigError(f"S3 upload failed for {key}: {exc}") from exc


def _decode_png_data_url(value: str) -> bytes:
    payload = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        return base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ConfigError("invalid flatpak icon base64 data") from exc


def _overlay_icon(icon: bytes, overlay: Path) -> bytes:
    if not overlay.is_file():
        raise ConfigError(f"flatpak icon overlay does not exist: {overlay}")
    try:
        from PIL import Image
    except ImportError as exc:
        raise ConfigError(
            "Pillow must be installed to use flatpaks.images.overlay"
        ) from exc

    with Image.open(io.BytesIO(icon)).convert("RGBA") as base:
        with Image.open(overlay).convert("RGBA") as overlay_image:
            if overlay_image.size != base.size:
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                overlay_image = overlay_image.resize(base.size, resample)
            base.alpha_composite(overlay_image)
        output = io.BytesIO()
        base.save(output, format="PNG")
        return output.getvalue()


def _flatpak_image_overlay_path(root_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root_dir / path


def _join_s3_key(base: str, *parts: str) -> str:
    key = "/".join((base.strip("/"), *(part.strip("/") for part in parts)))
    if key.startswith("/") or ".." in key.split("/") or "//" in key:
        raise ConfigError(f"invalid flatpak image S3 key: {key}")
    return key


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
    icon = (
        f"    <icon type=\"remote\" width=\"128\" height=\"128\">"
        f"{escape(runtime.image)}</icon>\n"
        if runtime.image
        else ""
    )
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
        f"{icon}"
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

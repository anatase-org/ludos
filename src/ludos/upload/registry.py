from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..logging import log, piter, warning
from ..model import ConfigError
from .common import _client_error_code, _create_s3_client, _s3_config_from_env


DEFAULT_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
DOCKER_MANIFEST_LIST_MEDIA_TYPE = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
DEFAULT_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
DEFAULT_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
OCI_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
OCI_MUTABLE_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
REGISTRY_PING_BODY = b"{}"

_REF_COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHORT_SHA256_RE = re.compile(r"sha256:([0-9a-f]{11})[0-9a-f]{53}")


@dataclass(frozen=True)
class Descriptor:
    digest: str
    media_type: str
    size: int

    @property
    def hex_digest(self) -> str:
        _algorithm, value = self.digest.split(":", 1)
        return value


def registry_init(
    *,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    config = _s3_config_from_env(environ)
    s3 = client if client is not None else _create_s3_client(config, environ)
    for key in ("v2/", "v2"):
        log(f"Uploading registry ping object: {key}")
        try:
            s3.put_object(
                Bucket=config.bucket,
                Key=key,
                Body=REGISTRY_PING_BODY,
                ContentType="application/json",
                CacheControl=OCI_MUTABLE_CACHE_CONTROL,
            )
        except Exception as exc:
            raise ConfigError(f"S3 upload failed for {key}: {exc}") from exc
    return 0


def upload_oci(
    path: Path,
    ref: str,
    tags: tuple[str, ...],
    *,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    source = Path(path)
    repo_ref = _validate_ref(ref)
    tag_list = _validate_tags(tags)
    layout = _read_layout(source)

    config = _s3_config_from_env(environ)
    s3 = client if client is not None else _create_s3_client(config, environ)
    bucket = config.bucket

    log(f"Uploading OCI repository: {repo_ref}")
    total_bytes = (
        sum(layer.size for layer in layout.layers)
        + layout.config.size
        + len(layout.manifest_bytes) * (1 + len(tag_list))
    )
    with piter(
        total=total_bytes,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="Uploading OCI bytes",
    ) as overall:
        uploaded_layer_bytes = 0
        for layer in layout.layers:
            uploaded = _upload_blob_if_needed(
                s3,
                bucket,
                _blob_key(repo_ref, layer.digest),
                layout.blob_path(layer),
                layer.size,
                layer.media_type,
                overall,
                object_type="layer",
            )
            if uploaded:
                uploaded_layer_bytes += layer.size

        _upload_blob_if_needed(
            s3,
            bucket,
            _blob_key(repo_ref, layout.config.digest),
            layout.blob_path(layout.config),
            layout.config.size,
            layout.config.media_type,
            overall,
            object_type="config",
        )

        manifest_key = _manifest_key(repo_ref, layout.manifest.digest)
        _put_object_if_needed(
            s3,
            bucket,
            manifest_key,
            layout.manifest_bytes,
            layout.manifest.media_type,
            overall,
            object_type="manifest",
        )

        for tag in tag_list:
            tag_key = _tag_key(repo_ref, tag)
            log(f"Uploading tag: {_display_key(tag_key)}")
            try:
                s3.put_object(
                    Bucket=bucket,
                    Key=tag_key,
                    Body=layout.manifest_bytes,
                    ContentType=layout.manifest.media_type,
                    CacheControl=OCI_MUTABLE_CACHE_CONTROL,
                )
            except Exception as exc:
                raise ConfigError(f"S3 upload failed for {tag_key}: {exc}") from exc
            overall.update(len(layout.manifest_bytes))

    log(f"Uploaded {_format_mb(uploaded_layer_bytes)}.")
    log(f"Uploaded OCI repository {repo_ref} with tags: {', '.join(tag_list)}")
    return 0


def delete_oci_tags(
    ref: str,
    tags: tuple[str, ...],
    *,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    repo_ref = _validate_ref(ref)
    tag_list = _validate_tags(tags, command="registry oci delete")
    config = _s3_config_from_env(environ)
    s3 = client if client is not None else _create_s3_client(config, environ)
    for tag in tag_list:
        _delete_tag(s3, config.bucket, repo_ref, tag, dry_run=dry_run)
    return 0


def list_oci_tags(
    ref: str,
    *,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    repo_ref = _validate_ref(ref)
    config = _s3_config_from_env(environ)
    s3 = client if client is not None else _create_s3_client(config, environ)
    for tag in sorted(_list_oci_tags(s3, config.bucket, repo_ref)):
        log(tag)
    return 0


def prune_oci_tags(
    ref: str,
    pattern: str,
    *,
    rule: str,
    number: int,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    repo_ref = _validate_ref(ref)
    _validate_prune_args(pattern, rule, number)
    config = _s3_config_from_env(environ)
    s3 = client if client is not None else _create_s3_client(config, environ)

    tags = [
        tag
        for tag in _list_oci_tags(s3, config.bucket, repo_ref)
        if fnmatch.fnmatchcase(tag, pattern)
    ]
    tags = _sort_prune_tags(tags, rule)
    for tag in tags[number:]:
        _delete_tag(s3, config.bucket, repo_ref, tag, dry_run=dry_run)
    return 0


def tree_shake_oci(
    ref: str,
    *,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    repo_ref = _validate_ref(ref)
    config = _s3_config_from_env(environ)
    s3 = client if client is not None else _create_s3_client(config, environ)

    manifest_keys = _list_object_keys(s3, config.bucket, f"v2/{repo_ref}/manifests/")
    references = _referenced_oci_digests(s3, config.bucket, repo_ref, manifest_keys)
    for key, digest in _oci_digest_manifest_keys(manifest_keys, repo_ref):
        if digest in references.manifest_digests:
            continue
        _delete_manifest(s3, config.bucket, key, dry_run=dry_run)
    for key in _list_object_keys(s3, config.bucket, f"v2/{repo_ref}/blobs/"):
        digest = key.rsplit("/", 1)[-1]
        if digest in references.blob_digests:
            continue
        _delete_blob(s3, config.bucket, key, dry_run=dry_run)
    return 0


@dataclass(frozen=True)
class OciReferences:
    manifest_digests: set[str]
    blob_digests: set[str]


@dataclass(frozen=True)
class OciLayout:
    root: Path
    manifest: Descriptor
    manifest_bytes: bytes
    config: Descriptor
    layers: tuple[Descriptor, ...]

    def blob_path(self, descriptor: Descriptor) -> Path:
        return self.root / "blobs" / "sha256" / descriptor.hex_digest


def _read_layout(path: Path) -> OciLayout:
    if not path.is_dir():
        raise ConfigError(f"OCI path is not a directory: {path}")

    index_path = path / "index.json"
    layout_path = path / "oci-layout"
    blobs_dir = path / "blobs" / "sha256"
    if not index_path.is_file():
        raise ConfigError(f"OCI layout is missing index.json: {path}")
    if not layout_path.is_file():
        raise ConfigError(f"OCI layout is missing oci-layout: {path}")
    if not blobs_dir.is_dir():
        raise ConfigError(f"OCI layout is missing blobs/sha256: {path}")

    index = _read_json(index_path, "OCI index")
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise ConfigError("OCI index.json must contain a manifests list")
    if len(manifests) != 1:
        raise ConfigError("OCI index.json must contain exactly one manifest")
    manifest = _descriptor(
        manifests[0],
        default_media_type=DEFAULT_MANIFEST_MEDIA_TYPE,
        what="manifest",
    )
    if manifest.media_type in (OCI_INDEX_MEDIA_TYPE, DOCKER_MANIFEST_LIST_MEDIA_TYPE):
        raise ConfigError("OCI image indexes are not supported for registry oci upload")

    manifest_path = path / "blobs" / "sha256" / manifest.hex_digest
    manifest_bytes = _read_blob_bytes(manifest_path, manifest)
    manifest_json = _loads_json(manifest_bytes, f"manifest {manifest.digest}")
    config = _descriptor(
        manifest_json.get("config"),
        default_media_type=DEFAULT_CONFIG_MEDIA_TYPE,
        what="config",
    )
    layers_json = manifest_json.get("layers")
    if not isinstance(layers_json, list):
        raise ConfigError("OCI manifest must contain a layers list")
    layers = tuple(
        _descriptor(
            layer,
            default_media_type=DEFAULT_LAYER_MEDIA_TYPE,
            what=f"layer {index}",
        )
        for index, layer in enumerate(layers_json)
    )

    layout = OciLayout(
        root=path,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        config=config,
        layers=layers,
    )
    _validate_blob(layout.blob_path(config), config)
    for layer in layers:
        _validate_blob(layout.blob_path(layer), layer)
    return layout


def _read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"invalid {what}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{what} must be a JSON object")
    return data


def _loads_json(data: bytes, what: str) -> dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ConfigError(f"invalid {what}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"{what} must be a JSON object")
    return parsed


def _descriptor(
    data: object,
    *,
    default_media_type: str,
    what: str,
) -> Descriptor:
    if not isinstance(data, dict):
        raise ConfigError(f"OCI {what} descriptor must be an object")
    digest = data.get("digest")
    if not isinstance(digest, str):
        raise ConfigError(f"OCI {what} descriptor is missing digest")
    _validate_digest(digest, what)
    size = data.get("size")
    if not isinstance(size, int) or size < 0:
        raise ConfigError(f"OCI {what} descriptor is missing size")
    media_type = data.get("mediaType", default_media_type)
    if not isinstance(media_type, str) or not media_type:
        raise ConfigError(f"OCI {what} descriptor has invalid mediaType")
    return Descriptor(digest=digest, media_type=media_type, size=size)


def _validate_digest(digest: str, what: str) -> None:
    try:
        algorithm, value = digest.split(":", 1)
    except ValueError as exc:
        raise ConfigError(f"OCI {what} digest must be algorithm:value") from exc
    if algorithm != "sha256":
        raise ConfigError(f"OCI {what} digest uses unsupported algorithm: {algorithm}")
    if not _SHA256_RE.match(value):
        raise ConfigError(f"OCI {what} digest is not a valid sha256 digest")


def _validate_blob(path: Path, descriptor: Descriptor) -> None:
    if not path.is_file():
        raise ConfigError(f"OCI blob is missing: {path}")
    size = path.stat().st_size
    if size != descriptor.size:
        raise ConfigError(
            f"OCI blob size mismatch for {descriptor.digest}: "
            f"expected {descriptor.size}, got {size}"
        )


def _read_blob_bytes(path: Path, descriptor: Descriptor) -> bytes:
    _validate_blob(path, descriptor)
    return path.read_bytes()


def _validate_ref(ref: str) -> str:
    if "://" in ref:
        raise ConfigError("OCI ref must not include a protocol")
    if ref.startswith("/"):
        raise ConfigError("OCI ref must be relative")
    if not ref:
        raise ConfigError("OCI ref must not be empty")
    parts = ref.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ConfigError("OCI ref must not contain empty, '.', or '..' segments")
    for part in parts:
        if not _REF_COMPONENT_RE.match(part):
            raise ConfigError(f"invalid OCI ref component: {part}")
    return "/".join(parts)


def _validate_tags(
    tags: tuple[str, ...],
    *,
    command: str = "registry oci upload",
) -> tuple[str, ...]:
    if not tags:
        raise ConfigError(f"{command} requires at least one --tag")
    seen = set()
    result = []
    for tag in tags:
        if not _TAG_RE.match(tag):
            raise ConfigError(f"invalid OCI tag: {tag}")
        if tag in seen:
            raise ConfigError(f"duplicate OCI tag: {tag}")
        seen.add(tag)
        result.append(tag)
    return tuple(result)


def _blob_key(ref: str, digest: str) -> str:
    return f"v2/{ref}/blobs/{digest}"


def _manifest_key(ref: str, digest: str) -> str:
    return f"v2/{ref}/manifests/{digest}"


def _tag_key(ref: str, tag: str) -> str:
    return f"v2/{ref}/manifests/{tag}"


def _validate_prune_args(pattern: str, rule: str, number: int) -> None:
    if not pattern:
        raise ConfigError("registry oci prune requires a non-empty --pattern")
    if rule != "descending":
        raise ConfigError(f"unsupported registry oci prune rule: {rule}")
    if number < 0:
        raise ConfigError("registry oci prune --number must be zero or greater")


def _sort_prune_tags(tags: list[str], rule: str) -> list[str]:
    if rule == "descending":
        return sorted(tags, reverse=True)
    raise ConfigError(f"unsupported registry oci prune rule: {rule}")


def _list_oci_tags(client: Any, bucket: str, ref: str) -> tuple[str, ...]:
    prefix = f"v2/{ref}/manifests/"
    tags: list[str] = []
    for _key, tag in _list_oci_tag_manifest_keys(client, bucket, ref):
        tags.append(tag)
    return tuple(tags)


def _list_oci_tag_manifest_keys(
    client: Any,
    bucket: str,
    ref: str,
) -> tuple[tuple[str, str], ...]:
    return _oci_tag_manifest_keys(
        _list_object_keys(client, bucket, f"v2/{ref}/manifests/"),
        ref,
    )


def _oci_tag_manifest_keys(
    keys: tuple[str, ...],
    ref: str,
) -> tuple[tuple[str, str], ...]:
    tags: list[tuple[str, str]] = []
    for key, tag in _oci_manifest_names(keys, ref):
        if "/" in tag or tag.startswith("sha") or not _TAG_RE.match(tag):
            continue
        tags.append((key, tag))
    return tuple(tags)


def _oci_digest_manifest_keys(
    keys: tuple[str, ...],
    ref: str,
) -> tuple[tuple[str, str], ...]:
    manifests: list[tuple[str, str]] = []
    for key, name in _oci_manifest_names(keys, ref):
        if "/" in name or not name.startswith("sha"):
            continue
        manifests.append((key, name))
    return tuple(manifests)


def _oci_manifest_names(keys: tuple[str, ...], ref: str) -> tuple[tuple[str, str], ...]:
    prefix = f"v2/{ref}/manifests/"
    return tuple((key, key[len(prefix) :]) for key in keys if key.startswith(prefix))


def _list_object_keys(client: Any, bucket: str, prefix: str) -> tuple[str, ...]:
    keys: list[str] = []
    continuation_token: str | None = None
    while True:
        request: dict[str, object] = {
            "Bucket": bucket,
            "Prefix": prefix,
        }
        if continuation_token is not None:
            request["ContinuationToken"] = continuation_token
        try:
            response = client.list_objects_v2(**request)
        except Exception as exc:
            raise ConfigError(f"S3 list failed for {prefix}: {exc}") from exc
        for item in response.get("Contents", []):
            key = item.get("Key") if isinstance(item, dict) else None
            if isinstance(key, str) and key.startswith(prefix):
                keys.append(key)
        if not response.get("IsTruncated"):
            return tuple(keys)
        token = response.get("NextContinuationToken")
        if not isinstance(token, str) or not token:
            raise ConfigError(f"S3 list failed for {prefix}: missing continuation token")
        continuation_token = token


def _referenced_oci_digests(
    client: Any,
    bucket: str,
    ref: str,
    manifest_keys: tuple[str, ...],
) -> OciReferences:
    manifest_digests: set[str] = set()
    blob_digests: set[str] = set()
    tag_manifest_keys = _oci_tag_manifest_keys(manifest_keys, ref)
    log(f"Downloading {len(tag_manifest_keys)} manifests")
    with piter(
        tag_manifest_keys,
        desc="Downloading manifests",
        unit="manifest",
    ) as manifests:
        for key, _tag in manifests:
            data = _read_s3_object(client, bucket, key)
            manifest_digests.add(f"sha256:{hashlib.sha256(data).hexdigest()}")
            manifest = _loads_json(data, f"manifest object {key}")
            config = _descriptor(
                manifest.get("config"),
                default_media_type=DEFAULT_CONFIG_MEDIA_TYPE,
                what=f"manifest object {key} config",
            )
            blob_digests.add(config.digest)
            layers = manifest.get("layers")
            if not isinstance(layers, list):
                raise ConfigError(f"manifest object {key} must contain a layers list")
            for index, layer in enumerate(layers):
                descriptor = _descriptor(
                    layer,
                    default_media_type=DEFAULT_LAYER_MEDIA_TYPE,
                    what=f"manifest object {key} layer {index}",
                )
                blob_digests.add(descriptor.digest)
    return OciReferences(manifest_digests=manifest_digests, blob_digests=blob_digests)


def _read_s3_object(client: Any, bucket: str, key: str) -> bytes:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise ConfigError(f"S3 download failed for {key}: {exc}") from exc
    body = response.get("Body")
    if body is None:
        return b""
    data = body.read()
    if isinstance(data, str):
        return data.encode("utf-8")
    return data


def _delete_blob(
    client: Any,
    bucket: str,
    key: str,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        log(f"Would delete blob: {_display_key(key)}")
        return
    log(f"Deleting blob: {_display_key(key)}")
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise ConfigError(f"S3 delete failed for {key}: {exc}") from exc


def _delete_manifest(
    client: Any,
    bucket: str,
    key: str,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        log(f"Would delete manifest: {_display_key(key)}")
        return
    log(f"Deleting manifest: {_display_key(key)}")
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise ConfigError(f"S3 delete failed for {key}: {exc}") from exc


def _delete_tag(
    client: Any,
    bucket: str,
    ref: str,
    tag: str,
    *,
    dry_run: bool,
) -> None:
    key = _tag_key(ref, tag)
    if dry_run:
        log(f"Would delete tag: {_display_key(key)}")
        return
    log(f"Deleting tag: {_display_key(key)}")
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _client_error_code(exc) in ("404", "NoSuchKey", "NotFound"):
            warning(f"OCI tag is already missing: {ref}:{tag}")
            return
        raise ConfigError(f"S3 delete failed for {key}: {exc}") from exc


def _upload_blob_if_needed(
    client: Any,
    bucket: str,
    key: str,
    path: Path,
    size: int,
    content_type: str,
    overall_progress: Any,
    object_type: str,
) -> bool:
    if _object_matches_size(client, bucket, key, size):
        log(f"Skipping {object_type}: {_display_key(key)}")
        overall_progress.update(size)
        return False
    log(f"Uploading {object_type}: {_display_key(key)}")
    try:
        with piter(
            total=size,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            leave=False,
            desc=f"Uploading {_display_digest_filename(path.name)}",
        ) as progress:

            def update_progress(bytes_count: int) -> None:
                progress.update(bytes_count)
                overall_progress.update(bytes_count)

            client.upload_file(
                str(path),
                bucket,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "CacheControl": OCI_IMMUTABLE_CACHE_CONTROL,
                },
                Callback=update_progress,
            )
    except Exception as exc:
        raise ConfigError(f"S3 upload failed for {key}: {exc}") from exc
    return True


def _format_mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}MB"


def _display_key(key: str) -> str:
    return _SHORT_SHA256_RE.sub(r"sha256:\1...", key)


def _display_digest_filename(name: str) -> str:
    if _SHA256_RE.match(name):
        return _display_key(f"sha256:{name}")
    return name


def _put_object_if_needed(
    client: Any,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
    overall_progress: Any,
    object_type: str,
) -> None:
    if _object_matches_size(client, bucket, key, len(body)):
        log(f"Skipping {object_type}: {_display_key(key)}")
        overall_progress.update(len(body))
        return
    log(f"Uploading {object_type}: {_display_key(key)}")
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl=OCI_IMMUTABLE_CACHE_CONTROL,
        )
    except Exception as exc:
        raise ConfigError(f"S3 upload failed for {key}: {exc}") from exc
    overall_progress.update(len(body))


def _object_matches_size(
    client: Any,
    bucket: str,
    key: str,
    size: int,
) -> bool:
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _client_error_code(exc) in ("404", "NoSuchKey", "NotFound"):
            return False
        raise ConfigError(f"S3 stat failed for {key}: {exc}") from exc
    return response.get("ContentLength") == size

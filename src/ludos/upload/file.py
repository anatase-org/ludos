from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..logging import log, piter
from ..model import ConfigError
from .common import (
    S3Config,
    _client_error_code,
    _create_s3_client,
    _normalize_object_key,
    _s3_config_from_env,
)


SHA256SUMS = "SHA256SUMS"
SHA256SUMS_LIMIT = 20
HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class S3Object:
    config: S3Config
    key: str

    @property
    def checksum_key(self) -> str:
        if "/" not in self.key:
            return SHA256SUMS
        prefix, _name = self.key.rsplit("/", 1)
        return f"{prefix}/{SHA256SUMS}"


def upload_file(
    path: Path,
    output_path: str,
    download_name: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"upload source is not a file: {source}")
    if download_name is not None:
        _validate_download_name(download_name)

    target = _s3_object(output_path, environ=environ)
    s3 = client if client is not None else _create_s3_client(target.config, environ)
    log(f"Calculating digest for {source}")
    digest = _sha256_file(source)
    log(f"Digest of {source}\n{digest}")
    checksum_name = download_name or source.name
    extra_args = {}
    if download_name is not None:
        extra_args["ContentDisposition"] = _content_disposition(download_name)
    size_bytes = source.stat().st_size

    log(f"Uploading file: {source} -> {target.key}")
    try:
        with piter(
            total=size_bytes,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=f"Uploading {source.name}",
        ) as progress:
            s3.upload_file(
                str(source),
                target.config.bucket,
                target.key,
                ExtraArgs=extra_args,
                Callback=progress.update,
            )
    except Exception as exc:
        raise ConfigError(f"S3 upload failed for {target.key}: {exc}") from exc

    entries = _read_sha256sums(s3, target)
    updated = _update_sha256sums(entries, digest, checksum_name)
    _write_sha256sums(s3, target, updated)
    log(f"Uploaded {target.key} and updated {target.checksum_key}")
    return 0


def delete_file(
    output_path: str,
    *,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    target = _s3_object(output_path, environ=environ)
    s3 = client if client is not None else _create_s3_client(target.config, environ)
    log(f"Deleting file: {target.key}")
    try:
        s3.delete_object(Bucket=target.config.bucket, Key=target.key)
    except Exception as exc:
        raise ConfigError(f"S3 delete failed for {target.key}: {exc}") from exc
    return 0


def _s3_object(
    output_path: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> S3Object:
    return S3Object(_s3_config_from_env(environ), _normalize_object_key(output_path))


def _validate_download_name(download_name: str) -> None:
    if not download_name:
        raise ConfigError("download name must not be empty")
    if download_name in (".", ".."):
        raise ConfigError("download name must be a filename")
    if any(char in download_name for char in ("/", "\\", "\n", "\r", "\0")):
        raise ConfigError("download name must not contain separators or newlines")


def _content_disposition(download_name: str) -> str:
    escaped = download_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'attachment; filename="{escaped}"'


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _read_sha256sums(client: Any, target: S3Object) -> list[tuple[str, str]]:
    try:
        response = client.get_object(
            Bucket=target.config.bucket,
            Key=target.checksum_key,
        )
    except Exception as exc:
        if _client_error_code(exc) in ("404", "NoSuchKey", "NotFound"):
            return []
        raise ConfigError(f"S3 download failed for {target.checksum_key}: {exc}") from exc
    body = response.get("Body")
    if body is None:
        return []
    data = body.read()
    if isinstance(data, str):
        text = data
    else:
        text = data.decode("utf-8")
    return _parse_sha256sums(text)


def _write_sha256sums(
    client: Any,
    target: S3Object,
    entries: list[tuple[str, str]],
) -> None:
    text = "".join(f"{digest} {name}\n" for digest, name in entries)
    try:
        client.put_object(
            Bucket=target.config.bucket,
            Key=target.checksum_key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
    except Exception as exc:
        raise ConfigError(f"S3 upload failed for {target.checksum_key}: {exc}") from exc


def _parse_sha256sums(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            digest, name = line.split(maxsplit=1)
        except ValueError:
            continue
        entries.append((digest, name))
    return entries


def _update_sha256sums(
    entries: list[tuple[str, str]],
    digest: str,
    download_name: str,
) -> list[tuple[str, str]]:
    preserved = [entry for entry in entries if entry[1] != download_name]
    return [(digest, download_name), *preserved][:SHA256SUMS_LIMIT]

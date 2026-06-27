from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlunparse, urlparse

from ..logging import log, piter
from ..model import ConfigError


SHA256SUMS = "SHA256SUMS"
SHA256SUMS_LIMIT = 20
HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str
    bucket: str


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
    download_name: str,
    *,
    environ: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> int:
    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"upload source is not a file: {source}")
    _validate_download_name(download_name)

    target = _s3_object(output_path, environ=environ)
    s3 = client if client is not None else _create_s3_client(target.config, environ)
    log(f"Calculating digest for {source}")
    digest = _sha256_file(source)
    log(f"Digest of {source}\n{digest}")
    content_disposition = _content_disposition(download_name)
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
                ExtraArgs={"ContentDisposition": content_disposition},
                Callback=progress.update,
            )
    except Exception as exc:
        raise ConfigError(f"S3 upload failed for {target.key}: {exc}") from exc

    entries = _read_sha256sums(s3, target)
    updated = _update_sha256sums(entries, digest, download_name)
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


def _s3_config_from_env(environ: Mapping[str, str] | None = None) -> S3Config:
    env = os.environ if environ is None else environ
    api = env.get("LUDOS_S3_API", "")
    if not api:
        raise ConfigError("LUDOS_S3_API must be set")
    if not env.get("LUDOS_S3_KEY"):
        raise ConfigError("LUDOS_S3_KEY must be set")
    if not env.get("LUDOS_S3_SECRET"):
        raise ConfigError("LUDOS_S3_SECRET must be set")

    parsed = urlparse(api)
    if not parsed.scheme or not parsed.netloc:
        raise ConfigError("LUDOS_S3_API must be an absolute URL with a bucket path")
    if parsed.params or parsed.query or parsed.fragment:
        raise ConfigError("LUDOS_S3_API must not include params, query, or fragment")
    bucket_parts = [part for part in parsed.path.split("/") if part]
    if len(bucket_parts) != 1:
        raise ConfigError("LUDOS_S3_API must include exactly one bucket path segment")
    endpoint_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return S3Config(endpoint_url=endpoint_url, bucket=bucket_parts[0])


def _create_s3_client(
    config: S3Config,
    environ: Mapping[str, str] | None = None,
) -> Any:
    env = os.environ if environ is None else environ
    try:
        import boto3
    except ImportError as exc:
        raise ConfigError("boto3 must be installed to upload files to S3") from exc
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=env["LUDOS_S3_KEY"],
        aws_secret_access_key=env["LUDOS_S3_SECRET"],
    )


def _normalize_object_key(output_path: str) -> str:
    if not output_path:
        raise ConfigError("output path must not be empty")
    if output_path.startswith("/"):
        raise ConfigError("output path must be a relative S3 object key")
    if output_path.endswith("/"):
        raise ConfigError("output path must not be a directory key")
    parts = output_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ConfigError("output path must not contain empty, '.', or '..' segments")
    return "/".join(parts)


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


def _client_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""

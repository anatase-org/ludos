from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

from ..model import ConfigError


REGISTRY_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
REGISTRY_SHORT_CACHE_CONTROL = "public, max-age=60"


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str
    bucket: str


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
        raise ConfigError(
            "boto3 must be installed to access S3; "
            "install ludos[images] or ludos[flatpaks]"
        ) from exc
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


def _client_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    if not isinstance(error, dict):
        return ""
    code = error.get("Code")
    return code if isinstance(code, str) else ""

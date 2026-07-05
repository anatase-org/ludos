from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from ..logging import log, warning
from ..model import ConfigError, OciCosignConfig
from .common import (
    REGISTRY_IMMUTABLE_CACHE_CONTROL,
    REGISTRY_SHORT_CACHE_CONTROL,
    _client_error_code,
)
from .sign_utils import gcloud_sign, parse_gcloud_key_uri, run_streamed_command


COSIGN_SIGNATURE_TYPE = "cosign container image signature"
COSIGN_PAYLOAD_MEDIA_TYPE = "application/vnd.dev.cosign.simplesigning.v1+json"
COSIGN_ARTIFACT_TYPE = COSIGN_PAYLOAD_MEDIA_TYPE
COSIGN_EMPTY_CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_IMAGE_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
COSIGN_SIGNATURE_ANNOTATION = "dev.cosignproject.cosign/signature"
COSIGN_CERTIFICATE_ANNOTATION = "dev.sigstore.cosign/certificate"
COSIGN_CERTIFICATE_ANNOTATION_LEGACY = "dev.cosignproject.cosign/certificate"
COSIGN_BUNDLE_ANNOTATION = "dev.sigstore.cosign/bundle"
EMPTY_JSON = b"{}"


@dataclass(frozen=True)
class CosignSigningConfig:
    key_uri: str
    cert_path: Path
    root_path: Path
    registry: str
    identity: str
    signer: Any | None = None


@dataclass(frozen=True)
class CosignArtifacts:
    payload: bytes
    signature: bytes
    config: bytes
    legacy_manifest: bytes
    referrer_manifest: bytes
    referrers_index: bytes
    legacy_manifest_digest: str
    referrer_manifest_digest: str
    payload_digest: str
    config_digest: str


def config_from_env(
    cosign: OciCosignConfig,
    *,
    project_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> CosignSigningConfig:
    env = os.environ if environ is None else environ
    key_uri = env.get("LUDOS_COSIGN_KEY", "").strip()
    cert_value = env.get("LUDOS_COSIGN_CERT", "").strip()
    if not key_uri:
        raise ConfigError("LUDOS_COSIGN_KEY is required")
    if not cert_value:
        raise ConfigError("LUDOS_COSIGN_CERT is required")
    if not cosign.registry:
        raise ConfigError("oci.cosign.registry is required")
    if not cosign.identity:
        raise ConfigError("oci.cosign.identity is required")
    if not cosign.verify:
        raise ConfigError("oci.cosign.verify is required")
    parse_gcloud_key_uri(key_uri, env_name="LUDOS_COSIGN_KEY")
    cert_path = _project_path(Path(cert_value), project_root)
    root_path = _project_path(Path(cosign.verify), project_root)
    try:
        _verify_certificate_identity(cert_path, cosign.identity)
    except ConfigError as exc:
        raise ConfigError(
            "LUDOS_COSIGN_CERT must point to the leaf certificate for "
            f"{cosign.identity}: {exc}"
        ) from exc
    return CosignSigningConfig(
        key_uri=key_uri,
        cert_path=cert_path,
        root_path=root_path,
        registry=cosign.registry,
        identity=cosign.identity,
    )


def sign_oci_manifest(
    *,
    repo: str,
    manifest_digest: str,
    manifest_media_type: str,
    manifest_size: int,
    cosign: OciCosignConfig,
    project_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    signer: Any | None = None,
    signing_config: CosignSigningConfig | None = None,
) -> tuple[CosignSigningConfig, CosignArtifacts]:
    config = signing_config or config_from_env(
        cosign,
        project_root=project_root,
        environ=environ,
    )
    if signer is not None:
        config = CosignSigningConfig(
            key_uri=config.key_uri,
            cert_path=config.cert_path,
            root_path=config.root_path,
            registry=config.registry,
            identity=config.identity,
            signer=signer,
        )
    payload = cosign_payload(
        registry=config.registry,
        repo=repo,
        manifest_digest=manifest_digest,
    )
    signature = _sign_payload(payload, config)
    return config, build_cosign_artifacts(
        payload=payload,
        signature=signature,
        certificate=config.cert_path.read_text(encoding="utf-8"),
        manifest_digest=manifest_digest,
        manifest_media_type=manifest_media_type,
        manifest_size=manifest_size,
    )


def cosign_payload(*, registry: str, repo: str, manifest_digest: str) -> bytes:
    reference = cosign_docker_reference(registry, repo)
    body = {
        "critical": {
            "type": COSIGN_SIGNATURE_TYPE,
            "image": {"docker-manifest-digest": manifest_digest},
            "identity": {"docker-reference": reference},
        },
        "optional": {},
    }
    return (
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def cosign_docker_reference(registry: str, repo: str) -> str:
    parsed = urlparse(registry.rstrip("/"))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError("oci.cosign.registry must be an http(s) URL")
    if parsed.params or parsed.query or parsed.fragment:
        raise ConfigError(
            "oci.cosign.registry must not include params, query, or fragment"
        )
    path = parsed.path.strip("/")
    prefix = parsed.netloc if not path else f"{parsed.netloc}/{path}"
    return f"{prefix}/{repo}"


def build_cosign_artifacts(
    *,
    payload: bytes,
    signature: bytes,
    certificate: str,
    manifest_digest: str,
    manifest_media_type: str,
    manifest_size: int,
) -> CosignArtifacts:
    signature_b64 = base64.b64encode(signature).decode("ascii")
    config_digest = _digest(EMPTY_JSON)
    payload_digest = _digest(payload)
    annotations = {
        COSIGN_SIGNATURE_ANNOTATION: signature_b64,
        COSIGN_CERTIFICATE_ANNOTATION: certificate,
        COSIGN_CERTIFICATE_ANNOTATION_LEGACY: certificate,
        COSIGN_BUNDLE_ANNOTATION: "",
    }
    layer = _descriptor(
        media_type=COSIGN_PAYLOAD_MEDIA_TYPE,
        digest=payload_digest,
        size=len(payload),
        annotations=annotations,
    )
    config = _descriptor(
        media_type=COSIGN_EMPTY_CONFIG_MEDIA_TYPE,
        digest=config_digest,
        size=len(EMPTY_JSON),
    )
    legacy_manifest = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "config": config,
            "layers": [layer],
        }
    )
    subject = _descriptor(
        media_type=manifest_media_type,
        digest=manifest_digest,
        size=manifest_size,
    )
    referrer_manifest = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "artifactType": COSIGN_ARTIFACT_TYPE,
            "config": config,
            "subject": subject,
            "layers": [layer],
        }
    )
    referrer_descriptor = _descriptor(
        media_type=OCI_MANIFEST_MEDIA_TYPE,
        digest=_digest(referrer_manifest),
        size=len(referrer_manifest),
        artifact_type=COSIGN_ARTIFACT_TYPE,
    )
    referrers_index = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
            "manifests": [referrer_descriptor],
        }
    )
    return CosignArtifacts(
        payload=payload,
        signature=signature,
        config=EMPTY_JSON,
        legacy_manifest=legacy_manifest,
        referrer_manifest=referrer_manifest,
        referrers_index=referrers_index,
        legacy_manifest_digest=_digest(legacy_manifest),
        referrer_manifest_digest=_digest(referrer_manifest),
        payload_digest=payload_digest,
        config_digest=config_digest,
    )


def upload_cosign_artifacts(
    client: Any,
    bucket: str,
    repo: str,
    subject_digest: str,
    artifacts: CosignArtifacts,
) -> None:
    subject_hex = subject_digest.split(":", 1)[1]
    referrers_key = f"v2/{repo}/referrers/{subject_digest}"
    referrers_index = _merged_referrers_index(
        client,
        bucket,
        referrers_key,
        artifacts,
    )
    objects = (
        (
            f"v2/{repo}/blobs/{artifacts.config_digest}",
            artifacts.config,
            COSIGN_EMPTY_CONFIG_MEDIA_TYPE,
            REGISTRY_IMMUTABLE_CACHE_CONTROL,
        ),
        (
            f"v2/{repo}/blobs/{artifacts.payload_digest}",
            artifacts.payload,
            COSIGN_PAYLOAD_MEDIA_TYPE,
            REGISTRY_IMMUTABLE_CACHE_CONTROL,
        ),
        (
            f"v2/{repo}/manifests/{artifacts.referrer_manifest_digest}",
            artifacts.referrer_manifest,
            OCI_MANIFEST_MEDIA_TYPE,
            REGISTRY_IMMUTABLE_CACHE_CONTROL,
        ),
        (
            f"v2/{repo}/manifests/sha256-{subject_hex}.sig",
            artifacts.legacy_manifest,
            OCI_MANIFEST_MEDIA_TYPE,
            REGISTRY_SHORT_CACHE_CONTROL,
        ),
        (
            referrers_key,
            referrers_index,
            OCI_IMAGE_INDEX_MEDIA_TYPE,
            REGISTRY_SHORT_CACHE_CONTROL,
        ),
    )
    for key, body, content_type, cache_control in objects:
        log(f"Uploading cosign artifact: {key}")
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                CacheControl=cache_control,
            )
        except Exception as exc:
            raise ConfigError(f"S3 upload failed for {key}: {exc}") from exc


def verify_cosign_signature(
    *,
    image: str,
    config: CosignSigningConfig,
) -> None:
    if shutil.which("cosign") is None:
        warning("cosign is not installed; skipping cosign verification")
        return
    _verify_certificate_identity(config.cert_path, config.identity)
    _verify_certificate_chain(config.cert_path, config.root_path)
    with tempfile.TemporaryDirectory(prefix="ludos-cosign-verify-") as tmp:
        key_path = Path(tmp) / "cosign.pub"
        _write_certificate_public_key(config.cert_path, key_path)
        base = [
            "cosign",
            "verify",
            "--key",
            str(key_path),
            "--insecure-ignore-tlog",
        ]
        commands = (
            [
                [*base, "--registry-referrers-mode=legacy", image],
                [*base, "--registry-referrers-mode=oci-1-1", image],
            ]
            if _cosign_verify_supports("--registry-referrers-mode")
            else [[*base, image]]
        )
        for command in commands:
            try:
                _run_quiet_verify(command)
            except FileNotFoundError as exc:
                raise ConfigError("cosign must be installed to verify signatures") from exc
            except subprocess.CalledProcessError as exc:
                raise ConfigError(
                    f"cosign verification failed with exit code {exc.returncode}"
                ) from exc


def image_digest_reference(registry: str, repo: str, digest: str) -> str:
    return f"{cosign_docker_reference(registry, repo)}@{digest}"


def _run_quiet_verify(command: list[str]) -> None:
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode == 0:
        return
    if result.stdout:
        for line in result.stdout.splitlines():
            log(line)
    raise subprocess.CalledProcessError(result.returncode, command)


def _verify_certificate_identity(cert_path: Path, identity: str) -> None:
    try:
        certificate = ssl._ssl._test_decode_cert(str(cert_path))  # type: ignore[attr-defined]
    except Exception as exc:
        raise ConfigError(f"{cert_path}: failed to read certificate identity") from exc
    names = [
        value
        for name_type, value in certificate.get("subjectAltName", ())
        if name_type == "DNS"
    ]
    if not names:
        for subject in certificate.get("subject", ()):
            for key, value in subject:
                if key == "commonName":
                    names.append(value)
    if not any(_matches_dns_identity(name, identity) for name in names):
        raise ConfigError(f"{cert_path}: certificate is not valid for {identity}")


def _verify_certificate_chain(cert_path: Path, root_path: Path) -> None:
    command = ["openssl", "verify", "-CAfile", str(root_path), str(cert_path)]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ConfigError("openssl must be installed to verify cosign certificates") from exc
    if result.returncode != 0:
        detail = result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ConfigError(f"{cert_path}: certificate chain verification failed{suffix}")


def _write_certificate_public_key(cert_path: Path, output_path: Path) -> None:
    command = ["openssl", "x509", "-in", str(cert_path), "-pubkey", "-noout"]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ConfigError("openssl must be installed to verify cosign signatures") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise ConfigError(f"{cert_path}: failed to extract public key{suffix}")
    output_path.write_bytes(result.stdout)


def _matches_dns_identity(pattern: str, identity: str) -> bool:
    pattern = pattern.lower()
    identity = identity.lower()
    if pattern == identity:
        return True
    if not pattern.startswith("*."):
        return False
    suffix = pattern[1:]
    if not identity.endswith(suffix):
        return False
    prefix = identity[: -len(suffix)]
    return bool(prefix) and "." not in prefix


def _cosign_verify_supports(flag: str) -> bool:
    try:
        result = subprocess.run(
            ["cosign", "verify", "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        return True
    return flag in result.stdout


def _merged_referrers_index(
    client: Any,
    bucket: str,
    key: str,
    artifacts: CosignArtifacts,
) -> bytes:
    new_index = json.loads(artifacts.referrers_index.decode("utf-8"))
    new_manifests = new_index.get("manifests")
    if not isinstance(new_manifests, list) or len(new_manifests) != 1:
        raise ConfigError("generated cosign referrers index is invalid")
    new_descriptor = new_manifests[0]

    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _client_error_code(exc) not in ("404", "NoSuchKey", "NotFound"):
            raise ConfigError(f"S3 download failed for {key}: {exc}") from exc
        manifests: list[object] = []
    else:
        body = response.get("Body")
        data = b"" if body is None else body.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        existing = json.loads(data.decode("utf-8"))
        manifests = existing.get("manifests")
        if not isinstance(manifests, list):
            raise ConfigError(f"cosign referrers index {key} must contain manifests")

    digest = artifacts.referrer_manifest_digest
    merged = [
        item
        for item in manifests
        if not isinstance(item, dict) or item.get("digest") != digest
    ]
    merged.append(new_descriptor)
    return _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": OCI_IMAGE_INDEX_MEDIA_TYPE,
            "manifests": merged,
        }
    )


def _sign_payload(payload: bytes, config: CosignSigningConfig) -> bytes:
    if config.signer is not None:
        return config.signer(payload, config)
    key = parse_gcloud_key_uri(config.key_uri, env_name="LUDOS_COSIGN_KEY")
    return gcloud_sign(payload, key, digest_algorithm="sha256")


def _project_path(path: Path, project_root: Path | None) -> Path:
    if path.is_absolute():
        return path
    root = Path.cwd() if project_root is None else Path(project_root)
    return root / path


def _descriptor(
    *,
    media_type: str,
    digest: str,
    size: int,
    annotations: dict[str, str] | None = None,
    artifact_type: str | None = None,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "mediaType": media_type,
        "digest": digest,
        "size": size,
    }
    if artifact_type is not None:
        descriptor["artifactType"] = artifact_type
    if annotations is not None:
        descriptor["annotations"] = annotations
    return descriptor


def _json_bytes(data: object) -> bytes:
    return (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"

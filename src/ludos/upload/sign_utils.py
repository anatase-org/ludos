from __future__ import annotations

import base64
import binascii
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..logging import stream
from ..model import ConfigError


@dataclass(frozen=True)
class GcloudKey:
    project: str
    location: str
    keyring: str
    key: str
    version: str


def parse_gcloud_key_uri(value: str, *, env_name: str) -> GcloudKey:
    if value.startswith("gcloud://"):
        parts = value.removeprefix("gcloud://").strip("/").split("/")
        version_label = "cryptoKeyVersions"
    elif value.startswith("gcpkms://"):
        parts = value.removeprefix("gcpkms://").strip("/").split("/")
        version_label = "versions"
    else:
        raise ConfigError(
            f"only gcloud:// and gcpkms:// {env_name} values are supported"
        )
    expected = ("projects", "locations", "keyRings", "cryptoKeys")
    if len(parts) != 10:
        raise ConfigError(f"invalid Google Cloud KMS key path in {env_name}")
    for index, label in enumerate(expected):
        if parts[index * 2] != label or not parts[index * 2 + 1]:
            raise ConfigError(f"invalid Google Cloud KMS key path in {env_name}")
    if parts[8] != version_label or not parts[9]:
        raise ConfigError(f"invalid Google Cloud KMS key version in {env_name}")
    return GcloudKey(
        project=parts[1],
        location=parts[3],
        keyring=parts[5],
        key=parts[7],
        version=parts[9],
    )


def gcloud_sign(
    data: bytes,
    key: GcloudKey,
    *,
    digest_algorithm: str | None = None,
    expected_length: int | None = None,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="ludos-kms-sign-") as tmp:
        input_path = Path(tmp) / "input"
        signature_path = Path(tmp) / "signature"
        input_path.write_bytes(data)
        command = [
            "gcloud",
            "kms",
            "asymmetric-sign",
            f"--project={key.project}",
            f"--location={key.location}",
            f"--keyring={key.keyring}",
            f"--key={key.key}",
            f"--input-file={input_path}",
            f"--signature-file={signature_path}",
            f"--version={key.version}",
        ]
        if digest_algorithm is not None:
            command.append(f"--digest-algorithm={digest_algorithm}")
        try:
            run_streamed_command(command)
        except FileNotFoundError as exc:
            raise ConfigError(f"gcloud must be installed to sign with {key.key}") from exc
        except subprocess.CalledProcessError as exc:
            raise ConfigError(
                f"gcloud KMS signing failed with exit code {exc.returncode}"
            ) from exc
        return decode_gcloud_signature(
            signature_path.read_bytes(),
            expected_length=expected_length,
        )


def decode_gcloud_signature(
    data: bytes,
    *,
    expected_length: int | None = None,
) -> bytes:
    stripped = data.strip()
    if expected_length is not None and len(stripped) == expected_length:
        return stripped
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except binascii.Error:
        return data
    if expected_length is not None and len(decoded) != expected_length:
        return data
    return decoded or data


def run_streamed_command(command: list[str]) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            stream(line)
        returncode = process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, command)

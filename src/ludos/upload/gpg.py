from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..logging import stream
from ..model import ConfigError


RSA_ALGORITHM = 1
SHA256_ALGORITHM = 8
SIGNATURE_TYPE_BINARY = 0x00
SIGNATURE_VERSION = 4
ONE_PASS_SIGNATURE_VERSION = 3
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
RSA_SIGN_RAW_PKCS1_4096_BYTES = 512
_CERT_SELECTOR_RE = re.compile(r"^(?P<path>.+):s(?P<index>[0-9]+)$")


@dataclass(frozen=True)
class PublicKey:
    packet: bytes
    created: int
    algorithm: int
    fingerprint: bytes
    key_id: bytes

    @property
    def fingerprint_hex(self) -> str:
        return self.fingerprint.hex().upper()


@dataclass(frozen=True)
class GcloudKey:
    project: str
    location: str
    keyring: str
    key: str
    version: str


@dataclass(frozen=True)
class GpgSigningConfig:
    cert_path: Path
    key_uri: str
    public_key: PublicKey
    gcloud_key: GcloudKey
    signer: Callable[[bytes, "GpgSigningConfig"], bytes] | None = None


def sign_file(
    input_path: Path,
    output_path: Path,
    *,
    verify: bool = False,
    project_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    config = config_from_env(environ, project_root=project_root)
    data = Path(input_path).read_bytes()
    signed = sign_attached_data(data, config)
    Path(output_path).write_bytes(signed)
    if verify:
        _verify_attached(
            Path(output_path),
            config.cert_path,
            _gpg_verify_home(project_root),
        )
    return 0


def sign_detached(
    input_path: Path,
    *,
    verify: bool = False,
    project_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    config = config_from_env(environ, project_root=project_root)
    input_file = Path(input_path)
    output_path = Path(f"{input_file}.sig")
    sign_detached_file(input_file, output_path, config)
    if verify:
        _verify_detached(
            input_file,
            output_path,
            config.cert_path,
            _gpg_verify_home(project_root),
        )
    return 0


def sign_attached_data(data: bytes, config: GpgSigningConfig) -> bytes:
    digest = hashlib.sha256()
    digest.update(data)
    signature = _make_signature_from_digest(digest, config)
    one_pass = _one_pass_signature_packet(config.public_key)
    literal = _literal_data_packet(data)
    return one_pass + literal + signature


def sign_detached_file(
    input_path: Path,
    output_path: Path,
    config: GpgSigningConfig,
) -> None:
    digest = hashlib.sha256()
    with Path(input_path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    Path(output_path).write_bytes(_make_signature_from_digest(digest, config))


def verify_attached_data(
    data: bytes,
    cert_path: Path,
    *,
    project_root: Path | None = None,
) -> None:
    homedir = _gpg_verify_home(project_root)
    verify_dir = homedir / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="attached-",
        suffix=".gpg",
        dir=verify_dir,
        delete=False,
    ) as handle:
        handle.write(data)
        output_path = Path(handle.name)
    try:
        _verify_attached(output_path, cert_path, homedir)
    finally:
        output_path.unlink(missing_ok=True)


def cert_path_from_spec(
    cert_spec: str,
    *,
    project_root: Path | None = None,
) -> Path:
    cert_path, _selector = _parse_cert_spec(cert_spec, project_root)
    return cert_path


def config_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
) -> GpgSigningConfig:
    env = os.environ if environ is None else environ
    cert_spec = env.get("LUDOS_GPG_CERT", "").strip()
    key_uri = env.get("LUDOS_GPG_KEY", "").strip()
    if not cert_spec:
        raise ConfigError("LUDOS_GPG_CERT is required")
    if not key_uri:
        raise ConfigError("LUDOS_GPG_KEY is required")
    cert_path, selector = _parse_cert_spec(cert_spec, project_root)
    public_key = _select_public_key(cert_path, selector)
    if public_key.algorithm != RSA_ALGORITHM:
        raise ConfigError("only RSA OpenPGP signing keys are supported")
    return GpgSigningConfig(
        cert_path=cert_path,
        key_uri=key_uri,
        public_key=public_key,
        gcloud_key=_parse_gcloud_key_uri(key_uri),
    )


def _make_signature_from_digest(data_digest: Any, config: GpgSigningConfig) -> bytes:
    created = int(time.time())
    hashed_subpackets = b"".join(
        (
            _signature_subpacket(2, created.to_bytes(4, "big")),
            _signature_subpacket(33, b"\x04" + config.public_key.fingerprint),
        )
    )
    unhashed_subpackets = _signature_subpacket(16, config.public_key.key_id)
    prefix = b"".join(
        (
            bytes(
                (
                    SIGNATURE_VERSION,
                    SIGNATURE_TYPE_BINARY,
                    RSA_ALGORITHM,
                    SHA256_ALGORITHM,
                )
            ),
            len(hashed_subpackets).to_bytes(2, "big"),
            hashed_subpackets,
        )
    )
    digest = data_digest.copy()
    digest.update(prefix)
    digest.update(_signature_trailer(prefix))
    final_digest = digest.digest()
    signature = _sign_digest_info(SHA256_DIGEST_INFO_PREFIX + final_digest, config)
    body = b"".join(
        (
            prefix,
            len(unhashed_subpackets).to_bytes(2, "big"),
            unhashed_subpackets,
            final_digest[:2],
            _mpi_from_bytes(signature),
        )
    )
    return _packet(2, body)


def _sign_digest_info(digest_info: bytes, config: GpgSigningConfig) -> bytes:
    signer = config.signer or _gcloud_sign_digest_info
    signature = signer(digest_info, config)
    if len(signature) != RSA_SIGN_RAW_PKCS1_4096_BYTES:
        raise ConfigError(
            "gcloud returned an unsupported RSA signature length: "
            f"{len(signature)} bytes"
        )
    return signature


def _gcloud_sign_digest_info(digest_info: bytes, config: GpgSigningConfig) -> bytes:
    key = config.gcloud_key
    with tempfile.TemporaryDirectory(prefix="ludos-gpg-sign-") as tmp:
        input_path = Path(tmp) / "digest-info"
        signature_path = Path(tmp) / "signature"
        input_path.write_bytes(digest_info)
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
        try:
            _run_streamed_command(command)
        except FileNotFoundError as exc:
            raise ConfigError("gcloud must be installed to sign with LUDOS_GPG_KEY") from exc
        except subprocess.CalledProcessError as exc:
            raise ConfigError(f"gcloud KMS signing failed with exit code {exc.returncode}") from exc
        return _decode_gcloud_signature(signature_path.read_bytes())


def _decode_gcloud_signature(data: bytes) -> bytes:
    if len(data) == RSA_SIGN_RAW_PKCS1_4096_BYTES:
        return data
    try:
        decoded = base64.b64decode(data.strip(), validate=True)
    except binascii.Error:
        return data
    if len(decoded) == RSA_SIGN_RAW_PKCS1_4096_BYTES:
        return decoded
    return data


def _parse_cert_spec(value: str, project_root: Path | None) -> tuple[Path, int | None]:
    match = _CERT_SELECTOR_RE.match(value)
    if match is None:
        return _project_path(Path(value), project_root), None
    index = int(match.group("index"))
    if index < 1:
        raise ConfigError("LUDOS_GPG_CERT subkey selector must be one-based")
    return _project_path(Path(match.group("path")), project_root), index


def _project_path(path: Path, project_root: Path | None) -> Path:
    if path.is_absolute():
        return path
    root = Path.cwd() if project_root is None else Path(project_root)
    return root / path


def _select_public_key(path: Path, selector: int | None) -> PublicKey:
    keys = _read_public_keys(path)
    if not keys:
        raise ConfigError(f"{path}: no OpenPGP public keys found")
    if selector is None:
        return keys[0]
    subkeys = [key for key in keys[1:] if key.algorithm == RSA_ALGORITHM]
    if selector > len(subkeys):
        raise ConfigError(f"{path}: signing subkey s{selector} was not found")
    return subkeys[selector - 1]


def _read_public_keys(path: Path) -> list[PublicKey]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"failed to read LUDOS_GPG_CERT {path}: {exc}") from exc
    decoded = _dearmor_public_key(data)
    packets = list(_iter_packets(decoded))
    keys: list[PublicKey] = []
    signing_subkeys: set[int] = set()
    pending_subkey_index: int | None = None
    for tag, body in packets:
        if tag == 6:
            keys.append(_parse_public_key_packet(body))
            pending_subkey_index = None
        elif tag == 14:
            keys.append(_parse_public_key_packet(body))
            pending_subkey_index = len(keys) - 1
        elif tag == 2 and pending_subkey_index is not None:
            if _signature_key_flags(body) & 0x02:
                signing_subkeys.add(pending_subkey_index)
            pending_subkey_index = None
        elif tag not in (13, 17):
            pending_subkey_index = None
    if len(keys) <= 1:
        return keys
    return [keys[0], *(keys[index] for index in sorted(signing_subkeys))]


def _dearmor_public_key(data: bytes) -> bytes:
    if not data.startswith(b"-----BEGIN PGP "):
        return data
    lines = data.decode("ascii").splitlines()
    in_body = False
    body: list[str] = []
    for line in lines:
        if line.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
            in_body = True
            continue
        if line.startswith("-----END PGP PUBLIC KEY BLOCK-----"):
            break
        if not in_body or not line or ":" in line or line.startswith("="):
            continue
        body.append(line.strip())
    if not body:
        raise ConfigError("LUDOS_GPG_CERT does not contain an armored public key")
    try:
        return base64.b64decode("".join(body), validate=True)
    except binascii.Error as exc:
        raise ConfigError("LUDOS_GPG_CERT contains invalid ASCII armor") from exc


def _parse_public_key_packet(body: bytes) -> PublicKey:
    if len(body) < 6 or body[0] != 4:
        raise ConfigError("only OpenPGP v4 public keys are supported")
    algorithm = body[5]
    if algorithm != RSA_ALGORITHM:
        raise ConfigError("only RSA OpenPGP public keys are supported")
    _offset, _n = _read_mpi(body, 6)
    _offset, _e = _read_mpi(body, _offset)
    fingerprint = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).digest()
    return PublicKey(
        packet=body,
        created=int.from_bytes(body[1:5], "big"),
        algorithm=algorithm,
        fingerprint=fingerprint,
        key_id=fingerprint[-8:],
    )


def _signature_key_flags(body: bytes) -> int:
    if len(body) < 6 or body[0] != 4:
        return 0
    hashed_len = int.from_bytes(body[4:6], "big")
    hashed = body[6 : 6 + hashed_len]
    offset = 6 + hashed_len
    if len(body) >= offset + 2:
        unhashed_len = int.from_bytes(body[offset : offset + 2], "big")
        unhashed = body[offset + 2 : offset + 2 + unhashed_len]
    else:
        unhashed = b""
    flags = 0
    for packet in (_iter_signature_subpackets(hashed), _iter_signature_subpackets(unhashed)):
        for subpacket_type, value in packet:
            if subpacket_type == 27 and value:
                flags |= value[0]
    return flags


def _iter_signature_subpackets(data: bytes) -> Iterable[tuple[int, bytes]]:
    offset = 0
    while offset < len(data):
        length, offset = _read_subpacket_length(data, offset)
        if length == 0 or offset + length > len(data):
            break
        subpacket_type = data[offset] & 0x7F
        yield subpacket_type, data[offset + 1 : offset + length]
        offset += length


def _read_subpacket_length(data: bytes, offset: int) -> tuple[int, int]:
    first = data[offset]
    offset += 1
    if first < 192:
        return first, offset
    if first < 255:
        if offset >= len(data):
            return 0, offset
        second = data[offset]
        offset += 1
        return ((first - 192) << 8) + second + 192, offset
    if offset + 4 > len(data):
        return 0, offset
    return int.from_bytes(data[offset : offset + 4], "big"), offset + 4


def _parse_gcloud_key_uri(value: str) -> GcloudKey:
    if not value.startswith("gcloud://"):
        raise ConfigError("only gcloud:// LUDOS_GPG_KEY values are supported")
    parts = value.removeprefix("gcloud://").strip("/").split("/")
    expected = ("projects", "locations", "keyRings", "cryptoKeys")
    if len(parts) != 10:
        raise ConfigError("invalid gcloud KMS key path in LUDOS_GPG_KEY")
    for index, label in enumerate(expected):
        if parts[index * 2] != label or not parts[index * 2 + 1]:
            raise ConfigError("invalid gcloud KMS key path in LUDOS_GPG_KEY")
    if parts[8] != "cryptoKeyVersions" or not parts[9]:
        raise ConfigError("invalid gcloud KMS key version in LUDOS_GPG_KEY")
    return GcloudKey(
        project=parts[1],
        location=parts[3],
        keyring=parts[5],
        key=parts[7],
        version=parts[9],
    )


def _packet(tag: int, body: bytes) -> bytes:
    return bytes((0xC0 | tag,)) + _packet_length(len(body)) + body


def _packet_length(length: int) -> bytes:
    if length < 192:
        return bytes((length,))
    if length <= 8383:
        length -= 192
        return bytes(((length >> 8) + 192, length & 0xFF))
    return b"\xff" + length.to_bytes(4, "big")


def _one_pass_signature_packet(key: PublicKey) -> bytes:
    body = bytes(
        (
            ONE_PASS_SIGNATURE_VERSION,
            SIGNATURE_TYPE_BINARY,
            SHA256_ALGORITHM,
            RSA_ALGORITHM,
        )
    )
    body += key.key_id
    body += b"\x01"
    return _packet(4, body)


def _literal_data_packet(data: bytes) -> bytes:
    body = b"b\x00" + (0).to_bytes(4, "big") + data
    return _packet(11, body)


def _signature_subpacket(subpacket_type: int, payload: bytes) -> bytes:
    body = bytes((subpacket_type,)) + payload
    return _packet_length(len(body)) + body


def _signature_trailer(prefix: bytes) -> bytes:
    return b"\x04\xff" + len(prefix).to_bytes(4, "big")


def _mpi_from_bytes(data: bytes) -> bytes:
    stripped = data.lstrip(b"\x00") or b"\x00"
    bit_length = int.from_bytes(stripped, "big").bit_length()
    return bit_length.to_bytes(2, "big") + stripped


def _read_mpi(data: bytes, offset: int) -> tuple[int, bytes]:
    if offset + 2 > len(data):
        raise ConfigError("truncated OpenPGP MPI")
    bit_length = int.from_bytes(data[offset : offset + 2], "big")
    offset += 2
    byte_length = (bit_length + 7) // 8
    if offset + byte_length > len(data):
        raise ConfigError("truncated OpenPGP MPI")
    return offset + byte_length, data[offset : offset + byte_length]


def _iter_packets(data: bytes) -> Iterable[tuple[int, bytes]]:
    offset = 0
    while offset < len(data):
        ctb = data[offset]
        offset += 1
        if not ctb & 0x80:
            raise ConfigError("invalid OpenPGP packet header")
        if ctb & 0x40:
            tag = ctb & 0x3F
            length, offset = _read_new_packet_length(data, offset)
        else:
            tag = (ctb >> 2) & 0x0F
            length_type = ctb & 0x03
            length, offset = _read_old_packet_length(data, offset, length_type)
        if offset + length > len(data):
            raise ConfigError("truncated OpenPGP packet")
        yield tag, data[offset : offset + length]
        offset += length


def _read_new_packet_length(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise ConfigError("truncated OpenPGP packet length")
    first = data[offset]
    offset += 1
    if first < 192:
        return first, offset
    if first < 224:
        if offset >= len(data):
            raise ConfigError("truncated OpenPGP packet length")
        second = data[offset]
        offset += 1
        return ((first - 192) << 8) + second + 192, offset
    if first == 255:
        if offset + 4 > len(data):
            raise ConfigError("truncated OpenPGP packet length")
        return int.from_bytes(data[offset : offset + 4], "big"), offset + 4
    raise ConfigError("partial OpenPGP packet lengths are not supported")


def _read_old_packet_length(data: bytes, offset: int, length_type: int) -> tuple[int, int]:
    if length_type == 0:
        if offset >= len(data):
            raise ConfigError("truncated OpenPGP packet length")
        return data[offset], offset + 1
    if length_type == 1:
        if offset + 2 > len(data):
            raise ConfigError("truncated OpenPGP packet length")
        return int.from_bytes(data[offset : offset + 2], "big"), offset + 2
    if length_type == 2:
        if offset + 4 > len(data):
            raise ConfigError("truncated OpenPGP packet length")
        return int.from_bytes(data[offset : offset + 4], "big"), offset + 4
    raise ConfigError("indeterminate OpenPGP packet lengths are not supported")


def _gpg_verify_home(project_root: Path | None) -> Path:
    root = Path.cwd() if project_root is None else Path(project_root)
    return root / "cache" / "gpg"


def _verify_attached(output_path: Path, cert_path: Path, homedir: Path) -> None:
    _import_verify_cert(cert_path, homedir)
    _run_gpg(["gpg", "--homedir", str(homedir), "--batch", "--verify", str(output_path)])


def _verify_detached(
    input_path: Path,
    output_path: Path,
    cert_path: Path,
    homedir: Path,
) -> None:
    _import_verify_cert(cert_path, homedir)
    _run_gpg(
        [
            "gpg",
            "--homedir",
            str(homedir),
            "--batch",
            "--verify",
            str(output_path),
            str(input_path),
        ]
    )


def _import_verify_cert(cert_path: Path, homedir: Path) -> None:
    homedir.mkdir(parents=True, exist_ok=True)
    homedir.chmod(0o700)
    _run_gpg(["gpg", "--homedir", str(homedir), "--batch", "--import", str(cert_path)])


def _run_gpg(command: list[str]) -> None:
    try:
        _run_streamed_command(command)
    except FileNotFoundError as exc:
        raise ConfigError("gpg must be installed to verify signatures") from exc
    except subprocess.CalledProcessError as exc:
        raise ConfigError(f"gpg verification failed with exit code {exc.returncode}") from exc


def _run_streamed_command(command: list[str]) -> None:
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

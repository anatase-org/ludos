from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from ludos.model import ConfigError, OciCosignConfig
from ludos.upload import cosign
from ludos.upload.common import (
    REGISTRY_IMMUTABLE_CACHE_CONTROL,
    REGISTRY_SHORT_CACHE_CONTROL,
)
from ludos.upload.cosign import (
    COSIGN_CERTIFICATE_ANNOTATION,
    COSIGN_PAYLOAD_MEDIA_TYPE,
    COSIGN_SIGNATURE_ANNOTATION,
    config_from_env,
)
from ludos.upload.sign_utils import parse_gcloud_key_uri


KEY_URI = (
    "gcpkms://projects/example/locations/global/keyRings/cosign/"
    "cryptoKeys/test/versions/9"
)
GCLOUD_URI = (
    "gcloud://projects/example/locations/global/keyRings/cosign/"
    "cryptoKeys/test/cryptoKeyVersions/9"
)
ROOT = Path(__file__).resolve().parents[2]


class UploadCosignTests(unittest.TestCase):
    def test_config_requires_environment(self) -> None:
        config = _cosign_config()

        with self.assertRaisesRegex(ConfigError, "LUDOS_COSIGN_KEY"):
            config_from_env(config, environ={})

        with self.assertRaisesRegex(ConfigError, "LUDOS_COSIGN_CERT"):
            config_from_env(config, environ={"LUDOS_COSIGN_KEY": KEY_URI})

    def test_config_requires_leaf_certificate_identity(self) -> None:
        config = OciCosignConfig(
            registry="https://flatpaks.example.test/",
            identity="cosign.anatase.org",
            verify="cards/base/atomic/keys/anatase-cosign-root.pem",
        )

        leaf = config_from_env(
            config,
            project_root=ROOT,
            environ={
                "LUDOS_COSIGN_KEY": KEY_URI,
                "LUDOS_COSIGN_CERT": "cards/base/atomic/keys/anatase-c002.pem",
            },
        )
        self.assertEqual(
            leaf.cert_path,
            ROOT / "cards/base/atomic/keys/anatase-c002.pem",
        )

        with self.assertRaisesRegex(ConfigError, "LUDOS_COSIGN_CERT.*leaf"):
            config_from_env(
                config,
                project_root=ROOT,
                environ={
                    "LUDOS_COSIGN_KEY": KEY_URI,
                    "LUDOS_COSIGN_CERT": (
                        "cards/base/atomic/keys/anatase-cosign-root.pem"
                    ),
                },
            )

    def test_gcpkms_and_gcloud_key_paths_parse(self) -> None:
        for uri in (KEY_URI, GCLOUD_URI):
            key = parse_gcloud_key_uri(uri, env_name="LUDOS_COSIGN_KEY")
            self.assertEqual(key.project, "example")
            self.assertEqual(key.location, "global")
            self.assertEqual(key.keyring, "cosign")
            self.assertEqual(key.key, "test")
            self.assertEqual(key.version, "9")

    def test_payload_is_deterministic_simple_signing_json(self) -> None:
        payload = cosign.cosign_payload(
            registry="https://flatpaks.example.test/",
            repo="images/anatase",
            manifest_digest="sha256:" + "a" * 64,
        )

        self.assertEqual(payload[-1:], b"\n")
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(
            data["critical"]["type"],
            "cosign container image signature",
        )
        self.assertEqual(
            data["critical"]["image"]["docker-manifest-digest"],
            "sha256:" + "a" * 64,
        )
        self.assertEqual(
            data["critical"]["identity"]["docker-reference"],
            "flatpaks.example.test/images/anatase",
        )
        self.assertEqual(data["optional"], {})

    def test_artifacts_include_signature_and_certificate_annotations(self) -> None:
        artifacts = cosign.build_cosign_artifacts(
            payload=b"payload\n",
            signature=b"signature",
            certificate="leaf cert\n",
            manifest_digest="sha256:" + "a" * 64,
            manifest_media_type="application/vnd.oci.image.manifest.v1+json",
            manifest_size=123,
        )

        legacy = json.loads(artifacts.legacy_manifest.decode("utf-8"))
        referrer = json.loads(artifacts.referrer_manifest.decode("utf-8"))
        index = json.loads(artifacts.referrers_index.decode("utf-8"))
        layer = legacy["layers"][0]
        annotations = layer["annotations"]

        self.assertEqual(layer["mediaType"], COSIGN_PAYLOAD_MEDIA_TYPE)
        self.assertEqual(
            annotations[COSIGN_SIGNATURE_ANNOTATION],
            base64.b64encode(b"signature").decode("ascii"),
        )
        self.assertEqual(annotations[COSIGN_CERTIFICATE_ANNOTATION], "leaf cert\n")
        self.assertNotIn("subject", legacy)
        self.assertEqual(referrer["subject"]["digest"], "sha256:" + "a" * 64)
        self.assertEqual(
            index["manifests"][0]["digest"],
            artifacts.referrer_manifest_digest,
        )

    def test_verify_calls_cosign_for_legacy_and_referrers_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cert = root / "leaf.pem"
            ca = root / "root.pem"
            config = cosign.CosignSigningConfig(
                key_uri=KEY_URI,
                cert_path=cert,
                root_path=ca,
                registry="https://flatpaks.example.test/",
                identity="cosign.example.test",
            )

            with (
                patch("ludos.upload.cosign.shutil.which", return_value="/usr/bin/cosign"),
                patch("ludos.upload.cosign._run_quiet_verify") as run,
                patch(
                    "ludos.upload.cosign._cosign_verify_supports",
                    return_value=True,
                ),
                patch("ludos.upload.cosign._verify_certificate_identity") as identity,
                patch("ludos.upload.cosign._verify_certificate_chain") as chain,
                patch("ludos.upload.cosign._write_certificate_public_key") as pubkey,
            ):
                cosign.verify_cosign_signature(
                    image="flatpaks.example.test/images/anatase@sha256:" + "a" * 64,
                    config=config,
                )
            identity.assert_called_once_with(cert, "cosign.example.test")
            chain.assert_called_once_with(cert, ca)
            pubkey.assert_called_once()
            self.assertEqual(pubkey.call_args.args[0], cert)
            key_path = pubkey.call_args.args[1]

        base = [
            "cosign",
            "verify",
            "--key",
            str(key_path),
            "--insecure-ignore-tlog",
        ]
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    [
                        *base,
                        "--registry-referrers-mode=legacy",
                        "flatpaks.example.test/images/anatase@sha256:" + "a" * 64,
                    ]
                ),
                call(
                    [
                        *base,
                        "--registry-referrers-mode=oci-1-1",
                        "flatpaks.example.test/images/anatase@sha256:" + "a" * 64,
                    ]
                ),
            ],
        )

    def test_verify_warns_and_skips_when_cosign_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cert = root / "leaf.pem"
            ca = root / "root.pem"
            config = cosign.CosignSigningConfig(
                key_uri=KEY_URI,
                cert_path=cert,
                root_path=ca,
                registry="https://flatpaks.example.test/",
                identity="cosign.example.test",
            )

            with (
                patch("ludos.upload.cosign.shutil.which", return_value=None),
                patch("ludos.upload.cosign.warning") as warning,
                patch("ludos.upload.cosign._run_quiet_verify") as run,
                patch("ludos.upload.cosign._verify_certificate_identity") as identity,
                patch("ludos.upload.cosign._verify_certificate_chain") as chain,
            ):
                cosign.verify_cosign_signature(
                    image="flatpaks.example.test/images/anatase@sha256:" + "a" * 64,
                    config=config,
                )

        warning.assert_called_once_with(
            "cosign is not installed; skipping cosign verification"
        )
        run.assert_not_called()
        identity.assert_not_called()
        chain.assert_not_called()

    def test_verify_falls_back_for_cosign_without_referrers_mode_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cert = root / "leaf.pem"
            ca = root / "root.pem"
            config = cosign.CosignSigningConfig(
                key_uri=KEY_URI,
                cert_path=cert,
                root_path=ca,
                registry="https://flatpaks.example.test/",
                identity="cosign.example.test",
            )

            with (
                patch("ludos.upload.cosign.shutil.which", return_value="/usr/bin/cosign"),
                patch("ludos.upload.cosign._run_quiet_verify") as run,
                patch(
                    "ludos.upload.cosign._cosign_verify_supports",
                    return_value=False,
                ),
                patch("ludos.upload.cosign._verify_certificate_identity"),
                patch("ludos.upload.cosign._verify_certificate_chain"),
                patch("ludos.upload.cosign._write_certificate_public_key"),
            ):
                cosign.verify_cosign_signature(
                    image="flatpaks.example.test/images/anatase@sha256:" + "a" * 64,
                    config=config,
                )

        command = run.call_args.args[0]
        self.assertNotIn("--registry-referrers-mode=legacy", command)
        self.assertNotIn("--registry-referrers-mode=oci-1-1", command)
        self.assertEqual(
            command[-1],
            "flatpaks.example.test/images/anatase@sha256:" + "a" * 64,
        )

    def test_verify_certificate_identity_rejects_wrong_leaf_cert(self) -> None:
        cert = ROOT / "cards/base/atomic/keys/anatase-c002.pem"

        with self.assertRaisesRegex(ConfigError, "not valid for wrong.example.test"):
            cosign._verify_certificate_identity(cert, "wrong.example.test")

    def test_verify_certificate_chain_accepts_leaf_signed_by_root(self) -> None:
        cert = ROOT / "cards/base/atomic/keys/anatase-c002.pem"
        root = ROOT / "cards/base/atomic/keys/anatase-cosign-root.pem"

        cosign._verify_certificate_chain(cert, root)

    def test_quiet_verify_suppresses_success_output(self) -> None:
        result = _CompletedProcess(0, "[{\"verified\":true}]\n")

        with (
            patch("ludos.upload.cosign.subprocess.run", return_value=result),
            patch("ludos.upload.cosign.log") as log,
        ):
            cosign._run_quiet_verify(["cosign", "verify", "image"])

        log.assert_not_called()

    def test_quiet_verify_logs_failure_output(self) -> None:
        result = _CompletedProcess(1, "bad signature\n")

        with (
            patch("ludos.upload.cosign.subprocess.run", return_value=result),
            patch("ludos.upload.cosign.log") as log,
            self.assertRaises(cosign.subprocess.CalledProcessError),
        ):
            cosign._run_quiet_verify(["cosign", "verify", "image"])

        log.assert_called_once_with("bad signature")

    def test_upload_merges_existing_referrers_index(self) -> None:
        artifacts = cosign.build_cosign_artifacts(
            payload=b"payload\n",
            signature=b"signature",
            certificate="leaf cert\n",
            manifest_digest="sha256:" + "a" * 64,
            manifest_media_type="application/vnd.oci.image.manifest.v1+json",
            manifest_size=123,
        )
        client = _FakeClient(
            {
                "v2/images/anatase/referrers/sha256:" + "a" * 64: json.dumps(
                    {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "manifests": [
                            {
                                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                "digest": "sha256:" + "b" * 64,
                                "size": 99,
                            }
                        ],
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            }
        )

        cosign.upload_cosign_artifacts(
            client,
            "bucket",
            "images/anatase",
            "sha256:" + "a" * 64,
            artifacts,
        )

        referrers = json.loads(
            client.objects["v2/images/anatase/referrers/sha256:" + "a" * 64].decode(
                "utf-8"
            )
        )
        self.assertEqual(
            [item["digest"] for item in referrers["manifests"]],
            ["sha256:" + "b" * 64, artifacts.referrer_manifest_digest],
        )
        cache_controls = {item["Key"]: item["CacheControl"] for item in client.puts}
        self.assertEqual(
            cache_controls[f"v2/images/anatase/blobs/{artifacts.payload_digest}"],
            REGISTRY_IMMUTABLE_CACHE_CONTROL,
        )
        self.assertEqual(
            cache_controls["v2/images/anatase/referrers/sha256:" + "a" * 64],
            REGISTRY_SHORT_CACHE_CONTROL,
        )


def _cosign_config() -> OciCosignConfig:
    return OciCosignConfig(
        registry="https://flatpaks.example.test/",
        identity="cosign.example.test",
        verify="root.pem",
    )


class _FakeClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = dict(objects)
        self.puts: list[dict[str, object]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        try:
            body = self.objects[Key]
        except KeyError as exc:
            raise _FakeClientError("NoSuchKey") from exc
        return {"Body": _Body(body)}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        CacheControl: str,
    ) -> None:
        self.puts.append(
            {
                "Bucket": Bucket,
                "Key": Key,
                "Body": Body,
                "ContentType": ContentType,
                "CacheControl": CacheControl,
            }
        )
        self.objects[Key] = Body


class _FakeClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _CompletedProcess:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

from ludos.__main__ import build_parser
from ludos.model import ConfigError
from ludos.upload.registry import (
    DEFAULT_CONFIG_MEDIA_TYPE,
    DEFAULT_LAYER_MEDIA_TYPE,
    DEFAULT_MANIFEST_MEDIA_TYPE,
    OCI_IMMUTABLE_CACHE_CONTROL,
    OCI_MUTABLE_CACHE_CONTROL,
    registry_init,
    upload_oci,
)

from .test_upload_file import ENV, FakeS3Client


class UploadRegistryTests(unittest.TestCase):
    def test_registry_init_parser(self) -> None:
        args = build_parser().parse_args(["registry", "init"])

        self.assertEqual(args.registry_action, "init")

    def test_upload_oci_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "oci",
                "upload",
                "cache/oci/anatase",
                "images/anatase",
                "--tag",
                "latest",
                "--tag",
                "f44",
            ]
        )

        self.assertEqual(args.registry_action, "oci")
        self.assertEqual(args.registry_oci_action, "upload")
        self.assertEqual(args.local_oci_path, Path("cache/oci/anatase"))
        self.assertEqual(args.ref, "images/anatase")
        self.assertEqual(args.tags, ["latest", "f44"])

    def test_registry_init_command_dispatches(self) -> None:
        args = build_parser().parse_args(["registry", "init"])

        with patch("ludos.__main__.registry_init", return_value=0) as init:
            self.assertEqual(args.func(args), 0)

        init.assert_called_once_with()

    def test_upload_oci_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "oci",
                "upload",
                "cache/oci/anatase",
                "images/anatase",
                "--tag",
                "latest",
                "--tag",
                "f44",
            ]
        )

        with patch("ludos.__main__.upload_oci", return_value=0) as upload:
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("cache/oci/anatase"),
            "images/anatase",
            ("latest", "f44"),
        )

    def test_upload_oci_parser_requires_tag(self) -> None:
        with patch("sys.stderr", new=StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    [
                        "registry",
                        "oci",
                        "upload",
                        "cache/oci/anatase",
                        "images/anatase",
                    ]
                )

    def test_registry_init_uploads_v2_ping_objects(self) -> None:
        client = FakeS3Client()

        self.assertEqual(registry_init(environ=ENV, client=client), 0)

        self.assertEqual(
            client.puts,
            [
                {
                    "Bucket": "anatase-artifacts",
                    "Key": "v2/",
                    "Body": b"{}",
                    "ContentType": "application/json",
                    "CacheControl": OCI_MUTABLE_CACHE_CONTROL,
                },
                {
                    "Bucket": "anatase-artifacts",
                    "Key": "v2",
                    "Body": b"{}",
                    "ContentType": "application/json",
                    "CacheControl": OCI_MUTABLE_CACHE_CONTROL,
                },
            ],
        )

    def test_upload_oci_uploads_expected_keys_content_types_and_ordering(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))

            self.assertEqual(
                upload_oci(
                    layout.root,
                    "images/anatase",
                    ("latest", "f44"),
                    environ=ENV,
                    client=client,
                ),
                0,
            )

        publish_calls = [
            call for call in client.calls if call[0] in ("upload_file", "put_object")
        ]
        self.assertEqual(
            publish_calls,
            [
                (
                    "upload_file",
                    f"v2/images/anatase/blobs/{layout.layer_digest}",
                ),
                (
                    "upload_file",
                    f"v2/images/anatase/blobs/{layout.config_digest}",
                ),
                (
                    "put_object",
                    f"v2/images/anatase/manifests/{layout.manifest_digest}",
                ),
                ("put_object", "v2/images/anatase/manifests/latest"),
                ("put_object", "v2/images/anatase/manifests/f44"),
            ],
        )
        self.assertEqual(
            [item["ExtraArgs"]["ContentType"] for item in client.uploads],
            [DEFAULT_LAYER_MEDIA_TYPE, DEFAULT_CONFIG_MEDIA_TYPE],
        )
        self.assertEqual(
            [item["ExtraArgs"]["CacheControl"] for item in client.uploads],
            [OCI_IMMUTABLE_CACHE_CONTROL, OCI_IMMUTABLE_CACHE_CONTROL],
        )
        self.assertEqual(
            [item["ContentType"] for item in client.puts],
            [
                DEFAULT_MANIFEST_MEDIA_TYPE,
                DEFAULT_MANIFEST_MEDIA_TYPE,
                DEFAULT_MANIFEST_MEDIA_TYPE,
            ],
        )
        self.assertEqual(
            [item["Body"] for item in client.puts],
            [layout.manifest_bytes, layout.manifest_bytes, layout.manifest_bytes],
        )
        self.assertEqual(
            [item["CacheControl"] for item in client.puts],
            [
                OCI_IMMUTABLE_CACHE_CONTROL,
                OCI_MUTABLE_CACHE_CONTROL,
                OCI_MUTABLE_CACHE_CONTROL,
            ],
        )
        self.assertNotIn(("anatase-artifacts", "v2"), client.objects)
        self.assertNotIn(("anatase-artifacts", "v2/"), client.objects)

    def test_upload_oci_skips_existing_matching_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))
            client = FakeS3Client(
                {
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.layer_digest}",
                    ): layout.layer_bytes,
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.config_digest}",
                    ): layout.config_bytes,
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/manifests/{layout.manifest_digest}",
                    ): layout.manifest_bytes,
                }
            )

            upload_oci(
                layout.root,
                "images/anatase",
                ("latest",),
                environ=ENV,
                client=client,
            )

        self.assertEqual(client.uploads, [])
        self.assertEqual(
            [item["Key"] for item in client.puts],
            ["v2/images/anatase/manifests/latest"],
        )

    def test_upload_oci_reuploads_existing_mismatched_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))
            client = FakeS3Client(
                {
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.layer_digest}",
                    ): b"wrong",
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.config_digest}",
                    ): b"wrong",
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/manifests/{layout.manifest_digest}",
                    ): b"wrong",
                }
            )

            upload_oci(
                layout.root,
                "images/anatase",
                ("latest",),
                environ=ENV,
                client=client,
            )

        self.assertEqual(
            [item["Key"] for item in client.uploads],
            [
                f"v2/images/anatase/blobs/{layout.layer_digest}",
                f"v2/images/anatase/blobs/{layout.config_digest}",
            ],
        )
        self.assertEqual(
            [item["Key"] for item in client.puts],
            [
                f"v2/images/anatase/manifests/{layout.manifest_digest}",
                "v2/images/anatase/manifests/latest",
            ],
        )

    def test_upload_oci_logs_only_uploaded_layer_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(
                Path(temp),
                layer_bytes=b"x" * (2 * 1024 * 1024),
            )
            client = FakeS3Client(
                {
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.config_digest}",
                    ): layout.config_bytes,
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/manifests/{layout.manifest_digest}",
                    ): layout.manifest_bytes,
                }
            )

            with patch("ludos.upload.registry.log") as log:
                upload_oci(
                    layout.root,
                    "images/anatase",
                    ("latest",),
                    environ=ENV,
                    client=client,
                )

        self.assertIn(
            call("Uploaded 2.0MB."),
            log.call_args_list,
        )
        messages = [item.args[0] for item in log.call_args_list]
        self.assertIn(
            (
                "Uploading layer: "
                f"v2/images/anatase/blobs/sha256:{layout.layer_hex[:11]}..."
            ),
            messages,
        )
        self.assertTrue(
            all(
                " -> " not in message
                for message in messages
                if message.startswith(("Uploading ", "Skipping "))
            )
        )
        self.assertTrue(all(layout.layer_hex not in message for message in messages))

        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(
                Path(temp),
                layer_bytes=b"x" * (2 * 1024 * 1024),
            )
            client = FakeS3Client(
                {
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.layer_digest}",
                    ): layout.layer_bytes,
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.config_digest}",
                    ): layout.config_bytes,
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/manifests/{layout.manifest_digest}",
                    ): layout.manifest_bytes,
                }
            )

            with patch("ludos.upload.registry.log") as log:
                upload_oci(
                    layout.root,
                    "images/anatase",
                    ("latest",),
                    environ=ENV,
                    client=client,
                )

        self.assertIn(
            call("Uploaded 0.0MB."),
            log.call_args_list,
        )

    def test_upload_oci_rejects_missing_layout_files(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ConfigError, "missing index.json"):
                upload_oci(
                    Path(temp),
                    "images/anatase",
                    ("latest",),
                    environ=ENV,
                    client=client,
                )

    def test_upload_oci_rejects_multiple_top_level_manifests(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))
            index_path = layout.root / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["manifests"].append(index["manifests"][0])
            index_path.write_text(json.dumps(index), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "exactly one manifest"):
                upload_oci(
                    layout.root,
                    "images/anatase",
                    ("latest",),
                    environ=ENV,
                    client=client,
                )

    def test_upload_oci_rejects_unsupported_digest_algorithm(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))
            index_path = layout.root / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["manifests"][0]["digest"] = "sha512:" + "0" * 128
            index_path.write_text(json.dumps(index), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "unsupported algorithm"):
                upload_oci(
                    layout.root,
                    "images/anatase",
                    ("latest",),
                    environ=ENV,
                    client=client,
                )

    def test_upload_oci_rejects_missing_blobs(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))
            (layout.root / "blobs" / "sha256" / layout.layer_hex).unlink()

            with self.assertRaisesRegex(ConfigError, "blob is missing"):
                upload_oci(
                    layout.root,
                    "images/anatase",
                    ("latest",),
                    environ=ENV,
                    client=client,
                )

    def test_upload_oci_rejects_invalid_refs(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))

            for ref in ("docker://images/anatase", "/images/anatase", "images//a"):
                with self.subTest(ref=ref):
                    with self.assertRaises(ConfigError):
                        upload_oci(
                            layout.root,
                            ref,
                            ("latest",),
                            environ=ENV,
                            client=client,
                        )

    def test_upload_oci_rejects_duplicate_and_invalid_tags(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))

            for tags in (("latest", "latest"), ("bad/tag",), ("bad tag",), ("a:b",)):
                with self.subTest(tags=tags):
                    with self.assertRaises(ConfigError):
                        upload_oci(
                            layout.root,
                            "images/anatase",
                            tags,
                            environ=ENV,
                            client=client,
                        )


class TinyOciLayout:
    def __init__(
        self,
        root: Path,
        *,
        config_bytes: bytes,
        layer_bytes: bytes,
        manifest_bytes: bytes,
    ) -> None:
        self.root = root
        self.config_bytes = config_bytes
        self.layer_bytes = layer_bytes
        self.manifest_bytes = manifest_bytes
        self.config_hex = hashlib.sha256(config_bytes).hexdigest()
        self.layer_hex = hashlib.sha256(layer_bytes).hexdigest()
        self.manifest_hex = hashlib.sha256(manifest_bytes).hexdigest()
        self.config_digest = f"sha256:{self.config_hex}"
        self.layer_digest = f"sha256:{self.layer_hex}"
        self.manifest_digest = f"sha256:{self.manifest_hex}"


def _create_oci_layout(root: Path, *, layer_bytes: bytes = b"tiny layer") -> TinyOciLayout:
    (root / "blobs" / "sha256").mkdir(parents=True)
    (root / "oci-layout").write_text(
        json.dumps({"imageLayoutVersion": "1.0.0"}),
        encoding="utf-8",
    )
    config_bytes = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": []},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    config_hex = hashlib.sha256(config_bytes).hexdigest()
    layer_hex = hashlib.sha256(layer_bytes).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": DEFAULT_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": DEFAULT_CONFIG_MEDIA_TYPE,
            "digest": f"sha256:{config_hex}",
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": DEFAULT_LAYER_MEDIA_TYPE,
                "digest": f"sha256:{layer_hex}",
                "size": len(layer_bytes),
            }
        ],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    layout = TinyOciLayout(
        root,
        config_bytes=config_bytes,
        layer_bytes=layer_bytes,
        manifest_bytes=manifest_bytes,
    )
    (root / "blobs" / "sha256" / layout.config_hex).write_bytes(config_bytes)
    (root / "blobs" / "sha256" / layout.layer_hex).write_bytes(layer_bytes)
    (root / "blobs" / "sha256" / layout.manifest_hex).write_bytes(manifest_bytes)
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": DEFAULT_MANIFEST_MEDIA_TYPE,
                "digest": layout.manifest_digest,
                "size": len(manifest_bytes),
            }
        ],
    }
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return layout


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import call, patch

from ludos.__main__ import build_parser
from ludos.model import ConfigError, OciCosignConfig
from ludos.upload.registry import (
    DEFAULT_CONFIG_MEDIA_TYPE,
    DEFAULT_LAYER_MEDIA_TYPE,
    DEFAULT_MANIFEST_MEDIA_TYPE,
    OCI_IMMUTABLE_CACHE_CONTROL,
    OCI_MUTABLE_CACHE_CONTROL,
    OciTagPromotion,
    delete_oci_tags,
    list_oci_tags,
    prune_oci_tags,
    promote_oci_tags,
    registry_init,
    tree_shake_oci,
    update_flatpak_static_index,
    upload_oci,
)
from ludos.upload.cosign import build_cosign_artifacts

from .test_upload_file import ENV, FakeS3Client


ROOT = Path(__file__).resolve().parents[2]


class UploadRegistryTests(unittest.TestCase):
    def test_promote_oci_tags_preflights_mirrors_and_deduplicates(self) -> None:
        manifest = json.dumps({"schemaVersion": 2, "layers": []}).encode()
        client = FakeS3Client(
            {
                ("anatase-artifacts", "v2/anatase/manifests/rolling"): manifest,
                ("anatase-artifacts", "v2/anatase/manifests/stable"): b"old",
            }
        )
        promotion = OciTagPromotion("anatase", "rolling", "stable")

        promoted = promote_oci_tags(
            (promotion, promotion),
            environ=ENV,
            client=client,
        )

        self.assertEqual(len(promoted), 1)
        self.assertEqual(
            promoted[0].manifest_digest,
            f"sha256:{hashlib.sha256(manifest).hexdigest()}",
        )
        self.assertEqual(
            client.objects[
                ("anatase-artifacts", "v2/anatase/manifests/stable")
            ],
            manifest,
        )
        target_puts = [
            put
            for put in client.puts
            if put["Key"] == "v2/anatase/manifests/stable"
        ]
        self.assertEqual(len(target_puts), 1)
        self.assertEqual(target_puts[0]["ContentType"], DEFAULT_MANIFEST_MEDIA_TYPE)
        self.assertEqual(target_puts[0]["CacheControl"], OCI_MUTABLE_CACHE_CONTROL)
        tags = json.loads(
            client.objects[("anatase-artifacts", "v2/anatase/tags/list")]
        )
        self.assertEqual(tags, {"name": "anatase", "tags": ["rolling", "stable"]})

    def test_promote_oci_tags_missing_source_writes_nothing(self) -> None:
        manifest = json.dumps({"schemaVersion": 2, "layers": []}).encode()
        client = FakeS3Client(
            {
                ("anatase-artifacts", "v2/anatase/manifests/rolling"): manifest,
            }
        )

        with self.assertRaisesRegex(ConfigError, "OCI source tag is missing"):
            promote_oci_tags(
                (
                    OciTagPromotion("anatase", "rolling", "stable"),
                    OciTagPromotion("flatpaks/kate", "rolling-f44", "f44"),
                ),
                environ=ENV,
                client=client,
            )

        self.assertEqual(client.puts, [])

    def test_promote_oci_tags_rejects_invalid_or_identical_tags(self) -> None:
        with self.assertRaisesRegex(ConfigError, "invalid OCI tag"):
            promote_oci_tags(
                (OciTagPromotion("anatase", "bad/tag", "stable"),)
            )
        with self.assertRaisesRegex(ConfigError, "must differ"):
            promote_oci_tags(
                (OciTagPromotion("anatase", "stable", "stable"),)
            )

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
            project_root=Path.cwd(),
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

    def test_delete_oci_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "oci",
                "delete",
                "images/anatase",
                "--tag",
                "latest",
                "--tag",
                "f44",
                "--dry-run",
            ]
        )

        self.assertEqual(args.registry_action, "oci")
        self.assertEqual(args.registry_oci_action, "delete")
        self.assertEqual(args.ref, "images/anatase")
        self.assertEqual(args.tags, ["latest", "f44"])
        self.assertTrue(args.dry_run)

    def test_list_oci_parser(self) -> None:
        args = build_parser().parse_args(
            ["registry", "oci", "list", "images/anatase"]
        )

        self.assertEqual(args.registry_action, "oci")
        self.assertEqual(args.registry_oci_action, "list")
        self.assertEqual(args.ref, "images/anatase")

    def test_delete_oci_parser_requires_tag(self) -> None:
        with patch("sys.stderr", new=StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    ["registry", "oci", "delete", "images/anatase"]
                )

    def test_prune_oci_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "oci",
                "prune",
                "images/anatase",
                "--pattern",
                "f*",
                "--dry-run",
            ]
        )

        self.assertEqual(args.registry_action, "oci")
        self.assertEqual(args.registry_oci_action, "prune")
        self.assertEqual(args.ref, "images/anatase")
        self.assertEqual(args.pattern, "f*")
        self.assertEqual(args.rule, "descending")
        self.assertEqual(args.number, 3)
        self.assertTrue(args.dry_run)

    def test_tree_shake_oci_parser(self) -> None:
        args = build_parser().parse_args(
            ["registry", "oci", "tree-shake", "images/anatase", "--dry-run"]
        )

        self.assertEqual(args.registry_action, "oci")
        self.assertEqual(args.registry_oci_action, "tree-shake")
        self.assertEqual(args.ref, "images/anatase")
        self.assertTrue(args.dry_run)

    def test_registry_flatpak_refresh_parser(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "refresh", "anatase.yml"]
        )

        self.assertEqual(args.registry_action, "flatpak")
        self.assertEqual(args.registry_flatpak_action, "refresh")
        self.assertEqual(args.manifest, Path("anatase.yml"))

    def test_delete_oci_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "oci",
                "delete",
                "images/anatase",
                "--tag",
                "latest",
                "--tag",
                "f44",
                "--dry-run",
            ]
        )

        with patch("ludos.__main__.delete_oci_tags", return_value=0) as delete:
            self.assertEqual(args.func(args), 0)

        delete.assert_called_once_with(
            "images/anatase",
            ("latest", "f44"),
            dry_run=True,
        )

    def test_list_oci_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            ["registry", "oci", "list", "images/anatase"]
        )

        with patch("ludos.__main__.list_oci_tags", return_value=0) as list_tags:
            self.assertEqual(args.func(args), 0)

        list_tags.assert_called_once_with("images/anatase")

    def test_prune_oci_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "oci",
                "prune",
                "images/anatase",
                "--pattern",
                "f*",
                "--dry-run",
            ]
        )

        with patch("ludos.__main__.prune_oci_tags", return_value=0) as prune:
            self.assertEqual(args.func(args), 0)

        prune.assert_called_once_with(
            "images/anatase",
            "f*",
            rule="descending",
            number=3,
            dry_run=True,
        )

    def test_tree_shake_oci_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            ["registry", "oci", "tree-shake", "images/anatase", "--dry-run"]
        )

        with patch("ludos.__main__.tree_shake_oci", return_value=0) as tree_shake:
            self.assertEqual(args.func(args), 0)

        tree_shake.assert_called_once_with(
            "images/anatase",
            dry_run=True,
            project_root=Path.cwd(),
        )

    def test_registry_flatpak_refresh_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "refresh", "anatase.yml"]
        )

        with patch("ludos.__main__.update_flatpak_index", return_value=0) as update:
            self.assertEqual(args.func(args), 0)

        update.assert_called_once_with(Path("anatase.yml"))

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
                    "CacheControl": OCI_IMMUTABLE_CACHE_CONTROL,
                },
                {
                    "Bucket": "anatase-artifacts",
                    "Key": "v2",
                    "Body": b"{}",
                    "ContentType": "application/json",
                    "CacheControl": OCI_IMMUTABLE_CACHE_CONTROL,
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
                ("put_object", "v2/images/anatase/tags/list"),
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
                "application/json",
            ],
        )
        self.assertEqual(
            [item["Body"] for item in client.puts],
            [
                layout.manifest_bytes,
                layout.manifest_bytes,
                layout.manifest_bytes,
                b'{"name":"images/anatase","tags":["f44","latest"]}',
            ],
        )
        self.assertEqual(
            [item["CacheControl"] for item in client.puts],
            [
                OCI_IMMUTABLE_CACHE_CONTROL,
                OCI_MUTABLE_CACHE_CONTROL,
                OCI_MUTABLE_CACHE_CONTROL,
                OCI_MUTABLE_CACHE_CONTROL,
            ],
        )
        self.assertNotIn(("anatase-artifacts", "v2"), client.objects)
        self.assertNotIn(("anatase-artifacts", "v2/"), client.objects)

    def test_upload_oci_skips_existing_matching_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))
            objects = {
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
            client = FakeS3Client(
                objects,
                cache_controls={
                    key: OCI_IMMUTABLE_CACHE_CONTROL for key in objects
                },
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
            ["v2/images/anatase/manifests/latest", "v2/images/anatase/tags/list"],
        )

    def test_upload_oci_reuploads_existing_objects_with_stale_cache_control(
        self,
    ) -> None:
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
                },
                cache_controls={
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.layer_digest}",
                    ): OCI_MUTABLE_CACHE_CONTROL,
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/blobs/{layout.config_digest}",
                    ): OCI_MUTABLE_CACHE_CONTROL,
                    (
                        "anatase-artifacts",
                        f"v2/images/anatase/manifests/{layout.manifest_digest}",
                    ): OCI_MUTABLE_CACHE_CONTROL,
                },
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
                "v2/images/anatase/tags/list",
            ],
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
                "v2/images/anatase/tags/list",
            ],
        )

    def test_upload_oci_cosign_missing_env_fails_before_upload(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(Path(temp))

            with self.assertRaisesRegex(ConfigError, "LUDOS_COSIGN_KEY"):
                upload_oci(
                    layout.root,
                    "images/anatase",
                    ("latest",),
                    environ=ENV,
                    client=client,
                    project_root=Path(temp),
                    cosign_config=_cosign_config(),
                )

        self.assertEqual(client.uploads, [])
        self.assertEqual(client.puts, [])

    def test_upload_oci_cosign_signs_verifies_before_tag_publish(self) -> None:
        client = FakeS3Client()
        events = []

        def sign(**kwargs: object) -> tuple[object, object]:
            events.append("sign")
            self.assertIsNotNone(kwargs["signing_config"])
            return object(), object()

        def upload_artifacts(*_args: object) -> None:
            events.append("upload-cosign")

        def verify(*_args: object, **_kwargs: object) -> None:
            events.append("verify")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = _create_oci_layout(root / "layout")
            cert = ROOT / "cards/base/atomic/keys/anatase-c002.pem"
            ca = ROOT / "cards/base/atomic/keys/anatase-cosign-root.pem"

            with (
                patch("ludos.upload.registry.sign_oci_manifest", side_effect=sign),
                patch(
                    "ludos.upload.registry.upload_cosign_artifacts",
                    side_effect=upload_artifacts,
                ),
                patch("ludos.upload.registry.verify_cosign_signature", side_effect=verify),
            ):
                self.assertEqual(
                    upload_oci(
                        layout.root,
                        "images/anatase",
                        ("latest",),
                        environ={
                            **ENV,
                            "LUDOS_COSIGN_KEY": (
                                "gcpkms://projects/example/locations/global/"
                                "keyRings/cosign/cryptoKeys/test/versions/1"
                            ),
                            "LUDOS_COSIGN_CERT": str(cert),
                        },
                        client=client,
                        project_root=root,
                        cosign_config=OciCosignConfig(
                            registry="https://flatpaks.example.test/",
                            identity="cosign.anatase.org",
                            verify=str(ca),
                        ),
                    ),
                    0,
                )

        self.assertEqual(events, ["sign", "upload-cosign", "verify"])
        self.assertEqual(
            [item["Key"] for item in client.puts[-2:]],
            ["v2/images/anatase/manifests/latest", "v2/images/anatase/tags/list"],
        )

    def test_upload_oci_cosign_verify_failure_skips_tag_publish(self) -> None:
        client = FakeS3Client()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layout = _create_oci_layout(root / "layout")
            cert = ROOT / "cards/base/atomic/keys/anatase-c002.pem"
            ca = ROOT / "cards/base/atomic/keys/anatase-cosign-root.pem"

            with (
                patch(
                    "ludos.upload.registry.sign_oci_manifest",
                    return_value=(object(), object()),
                ),
                patch("ludos.upload.registry.upload_cosign_artifacts"),
                patch(
                    "ludos.upload.registry.verify_cosign_signature",
                    side_effect=ConfigError("cosign verification failed"),
                ),
            ):
                with self.assertRaisesRegex(ConfigError, "cosign verification"):
                    upload_oci(
                        layout.root,
                        "images/anatase",
                        ("latest",),
                        environ={
                            **ENV,
                            "LUDOS_COSIGN_KEY": (
                                "gcpkms://projects/example/locations/global/"
                                "keyRings/cosign/cryptoKeys/test/versions/1"
                            ),
                            "LUDOS_COSIGN_CERT": str(cert),
                        },
                        client=client,
                        project_root=root,
                        cosign_config=OciCosignConfig(
                            registry="https://flatpaks.example.test/",
                            identity="cosign.anatase.org",
                            verify=str(ca),
                        ),
                    )

        self.assertNotIn(
            ("put_object", "v2/images/anatase/manifests/latest"),
            client.calls,
        )
        self.assertNotIn(("put_object", "v2/images/anatase/tags/list"), client.calls)

    def test_upload_oci_logs_only_uploaded_layer_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            layout = _create_oci_layout(
                Path(temp),
                layer_bytes=b"x" * (2 * 1024 * 1024),
            )
            objects = {
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{layout.config_digest}",
                ): layout.config_bytes,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/manifests/{layout.manifest_digest}",
                ): layout.manifest_bytes,
            }
            client = FakeS3Client(
                objects,
                cache_controls={
                    key: OCI_IMMUTABLE_CACHE_CONTROL for key in objects
                },
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
            objects = {
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
            client = FakeS3Client(
                objects,
                cache_controls={
                    key: OCI_IMMUTABLE_CACHE_CONTROL for key in objects
                },
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

    def test_delete_oci_tags_deletes_only_tag_manifests(self) -> None:
        client = FakeS3Client(
            {
                ("anatase-artifacts", "v2/images/anatase/manifests/latest"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/manifests/f44"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/blobs/sha256:abc"): b"blob",
            }
        )

        self.assertEqual(
            delete_oci_tags(
                "images/anatase",
                ("latest", "f44"),
                environ=ENV,
                client=client,
            ),
            0,
        )

        self.assertEqual(
            [item["Key"] for item in client.deletes],
            [
                "v2/images/anatase/manifests/latest",
                "v2/images/anatase/manifests/f44",
            ],
        )
        self.assertEqual(client.heads, [])
        self.assertEqual(
            client.lists,
            [
                {
                    "Bucket": "anatase-artifacts",
                    "Prefix": "v2/images/anatase/manifests/",
                }
            ],
        )
        self.assertEqual(
            client.objects[("anatase-artifacts", "v2/images/anatase/tags/list")],
            b'{"name":"images/anatase","tags":[]}',
        )
        self.assertIn(
            ("anatase-artifacts", "v2/images/anatase/blobs/sha256:abc"),
            client.objects,
        )

    def test_delete_oci_tags_warns_and_continues_on_missing_tag(self) -> None:
        missing_key = "v2/images/anatase/manifests/missing"
        client = FakeS3Client(
            {("anatase-artifacts", "v2/images/anatase/manifests/latest"): b"tag"}
        )
        client.delete_errors[("anatase-artifacts", missing_key)] = "NoSuchKey"

        with patch("ludos.upload.registry.warning") as warning:
            self.assertEqual(
                delete_oci_tags(
                    "images/anatase",
                    ("missing", "latest"),
                    environ=ENV,
                    client=client,
                ),
                0,
            )

        warning.assert_called_once_with("OCI tag is already missing: images/anatase:missing")
        self.assertEqual(
            [item["Key"] for item in client.deletes],
            [missing_key, "v2/images/anatase/manifests/latest"],
        )
        self.assertEqual(
            client.objects[("anatase-artifacts", "v2/images/anatase/tags/list")],
            b'{"name":"images/anatase","tags":[]}',
        )

    def test_delete_oci_tags_dry_run_does_not_delete(self) -> None:
        client = FakeS3Client(
            {("anatase-artifacts", "v2/images/anatase/manifests/latest"): b"tag"}
        )

        with patch("ludos.upload.registry.log") as log:
            self.assertEqual(
                delete_oci_tags(
                    "images/anatase",
                    ("latest",),
                    dry_run=True,
                    environ=ENV,
                    client=client,
                ),
                0,
            )

        self.assertEqual(client.deletes, [])
        log.assert_called_once_with(
            "Would delete tag: v2/images/anatase/manifests/latest"
        )

    def test_list_oci_tags_lists_tags_and_skips_sha_prefixes(self) -> None:
        client = FakeS3Client(
            {
                ("anatase-artifacts", "v2/images/anatase/manifests/f44"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/manifests/latest"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/manifests/sha-test"): b"tag",
                (
                    "anatase-artifacts",
                    "v2/images/anatase/manifests/sha256:" + "a" * 64,
                ): b"manifest",
                ("anatase-artifacts", "v2/images/anatase/blobs/sha256:abc"): b"blob",
            }
        )

        with patch("ludos.upload.registry.log") as log:
            self.assertEqual(
                list_oci_tags("images/anatase", environ=ENV, client=client),
                0,
            )

        self.assertEqual(
            client.lists,
            [
                {
                    "Bucket": "anatase-artifacts",
                    "Prefix": "v2/images/anatase/manifests/",
                }
            ],
        )
        self.assertEqual(log.call_args_list, [call("f44"), call("latest")])
        self.assertEqual(client.deletes, [])

    def test_prune_oci_tags_keeps_descending_number_and_deletes_rest(self) -> None:
        client = FakeS3Client(
            {
                ("anatase-artifacts", "v2/images/anatase/manifests/f44"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/manifests/f43"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/manifests/f42"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/manifests/f41"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/manifests/latest"): b"tag",
                (
                    "anatase-artifacts",
                    "v2/images/anatase/manifests/sha256:" + "a" * 64,
                ): b"manifest",
                ("anatase-artifacts", "v2/images/anatase/blobs/sha256:abc"): b"blob",
            }
        )

        self.assertEqual(
            prune_oci_tags(
                "images/anatase",
                "f*",
                rule="descending",
                number=3,
                environ=ENV,
                client=client,
            ),
            0,
        )

        self.assertEqual(
            client.lists,
            [
                {
                    "Bucket": "anatase-artifacts",
                    "Prefix": "v2/images/anatase/manifests/",
                },
                {
                    "Bucket": "anatase-artifacts",
                    "Prefix": "v2/images/anatase/manifests/",
                }
            ],
        )
        self.assertEqual(
            [item["Key"] for item in client.deletes],
            ["v2/images/anatase/manifests/f41"],
        )
        self.assertEqual(
            client.objects[("anatase-artifacts", "v2/images/anatase/tags/list")],
            b'{"name":"images/anatase","tags":["f42","f43","f44","latest"]}',
        )
        self.assertIn(
            ("anatase-artifacts", "v2/images/anatase/blobs/sha256:abc"),
            client.objects,
        )

    def test_prune_oci_tags_dry_run_does_not_delete(self) -> None:
        client = FakeS3Client(
            {
                ("anatase-artifacts", "v2/images/anatase/manifests/f44"): b"tag",
                ("anatase-artifacts", "v2/images/anatase/manifests/f43"): b"tag",
            }
        )

        with patch("ludos.upload.registry.log") as log:
            self.assertEqual(
                prune_oci_tags(
                    "images/anatase",
                    "f*",
                    rule="descending",
                    number=1,
                    dry_run=True,
                    environ=ENV,
                    client=client,
                ),
                0,
            )

        self.assertEqual(client.deletes, [])
        log.assert_called_once_with("Would delete tag: v2/images/anatase/manifests/f43")

    def test_prune_oci_tags_rejects_unsupported_rule(self) -> None:
        client = FakeS3Client()

        with self.assertRaisesRegex(ConfigError, "unsupported registry oci prune rule"):
            prune_oci_tags(
                "images/anatase",
                "f*",
                rule="ascending",
                number=1,
                environ=ENV,
                client=client,
            )

    def test_tree_shake_oci_deletes_unreferenced_blobs_only(self) -> None:
        config_digest = "sha256:" + "1" * 64
        first_layer_digest = "sha256:" + "2" * 64
        second_layer_digest = "sha256:" + "3" * 64
        unused_digest = "sha256:" + "4" * 64
        latest_manifest = _manifest_bytes(config_digest, first_layer_digest)
        latest_manifest_digest = "sha256:" + hashlib.sha256(latest_manifest).hexdigest()
        client = FakeS3Client(
            {
                (
                    "anatase-artifacts",
                    "v2/images/anatase/manifests/latest",
                ): latest_manifest,
                (
                    "anatase-artifacts",
                    "v2/images/anatase/manifests/f44",
                ): _manifest_bytes(config_digest, second_layer_digest),
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/manifests/{latest_manifest_digest}",
                ): latest_manifest,
                (
                    "anatase-artifacts",
                    "v2/images/anatase/manifests/sha256:" + "5" * 64,
                ): _manifest_bytes(config_digest, unused_digest),
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{config_digest}",
                ): b"config",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{first_layer_digest}",
                ): b"layer1",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{second_layer_digest}",
                ): b"layer2",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{unused_digest}",
                ): b"unused",
            }
        )

        self.assertEqual(
            tree_shake_oci("images/anatase", environ=ENV, client=client),
            0,
        )

        self.assertEqual(
            client.lists,
            [
                {
                    "Bucket": "anatase-artifacts",
                    "Prefix": "v2/images/anatase/manifests/",
                },
                {
                    "Bucket": "anatase-artifacts",
                    "Prefix": "v2/images/anatase/referrers/",
                },
                {
                    "Bucket": "anatase-artifacts",
                    "Prefix": "v2/images/anatase/blobs/",
                },
            ],
        )
        self.assertEqual(
            [item["Key"] for item in client.gets],
            [
                "v2/images/anatase/manifests/f44",
                "v2/images/anatase/manifests/latest",
            ],
        )
        self.assertEqual(
            [item["Key"] for item in client.deletes],
            [
                "v2/images/anatase/manifests/sha256:" + "5" * 64,
                f"v2/images/anatase/blobs/{unused_digest}",
            ],
        )

    def test_tree_shake_oci_dry_run_does_not_delete(self) -> None:
        config_digest = "sha256:" + "1" * 64
        layer_digest = "sha256:" + "2" * 64
        unused_digest = "sha256:" + "3" * 64
        client = FakeS3Client(
            {
                (
                    "anatase-artifacts",
                    "v2/images/anatase/manifests/latest",
                ): _manifest_bytes(config_digest, layer_digest),
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{layer_digest}",
                ): b"layer",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{unused_digest}",
                ): b"unused",
            }
        )

        with patch("ludos.upload.registry.log") as log:
            self.assertEqual(
                tree_shake_oci(
                    "images/anatase",
                    dry_run=True,
                    environ=ENV,
                    client=client,
                ),
                0,
            )

        self.assertEqual(client.deletes, [])
        self.assertEqual(
            log.call_args_list,
            [
                call("Downloading 1 manifests"),
                call(
                    f"Would delete blob: "
                    f"v2/images/anatase/blobs/sha256:{'3' * 11}..."
                ),
            ],
        )

    def test_tree_shake_oci_keeps_live_cosign_artifacts(self) -> None:
        config_digest = "sha256:" + "1" * 64
        layer_digest = "sha256:" + "2" * 64
        unused_digest = "sha256:" + "3" * 64
        manifest = _manifest_bytes(config_digest, layer_digest)
        manifest_digest = f"sha256:{hashlib.sha256(manifest).hexdigest()}"
        artifacts = build_cosign_artifacts(
            payload=b"payload\n",
            signature=b"signature",
            certificate="leaf\n",
            manifest_digest=manifest_digest,
            manifest_media_type=DEFAULT_MANIFEST_MEDIA_TYPE,
            manifest_size=len(manifest),
        )
        subject_hex = manifest_digest.removeprefix("sha256:")
        client = FakeS3Client(
            {
                ("anatase-artifacts", "v2/images/anatase/manifests/latest"): manifest,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/manifests/{manifest_digest}",
                ): manifest,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/manifests/sha256-{subject_hex}.sig",
                ): artifacts.legacy_manifest,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/manifests/{artifacts.referrer_manifest_digest}",
                ): artifacts.referrer_manifest,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/referrers/{manifest_digest}",
                ): artifacts.referrers_index,
                ("anatase-artifacts", f"v2/images/anatase/blobs/{config_digest}"): b"c",
                ("anatase-artifacts", f"v2/images/anatase/blobs/{layer_digest}"): b"l",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{artifacts.config_digest}",
                ): artifacts.config,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{artifacts.payload_digest}",
                ): artifacts.payload,
                ("anatase-artifacts", f"v2/images/anatase/blobs/{unused_digest}"): b"u",
            }
        )

        self.assertEqual(
            tree_shake_oci(
                "images/anatase",
                environ=ENV,
                client=client,
                cosign_config=_cosign_config(),
            ),
            0,
        )

        self.assertEqual(
            [item["Key"] for item in client.deletes],
            [f"v2/images/anatase/blobs/{unused_digest}"],
        )

    def test_tree_shake_oci_deletes_dead_cosign_artifacts(self) -> None:
        live_config_digest = "sha256:" + "1" * 64
        live_layer_digest = "sha256:" + "2" * 64
        dead_config_digest = "sha256:" + "3" * 64
        dead_layer_digest = "sha256:" + "4" * 64
        live_manifest = _manifest_bytes(live_config_digest, live_layer_digest)
        dead_manifest = _manifest_bytes(dead_config_digest, dead_layer_digest)
        dead_digest = f"sha256:{hashlib.sha256(dead_manifest).hexdigest()}"
        dead_hex = dead_digest.removeprefix("sha256:")
        artifacts = build_cosign_artifacts(
            payload=b"payload\n",
            signature=b"signature",
            certificate="leaf\n",
            manifest_digest=dead_digest,
            manifest_media_type=DEFAULT_MANIFEST_MEDIA_TYPE,
            manifest_size=len(dead_manifest),
        )
        client = FakeS3Client(
            {
                (
                    "anatase-artifacts",
                    "v2/images/anatase/manifests/latest",
                ): live_manifest,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/manifests/{dead_digest}",
                ): dead_manifest,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/manifests/sha256-{dead_hex}.sig",
                ): artifacts.legacy_manifest,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/manifests/{artifacts.referrer_manifest_digest}",
                ): artifacts.referrer_manifest,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/referrers/{dead_digest}",
                ): artifacts.referrers_index,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{live_config_digest}",
                ): b"c",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{live_layer_digest}",
                ): b"l",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{dead_config_digest}",
                ): b"dc",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{dead_layer_digest}",
                ): b"dl",
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{artifacts.config_digest}",
                ): artifacts.config,
                (
                    "anatase-artifacts",
                    f"v2/images/anatase/blobs/{artifacts.payload_digest}",
                ): artifacts.payload,
            }
        )

        self.assertEqual(
            tree_shake_oci(
                "images/anatase",
                environ=ENV,
                client=client,
                cosign_config=_cosign_config(),
            ),
            0,
        )

        self.assertCountEqual(
            [item["Key"] for item in client.deletes],
            [
                f"v2/images/anatase/referrers/{dead_digest}",
                f"v2/images/anatase/manifests/{dead_digest}",
                f"v2/images/anatase/manifests/{artifacts.referrer_manifest_digest}",
                f"v2/images/anatase/manifests/sha256-{dead_hex}.sig",
                f"v2/images/anatase/blobs/{dead_config_digest}",
                f"v2/images/anatase/blobs/{dead_layer_digest}",
                f"v2/images/anatase/blobs/{artifacts.config_digest}",
                f"v2/images/anatase/blobs/{artifacts.payload_digest}",
            ],
        )

    def test_update_flatpak_static_index_uploads_matching_distro_tags(self) -> None:
        kate = _flatpak_registry_objects(
            "flatpaks/kate",
            "f44-x86_64",
            "app/org.kde.kate/x86_64/stable",
            labels={
                "org.freedesktop.appstream.appdata": "<components><component/></components>",
                "org.freedesktop.appstream.icon-64": "data:image/png;base64,AA==",
            },
            annotations={"org.opencontainers.image.title": "Kate"},
        )
        ark = _flatpak_registry_objects(
            "flatpaks/ark",
            "f44-x86_64",
            "app/org.kde.ark/x86_64/stable",
            architecture="amd64",
        )
        ignored_other_tag = _flatpak_registry_objects(
            "flatpaks/gwenview",
            "f43-x86_64",
            "app/org.kde.gwenview/x86_64/stable",
        )
        ignored_other_prefix = _flatpak_registry_objects(
            "images/anatase",
            "f44-x86_64",
            "app/org.example.Ignored/x86_64/stable",
        )
        client = FakeS3Client(
            {
                **kate.objects,
                **ark.objects,
                **ignored_other_tag.objects,
                **ignored_other_prefix.objects,
            }
        )

        self.assertEqual(
            update_flatpak_static_index("f44-x86_64", environ=ENV, client=client),
            0,
        )

        self.assertEqual(
            client.lists,
            [{"Bucket": "anatase-artifacts", "Prefix": "v2/flatpaks/"}],
        )
        self.assertEqual(
            [item["Key"] for item in client.puts],
            ["f44-x86_64/index/static"],
        )
        self.assertEqual(client.puts[0]["ContentType"], "application/json")
        self.assertEqual(client.puts[0]["CacheControl"], OCI_MUTABLE_CACHE_CONTROL)
        body = json.loads(client.puts[0]["Body"].decode("utf-8"))
        self.assertEqual(body["Registry"], "../../../")
        self.assertEqual(
            [repo["Name"] for repo in body["Results"]],
            ["flatpaks/ark", "flatpaks/kate"],
        )

        ark_image = body["Results"][0]["Images"][0]
        self.assertEqual(ark_image["Digest"], ark.manifest_digest)
        self.assertEqual(ark_image["MediaType"], DEFAULT_MANIFEST_MEDIA_TYPE)
        self.assertEqual(ark_image["OS"], "linux")
        self.assertEqual(ark_image["Architecture"], "amd64")
        self.assertEqual(
            ark_image["Labels"]["org.flatpak.ref"],
            "app/org.kde.ark/x86_64/stable",
        )
        self.assertEqual(
            ark_image["Labels"]["org.flatpak.commit-metadata.xa.token-type"],
            "AAAAAABp",
        )
        self.assertEqual(ark_image["Annotations"], {})
        self.assertEqual(ark_image["Tags"], ["f44-x86_64"])

        kate_image = body["Results"][1]["Images"][0]
        self.assertEqual(
            kate_image["Labels"]["org.freedesktop.appstream.appdata"],
            "<components><component/></components>",
        )
        self.assertEqual(
            kate_image["Annotations"]["org.opencontainers.image.title"],
            "Kate",
        )
        self.assertNotIn(
            "flatpaks/gwenview",
            [repo["Name"] for repo in body["Results"]],
        )
        self.assertNotIn("images/anatase", [repo["Name"] for repo in body["Results"]])

    def test_update_flatpak_static_index_rejects_missing_tags(self) -> None:
        client = FakeS3Client()

        with self.assertRaisesRegex(ConfigError, "no flatpak manifests found"):
            update_flatpak_static_index("f44-x86_64", environ=ENV, client=client)

    def test_update_flatpak_static_index_rejects_missing_flatpak_ref(self) -> None:
        target = _flatpak_registry_objects(
            "flatpaks/kate",
            "f44-x86_64",
            "app/org.kde.kate/x86_64/stable",
            include_flatpak_ref=False,
        )
        client = FakeS3Client(target.objects)

        with self.assertRaisesRegex(ConfigError, "missing org.flatpak.ref"):
            update_flatpak_static_index("f44-x86_64", environ=ENV, client=client)

    def test_update_flatpak_static_index_rejects_duplicate_refs(self) -> None:
        first = _flatpak_registry_objects(
            "flatpaks/kate",
            "f44-x86_64",
            "app/org.kde.kate/x86_64/stable",
        )
        second = _flatpak_registry_objects(
            "flatpaks/kate-duplicate",
            "f44-x86_64",
            "app/org.kde.kate/x86_64/stable",
        )
        client = FakeS3Client({**first.objects, **second.objects})

        with self.assertRaisesRegex(ConfigError, "duplicate flatpak ref"):
            update_flatpak_static_index("f44-x86_64", environ=ENV, client=client)


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


class TinyFlatpakRegistryObjects:
    def __init__(
        self,
        *,
        objects: dict[tuple[str, str], bytes],
        manifest_bytes: bytes,
    ) -> None:
        self.objects = objects
        self.manifest_bytes = manifest_bytes
        self.manifest_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"


def _manifest_bytes(config_digest: str, *layer_digests: str) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": DEFAULT_MANIFEST_MEDIA_TYPE,
            "config": {
                "mediaType": DEFAULT_CONFIG_MEDIA_TYPE,
                "digest": config_digest,
                "size": 1,
            },
            "layers": [
                {
                    "mediaType": DEFAULT_LAYER_MEDIA_TYPE,
                    "digest": digest,
                    "size": 1,
                }
                for digest in layer_digests
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _flatpak_registry_objects(
    repo_ref: str,
    tag: str,
    flatpak_ref: str,
    *,
    architecture: str = "amd64",
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    include_flatpak_ref: bool = True,
) -> TinyFlatpakRegistryObjects:
    image_labels = {} if labels is None else dict(labels)
    if include_flatpak_ref:
        image_labels["org.flatpak.ref"] = flatpak_ref
    config = {
        "architecture": architecture,
        "os": "linux",
        "config": {
            "Labels": image_labels,
        },
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    config_bytes = json.dumps(config, separators=(",", ":")).encode("utf-8")
    config_digest = f"sha256:{hashlib.sha256(config_bytes).hexdigest()}"
    manifest = {
        "schemaVersion": 2,
        "mediaType": DEFAULT_MANIFEST_MEDIA_TYPE,
        "config": {
            "mediaType": DEFAULT_CONFIG_MEDIA_TYPE,
            "digest": config_digest,
            "size": len(config_bytes),
        },
        "layers": [],
    }
    if annotations:
        manifest["annotations"] = annotations
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    return TinyFlatpakRegistryObjects(
        objects={
            (
                "anatase-artifacts",
                f"v2/{repo_ref}/manifests/{tag}",
            ): manifest_bytes,
            (
                "anatase-artifacts",
                f"v2/{repo_ref}/blobs/{config_digest}",
            ): config_bytes,
        },
        manifest_bytes=manifest_bytes,
    )


def _cosign_config() -> OciCosignConfig:
    return OciCosignConfig(
        registry="https://flatpaks.example.test/",
        identity="cosign.example.test",
        verify="root.pem",
    )


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

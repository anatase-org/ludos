from __future__ import annotations

import base64
from contextlib import ExitStack
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from ludos.__main__ import build_parser
from ludos.model import ConfigError
from ludos.upload.flatpaks import (
    tree_shake_flatpaks,
    upload_dummy_runtime,
    upload_flatpaks,
)


class UploadFlatpaksTests(unittest.TestCase):
    def test_registry_flatpak_upload_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "upload",
                "anatase.yml",
                "--flatpak",
                "flatpaks/ark",
                "--flatpak",
                "flatpaks/kate",
                "--build",
                "--cache-dir",
                "out/cache",
                "--update",
            ]
        )

        self.assertEqual(args.registry_action, "flatpak")
        self.assertEqual(args.registry_flatpak_action, "upload")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertEqual(args.flatpaks, [Path("flatpaks/ark"), Path("flatpaks/kate")])
        self.assertTrue(args.build)
        self.assertEqual(args.cache_dir, Path("out/cache"))
        self.assertTrue(args.update)

    def test_registry_flatpak_upload_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "upload",
                "anatase.yml",
                "--flatpak",
                "flatpaks/ark",
                "--build",
                "--cache-dir",
                "out/cache",
            ]
        )

        with (
            patch("ludos.__main__.upload_flatpaks", return_value=0) as upload,
            patch("ludos.__main__.update_flatpak_index", return_value=0) as update,
        ):
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("anatase.yml"),
            (Path("flatpaks/ark"),),
            build=True,
            cache_dir=Path("out/cache"),
        )
        update.assert_not_called()

    def test_registry_flatpak_upload_update_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "upload",
                "anatase.yml",
                "--flatpak",
                "flatpaks/ark",
                "--update",
            ]
        )

        with (
            patch("ludos.__main__.upload_flatpaks", return_value=0) as upload,
            patch("ludos.__main__.update_flatpak_index", return_value=0) as update,
        ):
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("anatase.yml"),
            (Path("flatpaks/ark"),),
            build=False,
            cache_dir=None,
        )
        update.assert_called_once_with(Path("anatase.yml"))

    def test_registry_flatpak_upload_update_skips_after_failed_upload(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "upload", "anatase.yml", "--update"]
        )

        with (
            patch("ludos.__main__.upload_flatpaks", return_value=17) as upload,
            patch("ludos.__main__.update_flatpak_index", return_value=0) as update,
        ):
            self.assertEqual(args.func(args), 17)

        upload.assert_called_once_with(
            Path("anatase.yml"),
            tuple(),
            build=False,
            cache_dir=None,
        )
        update.assert_not_called()

    def test_registry_flatpak_upload_dummy_runtime_parser(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "upload-dummy-runtime", "anatase.yml"]
        )

        self.assertEqual(args.registry_action, "flatpak")
        self.assertEqual(args.registry_flatpak_action, "upload-dummy-runtime")
        self.assertEqual(args.manifest, Path("anatase.yml"))

    def test_registry_flatpak_upload_dummy_runtime_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "upload-dummy-runtime", "anatase.yml"]
        )

        with patch("ludos.__main__.upload_dummy_runtime", return_value=0) as upload:
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(Path("anatase.yml"))

    def test_registry_flatpak_tree_shake_parser(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "tree-shake", "anatase.yml"]
        )

        self.assertEqual(args.registry_action, "flatpak")
        self.assertEqual(args.registry_flatpak_action, "tree-shake")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertIsNone(args.flatpaks)
        self.assertFalse(args.dry_run)

    def test_registry_flatpak_tree_shake_parser_accepts_selected_flatpaks(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "tree-shake",
                "custom.yml",
                "--flatpak",
                "flatpaks/ark",
                "--flatpak",
                "flatpaks/kate/card.yaml",
                "--dry-run",
            ]
        )

        self.assertEqual(args.manifest, Path("custom.yml"))
        self.assertEqual(
            args.flatpaks,
            [Path("flatpaks/ark"), Path("flatpaks/kate/card.yaml")],
        )
        self.assertTrue(args.dry_run)

    def test_registry_flatpak_tree_shake_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "tree-shake",
                "anatase.yml",
                "--flatpak",
                "flatpaks/ark",
                "--dry-run",
            ]
        )

        with patch("ludos.__main__.tree_shake_flatpaks", return_value=0) as shake:
            self.assertEqual(args.func(args), 0)

        shake.assert_called_once_with(
            Path("anatase.yml"),
            (Path("flatpaks/ark"),),
            dry_run=True,
        )

    def test_upload_flatpaks_exports_all_manifest_flatpaks_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))

            with _mock_upload_deps() as deps:
                self.assertEqual(upload_flatpaks(manifest, tuple(), False), 0)

            self.assertEqual(
                deps.run_streamed.call_args_list,
                [
                    _podman_push_call(
                        root / "cache" / "flatpaks" / "kate-f44-x86_64",
                        "localhost/flatpaks:f44-x86_64-kate",
                    ),
                    _podman_push_call(
                        root / "cache" / "flatpaks" / "ark-f44-x86_64",
                        "localhost/flatpaks:f44-x86_64-ark",
                    ),
                ],
            )
            self.assertEqual(
                deps.upload_oci.call_args_list,
                [
                    call(
                        root / "cache" / "flatpaks" / "kate-f44-x86_64",
                        "flatpaks/kate",
                        ("f44-x86_64",),
                    ),
                    call(
                        root / "cache" / "flatpaks" / "ark-f44-x86_64",
                        "flatpaks/ark",
                        ("f44-x86_64",),
                    ),
                ],
            )

    def test_upload_flatpaks_uses_selected_flatpaks_and_cache_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))
            cache_dir = root / "out" / "cache"

            with _mock_upload_deps() as deps:
                self.assertEqual(
                    upload_flatpaks(
                        manifest,
                        (Path("flatpaks/ark"),),
                        False,
                        cache_dir=cache_dir,
                    ),
                    0,
                )

            export_dir = cache_dir / "flatpaks" / "ark-f44-x86_64"
            deps.run_streamed.assert_called_once_with(
                [
                    "/usr/bin/podman",
                    "push",
                    "--format",
                    "oci",
                    "--compression-format",
                    "gzip",
                    "--force-compression",
                    "localhost/flatpaks:f44-x86_64-ark",
                    f"oci:{export_dir}:f44-x86_64",
                ]
            )
            deps.upload_oci.assert_called_once_with(
                export_dir,
                "flatpaks/ark",
                ("f44-x86_64",),
            )

    def test_upload_flatpaks_rejects_missing_local_image_without_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))

            with _mock_upload_deps(image_exists=False) as deps:
                with self.assertRaisesRegex(ConfigError, "flatpak image is not cached"):
                    upload_flatpaks(manifest, tuple(), False)

            deps.run_streamed.assert_not_called()
            deps.upload_oci.assert_not_called()

    def test_upload_flatpaks_builds_manifest_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))
            cache_dir = root / "cache"
            results = (
                SimpleNamespace(image="localhost/flatpaks:built-kate"),
                SimpleNamespace(image="localhost/flatpaks:built-ark"),
            )

            with _mock_upload_deps() as deps:
                with patch(
                    "ludos.upload.flatpaks.build_flatpaks",
                    return_value=results,
                ) as build:
                    self.assertEqual(
                        upload_flatpaks(manifest, tuple(), True, cache_dir=cache_dir),
                        0,
                    )

            build.assert_called_once_with(manifest, cache_dir=cache_dir)
            self.assertEqual(
                [item.args[0][7] for item in deps.run_streamed.call_args_list],
                ["localhost/flatpaks:built-kate", "localhost/flatpaks:built-ark"],
            )

    def test_upload_flatpaks_builds_selected_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))
            cache_dir = root / "cache"

            def build_one(_manifest: Path, flatpak: Path, **_kwargs: object) -> object:
                return SimpleNamespace(
                    image=f"localhost/flatpaks:built-{flatpak.parent.name}"
                )

            with _mock_upload_deps() as deps:
                with patch(
                    "ludos.upload.flatpaks.build_flatpak",
                    side_effect=build_one,
                ) as build:
                    self.assertEqual(
                        upload_flatpaks(
                            manifest,
                            (Path("flatpaks/ark/card.yaml"),),
                            True,
                            cache_dir=cache_dir,
                        ),
                        0,
                    )

            build.assert_called_once_with(
                manifest,
                (root / "flatpaks" / "ark" / "card.yaml").resolve(),
                cache_dir=cache_dir,
            )
            self.assertEqual(
                deps.run_streamed.call_args.args[0][7],
                "localhost/flatpaks:built-ark",
            )

    def test_tree_shake_flatpaks_uses_all_manifest_flatpaks_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))

            with patch("ludos.upload.flatpaks.tree_shake_oci", return_value=0) as shake:
                self.assertEqual(tree_shake_flatpaks(manifest, tuple()), 0)

        self.assertEqual(
            shake.call_args_list,
            [
                call("flatpaks/kate", dry_run=False),
                call("flatpaks/ark", dry_run=False),
            ],
        )

    def test_tree_shake_flatpaks_uses_selected_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))

            with patch("ludos.upload.flatpaks.tree_shake_oci", return_value=0) as shake:
                self.assertEqual(
                    tree_shake_flatpaks(
                        manifest,
                        (Path("flatpaks/ark/card.yaml"),),
                        dry_run=True,
                    ),
                    0,
                )

        shake.assert_called_once_with("flatpaks/ark", dry_run=True)

    def test_upload_dummy_runtime_writes_runtime_oci_and_updates_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, tuple())
            events = []
            manifest_digests = []

            def upload(path: Path, ref: str, tags: tuple[str, ...]) -> int:
                events.append(("upload", ref, tags))
                index = json.loads((path / "index.json").read_text(encoding="utf-8"))
                manifest_desc = index["manifests"][0]
                manifest_digests.append(manifest_desc["digest"])
                manifest_blob = json.loads(
                    (
                        path
                        / "blobs"
                        / "sha256"
                        / manifest_desc["digest"].removeprefix("sha256:")
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    manifest_blob["layers"][0]["mediaType"],
                    "application/vnd.oci.image.layer.v1.tar",
                )
                config_desc = manifest_blob["config"]
                config = json.loads(
                    (
                        path
                        / "blobs"
                        / "sha256"
                        / config_desc["digest"].removeprefix("sha256:")
                    ).read_text(encoding="utf-8")
                )
                labels = config["config"]["Labels"]
                title = "Anatase Test Runtime"
                description = "Anatase Platform runtime for tests."
                license = "LicenseRef-Anatase-Test"
                author = "Anatase Test Authors"
                self.assertEqual(config["architecture"], "amd64")
                self.assertEqual(
                    labels["org.flatpak.ref"],
                    "runtime/org.anatase.Platform/x86_64/stable",
                )
                self.assertIn("[Runtime]", labels["org.flatpak.metadata"])
                self.assertIn(
                    "sdk=org.anatase.ludos.Sdk/x86_64/stable",
                    labels["org.flatpak.metadata"],
                )
                self.assertEqual(labels["org.flatpak.timestamp"], "0")
                self.assertEqual(
                    _uint64_variant_label(
                        labels["org.flatpak.commit-metadata.xa.download-size"]
                    ),
                    0,
                )
                self.assertEqual(labels["org.flatpak.download-size"], "0")
                self.assertEqual(
                    _uint64_variant_label(
                        labels["org.flatpak.commit-metadata.xa.installed-size"]
                    ),
                    0,
                )
                self.assertEqual(labels["org.flatpak.installed-size"], "0")
                self.assertEqual(labels["org.flatpak.subject"], title)
                self.assertEqual(labels["org.flatpak.body"], description)
                self.assertEqual(
                    labels["org.opencontainers.image.title"],
                    title,
                )
                self.assertEqual(
                    labels["org.opencontainers.image.description"],
                    description,
                )
                self.assertEqual(labels["org.opencontainers.image.licenses"], license)
                self.assertEqual(labels["org.opencontainers.image.authors"], author)
                self.assertEqual(labels["org.opencontainers.image.vendor"], author)
                appdata = labels["org.freedesktop.appstream.appdata"]
                self.assertIn('<component type="runtime">', appdata)
                self.assertIn("<id>org.anatase.Platform</id>", appdata)
                self.assertIn(
                    "<bundle type=\"flatpak\">"
                    "runtime/org.anatase.Platform/x86_64/stable"
                    "</bundle>",
                    appdata,
                )
                self.assertIn("<name>Anatase Test Runtime</name>", appdata)
                self.assertIn(
                    "<project_group>Anatase Test Authors</project_group>",
                    appdata,
                )
                self.assertIn(
                    "<developer> <name>Anatase Test Authors</name> </developer>",
                    appdata,
                )
                self.assertIn(
                    "<developer_name>Anatase Test Authors</developer_name>",
                    appdata,
                )
                self.assertIn("<metadata_license>CC0-1.0</metadata_license>", appdata)
                self.assertIn(
                    "<project_license>LicenseRef-Anatase-Test</project_license>",
                    appdata,
                )
                return 0

            def update(distro: str) -> int:
                events.append(("update", distro))
                return 0

            with (
                patch("ludos.upload.flatpaks.upload_oci", side_effect=upload),
                patch(
                    "ludos.upload.flatpaks.update_flatpak_static_index",
                    side_effect=update,
                ),
            ):
                self.assertEqual(
                    upload_dummy_runtime(manifest, cache_dir=root / "cache"),
                    0,
                )
                self.assertEqual(
                    upload_dummy_runtime(manifest, cache_dir=root / "other-cache"),
                    0,
                )

        self.assertEqual(
            events,
            [
                ("upload", "flatpaks/runtime", ("f44-x86_64",)),
                ("update", "f44-x86_64"),
                ("upload", "flatpaks/runtime", ("f44-x86_64",)),
                ("update", "f44-x86_64"),
            ],
        )
        self.assertEqual(len(set(manifest_digests)), 1)

    def test_upload_dummy_runtime_omits_optional_display_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, tuple())
            manifest.write_text(
                manifest.read_text(encoding="utf-8")
                .replace("  title: Anatase Test Runtime\n", "")
                .replace("  description: Anatase Platform runtime for tests.\n", "")
                .replace("  license: LicenseRef-Anatase-Test\n", ""),
                encoding="utf-8",
            )

            def upload(path: Path, _ref: str, _tags: tuple[str, ...]) -> int:
                index = json.loads((path / "index.json").read_text(encoding="utf-8"))
                manifest_desc = index["manifests"][0]
                manifest_blob = json.loads(
                    (
                        path
                        / "blobs"
                        / "sha256"
                        / manifest_desc["digest"].removeprefix("sha256:")
                    ).read_text(encoding="utf-8")
                )
                config_desc = manifest_blob["config"]
                config = json.loads(
                    (
                        path
                        / "blobs"
                        / "sha256"
                        / config_desc["digest"].removeprefix("sha256:")
                    ).read_text(encoding="utf-8")
                )
                labels = config["config"]["Labels"]
                self.assertNotIn("org.flatpak.subject", labels)
                self.assertNotIn("org.flatpak.body", labels)
                self.assertNotIn("org.opencontainers.image.title", labels)
                self.assertNotIn("org.opencontainers.image.description", labels)
                self.assertNotIn("org.opencontainers.image.licenses", labels)
                self.assertNotIn("org.freedesktop.appstream.appdata", labels)
                return 0

            with (
                patch("ludos.upload.flatpaks.upload_oci", side_effect=upload),
                patch("ludos.upload.flatpaks.update_flatpak_static_index", return_value=0),
            ):
                self.assertEqual(
                    upload_dummy_runtime(manifest, cache_dir=root / "cache"),
                    0,
                )

    def test_upload_dummy_runtime_rejects_missing_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, tuple())
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "runtime:\n"
                    "  id: org.anatase.Platform\n"
                    "  repo: runtime\n"
                    "  branch: stable\n"
                    "  title: Anatase Test Runtime\n"
                    "  author: Anatase Test Authors\n"
                    "  description: Anatase Platform runtime for tests.\n"
                    "  license: LicenseRef-Anatase-Test\n",
                    "",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "runtime"):
                upload_dummy_runtime(manifest, cache_dir=root / "cache")


def _write_manifest(root: Path, flatpaks: tuple[str, ...]) -> Path:
    (root / "cards").mkdir()
    (root / "cards/bootstrap.yml").write_text("version: 1\n", encoding="utf-8")
    (root / "cards/base.yml").write_text("version: 1\n", encoding="utf-8")
    for flatpak in flatpaks:
        card_path = root / flatpak / "card.yaml"
        card_path.parent.mkdir(parents=True, exist_ok=True)
        card_path.write_text("version: 1\n", encoding="utf-8")
    manifest = root / "anatase.yml"
    manifest.write_text(
        "\n".join(
            [
                "version: 1",
                "name: Anatase Test",
                "env:",
                "  arch: x86_64",
                "releasever: '44'",
                "distro: f$releasever-$arch",
                "orchestrator: quay.io/fedora/fedora:$releasever",
                "runtime:",
                "  id: org.anatase.Platform",
                "  repo: runtime",
                "  branch: stable",
                "  title: Anatase Test Runtime",
                "  author: Anatase Test Authors",
                "  description: Anatase Platform runtime for tests.",
                "  license: LicenseRef-Anatase-Test",
                "bootstrap: cards/bootstrap.yml",
                "repos: []",
                "cards:",
                "  - cards/base.yml",
                "flatpaks:",
                *(f"  - {flatpak}" for flatpak in flatpaks),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


def _uint64_variant_label(value: str) -> int:
    return struct.unpack(">Q", base64.b64decode(value)[:8])[0]


class _MockUploadDeps:
    def __init__(self, run_streamed: object, upload_oci: object) -> None:
        self.run_streamed = run_streamed
        self.upload_oci = upload_oci


class _mock_upload_deps:
    def __init__(self, *, image_exists: bool = True) -> None:
        self.image_exists = image_exists

    def __enter__(self) -> _MockUploadDeps:
        self.stack = ExitStack()
        self.stack.enter_context(
            patch("ludos.upload.flatpaks.shutil.which", return_value="/usr/bin/podman")
        )
        self.stack.enter_context(
            patch("ludos.upload.flatpaks._image_exists", return_value=self.image_exists)
        )
        run_streamed = self.stack.enter_context(
            patch(
                "ludos.upload.flatpaks._run_streamed_command",
                return_value=(0, ""),
            )
        )
        upload_oci = self.stack.enter_context(
            patch("ludos.upload.flatpaks.upload_oci", return_value=0)
        )
        return _MockUploadDeps(run_streamed, upload_oci)

    def __exit__(self, *exc: object) -> None:
        self.stack.close()


def _podman_push_call(export_dir: Path, image: str) -> object:
    return call(
        [
            "/usr/bin/podman",
            "push",
            "--format",
            "oci",
            "--compression-format",
            "gzip",
            "--force-compression",
            image,
            f"oci:{export_dir}:f44-x86_64",
        ]
    )


if __name__ == "__main__":
    unittest.main()

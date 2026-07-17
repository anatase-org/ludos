from __future__ import annotations

import base64
import lzma
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import yaml

from ludos.__main__ import build_parser, ci_command, main
from ludos.build import (
    BuildImagePlan,
    OciImagePlan,
    PackageImagePlan,
    ResolvedBuildMetadata,
    _metadata_with_final_image,
)
from ludos.ci import (
    DEFAULT_PREPARE_WORKERS,
    DEFAULT_SEED_BUFFER_RATIO,
    SeedDiskSpaceError,
    _ci_remote_image_exists,
    _ci_final_build_outputs,
    _ci_final_dependency_images,
    _ci_final_oci_outputs,
    _build_ci_package,
    _build_ci_manifest_image,
    _build_ci_flatpak,
    _create_seed_builder_image,
    _inspect_remote_labels,
    _manifest_tag,
    _metadata_from_seed_entry,
    _prepare_seed_rpms,
    _read_seed_entries,
    _remove_ci_dependency_images,
    _remove_ci_remote_image,
    _rebase_ci_entry,
    _restore_ci_build_context,
    _seed_rpm_download_sizes,
    _upload_ci_output,
    build_ci,
    init_ci,
    prepare_ci,
    promote_ci,
    remove_ci,
    seed_ci,
    upload_ci,
    write_ci_env,
)
from ludos.model import ConfigError, SpecBuild
from ludos.upload.registry import OciTagPromotion, PromotedOciTag


class CiParserTests(unittest.TestCase):
    def test_parser_accepts_env_ci_options(self) -> None:
        args = build_parser().parse_args(
            ["ci", "env", "anatase.yml", "ghcr.io/test/anatase:latest"]
        )

        self.assertEqual(args.ci_action, "env")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertEqual(args.ref, "ghcr.io/test/anatase:latest")
        self.assertEqual(args.label, "org.opencontainers.image.version")
        self.assertIsNone(args.arch)

    def test_parser_accepts_custom_env_label(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "env",
                "anatase.yml",
                "ghcr.io/test/anatase:latest",
                "--label",
                "com.example.version",
            ]
        )

        self.assertEqual(args.label, "com.example.version")

    def test_parser_accepts_env_architecture(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "env",
                "anatase.yml",
                "i.anatase.org/anatase:rolling",
                "--arch",
                "amd64",
            ]
        )

        self.assertEqual(args.arch, "amd64")

    def test_parser_accepts_init_ci_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "ci",
                "init",
                "--cache-dir",
                "cache",
                "--version",
                "20260629",
                "--recreate",
                "anatase.yml",
            ]
        )

        self.assertEqual(args.ci_action, "init")
        self.assertEqual(args.manifests, [Path("anatase.yml")])
        self.assertEqual(args.cache_dir, Path("cache"))
        self.assertEqual(args.version, "20260629")
        self.assertTrue(args.recreate)

    def test_parser_accepts_prepare_ci_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "ci",
                "prepare",
                "--cache-dir",
                "cache",
                "--version",
                "20260629",
                "--ci",
                "--full",
                "--prefix",
                "rolling-",
                "--tag",
                "rolling",
                "--registry",
                "i.anatase.org",
                "--workers",
                "8",
                "anatase.yml",
            ]
        )

        self.assertEqual(args.ci_action, "prepare")
        self.assertEqual(args.manifests, [Path("anatase.yml")])
        self.assertEqual(args.cache_dir, Path("cache"))
        self.assertEqual(args.version, "20260629")
        self.assertTrue(args.ci)
        self.assertTrue(args.full)
        self.assertEqual(args.prefix, "rolling-")
        self.assertEqual(args.tag, "rolling")
        self.assertEqual(args.registry, "i.anatase.org")
        self.assertEqual(args.workers, 8)

    def test_parser_defaults_prepare_workers(self) -> None:
        args = build_parser().parse_args(["ci", "prepare", "anatase.yml"])

        self.assertEqual(args.workers, DEFAULT_PREPARE_WORKERS)
        self.assertEqual(args.prefix, "")
        self.assertEqual(args.tag, "latest")
        self.assertEqual(args.registry, "")
        self.assertFalse(args.ci)

    def test_parser_accepts_seed_ci_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "ci",
                "seed",
                "cache/ci/build.yml",
                "--workers",
                "8",
                "--buffer-ratio",
                "2.5",
            ]
        )

        self.assertEqual(args.ci_action, "seed")
        self.assertEqual(args.build_manifest, Path("cache/ci/build.yml"))
        self.assertEqual(args.workers, 8)
        self.assertEqual(args.buffer_ratio, 2.5)

    def test_parser_defaults_seed_ci_build_manifest(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["ci", "seed"])

        self.assertEqual(args.ci_action, "seed")
        self.assertIsNone(args.build_manifest)
        self.assertIsNone(args.cache_dir)
        self.assertEqual(args.workers, DEFAULT_PREPARE_WORKERS)
        self.assertIsNone(args.buffer_ratio)

    def test_parser_accepts_seed_ci_cache_dir(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["ci", "seed", "--cache-dir", "out-cache", "--autoremove"]
        )

        self.assertEqual(args.ci_action, "seed")
        self.assertIsNone(args.build_manifest)
        self.assertEqual(args.cache_dir, Path("out-cache"))
        self.assertTrue(args.autoremove)

    def test_parser_accepts_composable_ci_build_selectors(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "build",
                "first",
                "0",
                "--builds",
                "--images",
                "--flatpaks",
                "--upload",
                "--ci",
                "--cache",
                "--autoremove",
            ]
        )

        self.assertEqual(args.ci_action, "build")
        self.assertEqual(args.build_ids, ["first", "0"])
        self.assertTrue(args.builds)
        self.assertTrue(args.images)
        self.assertTrue(args.flatpaks)
        self.assertTrue(args.upload)
        self.assertTrue(args.ci)
        self.assertTrue(args.cache)
        self.assertTrue(args.autoremove)

    def test_parser_disables_ci_build_upload_by_default(self) -> None:
        args = build_parser().parse_args(["ci", "build", "image"])

        self.assertFalse(args.upload)
        self.assertFalse(args.ci)
        self.assertFalse(args.cache)

    def test_parser_accepts_composable_ci_upload_selectors(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "upload",
                "image-id",
                "flatpak-id",
                "--images",
                "--flatpaks",
                "--refresh",
                "--tag",
                "candidate",
                "--tag",
                "latest",
                "--tag",
                "stable",
                "--previous-manifest",
                "registry.example.test/anatase:stable",
                "--prefix",
                "rolling-",
            ]
        )

        self.assertEqual(args.ci_action, "upload")
        self.assertEqual(args.upload_ids, ["image-id", "flatpak-id"])
        self.assertTrue(args.images)
        self.assertTrue(args.flatpaks)
        self.assertTrue(args.refresh)
        self.assertEqual(args.tags, ["candidate", "latest", "stable"])
        self.assertEqual(
            args.previous_manifest,
            "registry.example.test/anatase:stable",
        )
        self.assertEqual(args.prefix, "rolling-")

    def test_parser_accepts_ci_promote_options(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "promote",
                "anatase.yml",
                "other.yml",
                "--images",
                "--flatpaks",
                "--refresh",
                "--arch",
                "x86_64",
                "--arch",
                "aarch64",
                "--prefix",
                "rolling-",
                "--from",
                "rolling",
                "--to",
                "stable",
            ]
        )

        self.assertEqual(args.ci_action, "promote")
        self.assertEqual(args.manifests, [Path("anatase.yml"), Path("other.yml")])
        self.assertTrue(args.images)
        self.assertTrue(args.flatpaks)
        self.assertTrue(args.refresh)
        self.assertEqual(args.arches, ["x86_64", "aarch64"])
        self.assertEqual(args.prefix, "rolling-")
        self.assertEqual(args.from_tag, "rolling")
        self.assertEqual(args.to_tag, "stable")

    def test_parser_accepts_composable_ci_remove_selectors(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "remove",
                "image-id",
                "flatpak-id",
                "--images",
                "--flatpaks",
            ]
        )

        self.assertEqual(args.ci_action, "remove")
        self.assertEqual(args.remove_ids, ["image-id", "flatpak-id"])
        self.assertTrue(args.images)
        self.assertTrue(args.flatpaks)

    def test_prepare_ci_rejects_unsupported_options(self) -> None:
        parser = build_parser()

        for option in ("--cache", "--cards-dir", "--no-ccache"):
            with self.subTest(option=option):
                argv = ["ci", "prepare", option]
                if option == "--cards-dir":
                    argv.append("cards")
                argv.append("anatase.yml")
                with patch("sys.stderr", new=StringIO()):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(argv)

    def test_ci_command_calls_prepare_ci(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "prepare",
                "--cache-dir",
                "cache",
                "--version",
                "20260629",
                "--ci",
                "--workers",
                "8",
                "--prefix",
                "rolling-",
                "--tag",
                "rolling",
                "--registry",
                "i.anatase.org",
                "anatase.yml",
            ]
        )

        with patch("ludos.__main__.prepare_ci") as create:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        create.assert_called_once_with(
            (Path("anatase.yml"),),
            cache_dir=Path("cache"),
            cache_version="20260629",
            ci=True,
            full=False,
            prefix="rolling-",
            tag="rolling",
            registry="i.anatase.org",
            workers=8,
        )

    def test_ci_command_calls_write_ci_env(self) -> None:
        args = build_parser().parse_args(
            ["ci", "env", "anatase.yml", "ghcr.io/test/anatase:latest"]
        )

        with patch("ludos.__main__.write_ci_env") as write:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        write.assert_called_once_with(
            Path("anatase.yml"),
            "ghcr.io/test/anatase:latest",
            label="org.opencontainers.image.version",
            arch=None,
        )

    def test_ci_command_calls_init_ci(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "init",
                "--cache-dir",
                "cache",
                "--version",
                "20260629",
                "anatase.yml",
            ]
        )

        with patch("ludos.__main__.init_ci") as init:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        init.assert_called_once_with(
            (Path("anatase.yml"),),
            cache_dir=Path("cache"),
            cache_version="20260629",
            recreate=False,
        )

    def test_ci_command_calls_seed_ci(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "seed",
                "cache/ci/build.yml",
                "--workers",
                "8",
                "--buffer-ratio",
                "2.5",
            ]
        )

        with patch("ludos.__main__.seed_ci") as seed:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        seed.assert_called_once_with(
            Path("cache/ci/build.yml"),
            cache_dir=None,
            autoremove=False,
            workers=8,
            buffer_ratio=2.5,
        )

    def test_ci_command_calls_seed_ci_with_default_build_manifest(self) -> None:
        args = build_parser().parse_args(["ci", "seed"])

        with patch("ludos.__main__.seed_ci") as seed:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        seed.assert_called_once_with(
            None,
            cache_dir=None,
            autoremove=False,
            workers=DEFAULT_PREPARE_WORKERS,
            buffer_ratio=DEFAULT_PREPARE_WORKERS * DEFAULT_SEED_BUFFER_RATIO,
        )

    def test_ci_command_calls_seed_ci_with_cache_dir_and_autoremove(self) -> None:
        args = build_parser().parse_args(
            ["ci", "seed", "--cache-dir", "out-cache", "--autoremove"]
        )

        with patch("ludos.__main__.seed_ci") as seed:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        seed.assert_called_once_with(
            None,
            cache_dir=Path("out-cache"),
            autoremove=True,
            workers=DEFAULT_PREPARE_WORKERS,
            buffer_ratio=DEFAULT_PREPARE_WORKERS * DEFAULT_SEED_BUFFER_RATIO,
        )

    def test_ci_command_calls_build_ci(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "build",
                "first",
                "0",
                "--images",
                "--upload",
                "--ci",
                "--cache",
                "--autoremove",
                "--ccache",
            ]
        )

        with patch("ludos.__main__.build_ci") as build:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        build.assert_called_once_with(
            ("first", "0"),
            builds=False,
            images=True,
            flatpaks=False,
            upload=True,
            ci=True,
            cache=True,
            autoremove=True,
            ccache=True,
        )

    def test_ci_command_calls_upload_ci(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "upload",
                "--tag",
                "candidate",
                "--tag",
                "stable",
                "image",
                "0",
                "--flatpaks",
                "--refresh",
                "--previous-manifest",
                "registry.example.test/anatase:stable",
                "--prefix",
                "rolling-",
            ]
        )

        with patch("ludos.__main__.upload_ci", return_value=0) as upload:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        upload.assert_called_once_with(
            ("image", "0"),
            images=False,
            flatpaks=True,
            refresh=True,
            tags=("candidate", "stable"),
            previous_manifest="registry.example.test/anatase:stable",
            prefix="rolling-",
        )

    def test_ci_command_calls_promote_ci(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "promote",
                "anatase.yml",
                "--refresh",
                "--arch",
                "x86_64",
                "--arch",
                "aarch64",
                "--prefix",
                "rolling-",
                "--from",
                "rolling",
                "--to",
                "stable",
            ]
        )

        with patch("ludos.__main__.promote_ci", return_value=0) as promote:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        promote.assert_called_once_with(
            (Path("anatase.yml"),),
            prefix="rolling-",
            from_tag="rolling",
            to_tag="stable",
            arches=("x86_64", "aarch64"),
            images=False,
            flatpaks=False,
            refresh=True,
        )

    def test_ci_command_calls_remove_ci(self) -> None:
        args = build_parser().parse_args(
            ["ci", "remove", "image", "0", "--flatpaks"]
        )

        with patch("ludos.__main__.remove_ci", return_value=0) as remove:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        remove.assert_called_once_with(
            ("image", "0"),
            images=False,
            flatpaks=True,
        )

    def test_main_returns_7_for_seed_disk_space_error(self) -> None:
        with (
            patch("sys.argv", ["ludos", "ci", "seed"]),
            patch("ludos.__main__._discover_project_config", return_value=None),
            patch("ludos.__main__.configure_logging"),
            patch(
                "ludos.__main__.seed_ci",
                side_effect=SeedDiskSpaceError("not enough space"),
            ),
            patch("ludos.__main__.error") as error,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 7)
        error.assert_called_once()


class CiEnvTests(unittest.TestCase):
    def test_manifest_tag_uses_generated_version_and_manifest_defaults(self) -> None:
        manifest = SimpleNamespace(
            env={"releasever": 44, "dist": "", "tag": "$version$dist"},
            releasever="$releasever",
            tag="$tag",
        )
        with (
            patch("ludos.model.Manifest.from_file", return_value=manifest),
            patch("ludos.ci._default_cache_version", return_value="20260713"),
        ):
            tag = _manifest_tag(Path("anatase.yml"))

        self.assertEqual(tag, "20260713")

    def test_writes_first_dist_from_scratch_when_label_equals_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("manifest", encoding="utf-8")
            env = root / ".env"
            env.write_text("old=value\ndist=.99\n", encoding="utf-8")
            with (
                patch("ludos.ci._manifest_tag", return_value="20260713"),
                patch(
                    "ludos.ci._default_cache_version", return_value="20260713"
                ),
                patch(
                    "ludos.ci._inspect_remote_labels",
                    return_value={
                        "org.opencontainers.image.version": "20260713"
                    },
                ),
            ):
                output = write_ci_env(manifest, "ghcr.io/test/anatase:latest")

            self.assertEqual(output, env)
            self.assertEqual(
                env.read_text(encoding="utf-8"),
                "version=20260713\ndist=.1\n",
            )

    def test_increments_existing_dist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            with (
                patch("ludos.ci._manifest_tag", return_value="20260713"),
                patch(
                    "ludos.ci._default_cache_version", return_value="20260713"
                ),
                patch(
                    "ludos.ci._inspect_remote_labels",
                    return_value={"custom.version": "20260713.8"},
                ),
            ):
                write_ci_env(
                    manifest,
                    "ghcr.io/test/anatase:latest",
                    label="custom.version",
                )

            self.assertEqual(
                (root / ".env").read_text(encoding="utf-8"),
                "version=20260713\ndist=.9\n",
            )

    def test_rejects_invalid_version_suffix(self) -> None:
        for version in ("20260713dev", "20260713.dev"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp:
                manifest = Path(temp) / "anatase.yml"
                with (
                    patch("ludos.ci._manifest_tag", return_value="20260713"),
                    patch(
                        "ludos.ci._inspect_remote_labels",
                        return_value={
                            "org.opencontainers.image.version": version
                        },
                    ),
                ):
                    with self.assertRaises(ConfigError):
                        write_ci_env(manifest, "ghcr.io/test/anatase:latest")

                self.assertFalse((Path(temp) / ".env").exists())

    def test_clears_dist_for_a_different_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            with (
                patch("ludos.ci._manifest_tag", return_value="20260713"),
                patch(
                    "ludos.ci._default_cache_version", return_value="20260713"
                ),
                patch(
                    "ludos.ci._inspect_remote_labels",
                    return_value={
                        "org.opencontainers.image.version": "20260706.3"
                    },
                ),
            ):
                write_ci_env(manifest, "ghcr.io/test/anatase:latest")

            self.assertEqual(
                (root / ".env").read_text(encoding="utf-8"),
                "version=20260713\ndist=\n",
            )

    def test_missing_remote_image_starts_without_dist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            with (
                patch("ludos.ci._manifest_tag", return_value="20260713"),
                patch(
                    "ludos.ci._default_cache_version", return_value="20260713"
                ),
                patch(
                    "ludos.ci._inspect_remote_labels",
                    side_effect=ConfigError("failed to inspect remote OCI image"),
                ),
                patch(
                    "ludos.ci._remote_cache_image_exists",
                    return_value=False,
                ) as exists,
            ):
                write_ci_env(manifest, "i.anatase.org/anatase:rolling")

            self.assertEqual(
                (root / ".env").read_text(encoding="utf-8"),
                "version=20260713\ndist=\n",
            )
            exists.assert_called_once_with("i.anatase.org/anatase:rolling")

    def test_rejects_missing_label(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "anatase.yml"
            with (
                patch("ludos.ci._manifest_tag", return_value="20260713"),
                patch("ludos.ci._inspect_remote_labels", return_value={}),
            ):
                with self.assertRaisesRegex(ConfigError, "has no"):
                    write_ci_env(manifest, "ghcr.io/test/anatase:latest")

    def test_inspects_remote_image_labels_with_skopeo(self) -> None:
        result = SimpleNamespace(
            returncode=0,
            stdout='{"Labels":{"org.opencontainers.image.version":"20260713.2"}}',
        )
        with (
            patch("ludos.ci.shutil.which", return_value="/usr/bin/skopeo"),
            patch("ludos.ci.subprocess.run", return_value=result) as run,
        ):
            labels = _inspect_remote_labels("ghcr.io/test/anatase:latest")

        self.assertEqual(
            labels,
            {"org.opencontainers.image.version": "20260713.2"},
        )
        run.assert_called_once_with(
            [
                "/usr/bin/skopeo",
                "inspect",
                "--no-tags",
                "docker://ghcr.io/test/anatase:latest",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_inspects_remote_image_labels_for_selected_architecture(self) -> None:
        result = SimpleNamespace(
            returncode=0,
            stdout='{"Labels":{"org.opencontainers.image.version":"20260713.2"}}',
        )
        with (
            patch("ludos.ci.shutil.which", return_value="/usr/bin/skopeo"),
            patch("ludos.ci.subprocess.run", return_value=result) as run,
        ):
            labels = _inspect_remote_labels(
                "i.anatase.org/anatase:rolling",
                arch="amd64",
            )

        self.assertEqual(
            labels,
            {"org.opencontainers.image.version": "20260713.2"},
        )
        run.assert_called_once_with(
            [
                "/usr/bin/skopeo",
                "inspect",
                "--no-tags",
                "--override-arch",
                "amd64",
                "docker://i.anatase.org/anatase:rolling",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_write_ci_env_passes_selected_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "anatase.yml"
            with (
                patch("ludos.ci._manifest_tag", return_value="20260713"),
                patch("ludos.ci._default_cache_version", return_value="20260713"),
                patch(
                    "ludos.ci._inspect_remote_labels",
                    return_value={
                        "org.opencontainers.image.version": "20260713"
                    },
                ) as inspect,
            ):
                write_ci_env(
                    manifest,
                    "i.anatase.org/anatase:rolling",
                    arch="amd64",
                )

        inspect.assert_called_once_with(
            "i.anatase.org/anatase:rolling",
            arch="amd64",
        )


class PrepareCiTests(unittest.TestCase):
    def test_ci_final_dependencies_are_lazy_and_digest_pinned(self) -> None:
        root = Path("/workspace")
        context = self._context(root)
        metadata = replace(
            self._metadata(root, context),
            oci_images=(
                replace(
                    self._metadata(root, context).oci_images[0],
                    declared_package_ids=(
                        ("kernel-core", "x86_64"),
                        ("kernel-common", "noarch"),
                    ),
                ),
            ),
        )

        with patch(
            "ludos.ci._inspect_remote_digest",
            side_effect=("sha256:card", "sha256:build"),
        ) as inspect:
            dependencies = _ci_final_dependency_images(metadata)

        self.assertEqual(
            dependencies,
            {
                "cards:f44-common": (
                    "ghcr.io/anatase-org/cards:f44-common@sha256:card"
                ),
                "builds:f44-base": (
                    "ghcr.io/anatase-org/builds:f44-base@sha256:build"
                ),
                "kernel:f44": (
                    "ghcr.io/anatase-org/kernel:f44@sha256:kernel111"
                ),
            },
        )
        self.assertCountEqual(
            inspect.call_args_list,
            [
                call("ghcr.io/anatase-org/cards:f44-common"),
                call("ghcr.io/anatase-org/builds:f44-base"),
            ],
        )

        build_outputs = _ci_final_build_outputs(metadata)
        self.assertEqual(
            build_outputs.images_by_block,
            (("base", "builds:f44-base"),),
        )
        self.assertEqual(
            build_outputs.rpm_globs_by_block,
            (("base", ("*.rpm",)),),
        )
        self.assertEqual(build_outputs.file_blocks, ("base",))

        oci_outputs = _ci_final_oci_outputs(metadata)
        self.assertEqual(
            oci_outputs.rpm_ids_by_index,
            (
                (
                    0,
                    (
                        ("kernel-core", "x86_64"),
                        ("kernel-common", "noarch"),
                    ),
                ),
            ),
        )
        self.assertEqual(oci_outputs.file_indexes, (0,))

    def test_remote_image_exists_uses_shared_registry_check(self) -> None:
        with patch("ludos.ci._remote_cache_image_exists", return_value=True) as remote:
            exists = _ci_remote_image_exists(
                "podman",
                "orchestrator:f44",
                "ghcr.io/anatase-org",
            )

        self.assertTrue(exists)
        remote.assert_called_once_with("ghcr.io/anatase-org/orchestrator:f44")

    def test_init_ci_creates_and_pushes_missing_init_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"

            def resolve(_manifest: Path, **kwargs: object) -> SimpleNamespace:
                image_exists = kwargs["image_exists"]
                create_orchestrator = kwargs["create_orchestrator_image"]
                create_repo = kwargs["create_repo_image"]
                self.assertFalse(
                    image_exists("podman", "orchestrator:f44", "ghcr.io/test")
                )
                create_orchestrator(
                    podman="podman",
                    buildah="buildah",
                    source="fedora:44",
                    image="orchestrator:f44",
                    packages=tuple(),
                )
                self.assertFalse(
                    image_exists("podman", "repos:f44-fedora", "ghcr.io/test")
                )
                create_repo(
                    podman="podman",
                    buildah="buildah",
                    orchestrator="orchestrator:f44",
                    root_dir=root,
                    image="repos:f44-fedora",
                    repo_name="fedora.repo",
                    repo_id="fedora",
                    rendered_repo="[fedora]\n",
                )
                return SimpleNamespace(ci_registry="ghcr.io/test")

            with (
                patch("ludos.ci.resolve_manifest_context", side_effect=resolve),
                patch("ludos.ci._ci_remote_image_exists", return_value=False),
                patch("ludos.ci._local_image_exists", return_value=False),
                patch("ludos.ci._create_orchestrator_image") as create_orchestrator,
                patch("ludos.ci._create_repo_image") as create_repo,
                patch("ludos.ci._push_ci_image") as push,
            ):
                init_ci((manifest,), cache_dir=cache, cache_version="20260629")

            create_orchestrator.assert_called_once()
            create_repo.assert_called_once()
            self.assertEqual(
                [call.args[1] for call in push.call_args_list],
                ["orchestrator:f44", "repos:f44-fedora"],
            )

    def test_init_ci_skips_remote_init_images_without_creating(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"

            def resolve(_manifest: Path, **kwargs: object) -> SimpleNamespace:
                image_exists = kwargs["image_exists"]
                self.assertTrue(
                    image_exists("podman", "orchestrator:f44", "ghcr.io/test")
                )
                self.assertTrue(
                    image_exists("podman", "repos:f44-fedora", "ghcr.io/test")
                )
                return SimpleNamespace(ci_registry="ghcr.io/test")

            with (
                patch("ludos.ci.resolve_manifest_context", side_effect=resolve),
                patch("ludos.ci._ci_remote_image_exists", return_value=True),
                patch("ludos.ci._local_image_exists", return_value=False),
                patch("ludos.ci._create_orchestrator_image") as create_orchestrator,
                patch("ludos.ci._create_repo_image") as create_repo,
                patch("ludos.ci._push_ci_image") as push,
            ):
                init_ci((manifest,), cache_dir=cache, cache_version="20260629")

            create_orchestrator.assert_not_called()
            create_repo.assert_not_called()
            push.assert_not_called()

    def test_init_ci_recreates_remote_orchestrator_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"

            def resolve(_manifest: Path, **kwargs: object) -> SimpleNamespace:
                image_exists = kwargs["image_exists"]
                create_orchestrator = kwargs["create_orchestrator_image"]
                self.assertFalse(
                    image_exists("podman", "orchestrator:f44", "ghcr.io/test")
                )
                create_orchestrator(
                    podman="podman",
                    buildah="buildah",
                    source="fedora:44",
                    image="orchestrator:f44",
                    packages=tuple(),
                )
                self.assertTrue(
                    image_exists("podman", "repos:f44-fedora", "ghcr.io/test")
                )
                return SimpleNamespace(ci_registry="ghcr.io/test")

            with (
                patch("ludos.ci.resolve_manifest_context", side_effect=resolve),
                patch("ludos.ci._ci_remote_image_exists", return_value=True),
                patch("ludos.ci._local_image_exists", return_value=False),
                patch("ludos.ci._create_orchestrator_image") as create_orchestrator,
                patch("ludos.ci._create_repo_image") as create_repo,
                patch("ludos.ci._push_ci_image") as push,
            ):
                init_ci(
                    (manifest,),
                    cache_dir=cache,
                    cache_version="20260629",
                    recreate=True,
                )

            create_orchestrator.assert_called_once()
            create_repo.assert_not_called()
            push.assert_not_called()

    def test_init_ci_pulls_remote_orchestrator_for_missing_repo_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"

            def resolve(_manifest: Path, **kwargs: object) -> SimpleNamespace:
                image_exists = kwargs["image_exists"]
                create_repo = kwargs["create_repo_image"]
                self.assertTrue(
                    image_exists("podman", "orchestrator:f44", "ghcr.io/test")
                )
                self.assertFalse(
                    image_exists("podman", "repos:f44-fedora", "ghcr.io/test")
                )
                create_repo(
                    podman="podman",
                    buildah="buildah",
                    orchestrator="orchestrator:f44",
                    root_dir=root,
                    image="repos:f44-fedora",
                    repo_name="fedora.repo",
                    repo_id="fedora",
                    rendered_repo="[fedora]\n",
                )
                return SimpleNamespace(ci_registry="ghcr.io/test")

            with (
                patch("ludos.ci.resolve_manifest_context", side_effect=resolve),
                patch(
                    "ludos.ci._ci_remote_image_exists",
                    side_effect=[True, False],
                ),
                patch("ludos.ci._local_image_exists", return_value=False),
                patch("ludos.ci._ensure_context_image", return_value=True) as ensure,
                patch("ludos.ci._create_orchestrator_image") as create_orchestrator,
                patch("ludos.ci._create_repo_image") as create_repo,
                patch("ludos.ci._push_ci_image") as push,
            ):
                init_ci((manifest,), cache_dir=cache, cache_version="20260629")

            ensure.assert_called_once_with(
                "podman",
                "orchestrator:f44",
                "ghcr.io/test",
            )
            create_orchestrator.assert_not_called()
            create_repo.assert_called_once()
            push.assert_called_once_with(
                "podman",
                "repos:f44-fedora",
                "ghcr.io/test",
            )

    def test_prepare_ci_writes_build_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            metadata = replace(
                self._metadata(root, context),
                cache_card_envs=(("base", (("tag", "20260713"),)),),
            )
            ci_metadata = _metadata_with_final_image(metadata, mode="separated")
            image_id = ci_metadata.output_image.rsplit(":", 1)[-1]
            plan = self._flatpak_plan(root, cache)

            with (
                patch(
                    "ludos.ci.resolve_build_manifest_context",
                    return_value=context,
                ) as resolve_context,
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ) as resolve_build,
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=(plan,),
                ) as plan_flatpaks,
                patch(
                    "ludos.ci._ci_remote_image_exists",
                    return_value=False,
                ) as remote_exists,
                patch("ludos.ci._remove_tree") as remove_tree,
                patch("ludos.ci.log") as log,
            ):
                output = prepare_ci(
                    (manifest,),
                    cache_dir=cache,
                    cache_version="20260629",
                )

            self.assertEqual(output, cache.resolve() / "ci" / "build.yml")
            resolve_context.assert_called_once_with(
                manifest,
                cache_dir=cache.resolve(),
                cache_version="20260629",
                cache_only=True,
                ccache=False,
                dnf_workspace_dir=cache.resolve() / "ci" / "dnf" / "0-anatase",
            )
            resolve_build.assert_called_once_with(
                ((manifest, context),),
                cache_only=False,
                workers=DEFAULT_PREPARE_WORKERS,
            )
            plan_flatpaks.assert_called_once_with(
                context,
                manifest_path=manifest,
                cache_only=False,
                workers=DEFAULT_PREPARE_WORKERS,
            )
            self.assertCountEqual(
                remote_exists.call_args_list,
                [
                    call(
                        ci_metadata.podman,
                        ci_metadata.output_image,
                        "ghcr.io/anatase-org",
                    ),
                    call(context.podman, plan.output_image, "ghcr.io/anatase-org"),
                    call(metadata.podman, "cards:f44-common", "ghcr.io/anatase-org"),
                    call(metadata.podman, "builders:f44-base", "ghcr.io/anatase-org"),
                    call(metadata.podman, "builds:f44-base", "ghcr.io/anatase-org"),
                    call(
                        context.podman,
                        "builders:f44-flatpak-builder",
                        "ghcr.io/anatase-org",
                    ),
                    call(
                        context.podman,
                        "builds:f44-flatpak-kate-build",
                        "ghcr.io/anatase-org",
                    ),
                ],
            )
            remove_tree.assert_called_once_with(
                cache.resolve() / "ci" / "dnf" / "0-anatase",
                podman="podman",
            )

            data = yaml.safe_load(output.read_text(encoding="utf-8"))
            encoded = output.with_suffix(".yml.encoded").read_bytes()
            self.assertNotIn(b"\n", encoded)
            self.assertEqual(
                lzma.decompress(base64.b64decode(encoded)),
                output.read_bytes(),
            )
            log.assert_any_call(f"Wrote CI build manifest: {output}")
            log.assert_any_call("Checking current registry for image existence")
            decision_messages = [
                item.args[0]
                for item in log.call_args_list
                if item.args
                and item.args[0].startswith(("Reusing ", "Creating "))
                and item.args[0].endswith(" Image")
            ]
            self.assertCountEqual(
                decision_messages,
                [
                    f"Creating {ci_metadata.output_image} Image",
                    "Creating flatpaks:f44-kate-output Image",
                    "Creating cards:f44-common Image",
                    "Creating builders:f44-base Image",
                    "Creating builds:f44-base Image",
                    "Creating builders:f44-flatpak-builder Image",
                    "Creating builds:f44-flatpak-kate-build Image",
                ],
            )
            log.assert_any_call(
                "Wrote encoded CI build manifest: "
                f"{output.with_suffix('.yml.encoded')} "
                f"({(len(encoded) + 1023) // 1024} KiB)"
            )
            self.assertEqual(data["version"], 1)
            self.assertNotIn("manifests", data)
            self.assertEqual(
                data["cards"]["f44-common"]["image"],
                "cards:f44-common",
            )
            self.assertEqual(
                data["cards"]["f44-common"]["packages"],
                ["bash-0:1-1.fc44.x86_64"],
            )
            self.assertEqual(
                data["builders"]["f44-base"]["builder_packages"],
                ["rpm-build-0:1-1.fc44.x86_64"],
            )
            self.assertEqual(
                data["builders"]["f44-base"]["build_image"],
                "builds:f44-base",
            )
            self.assertEqual(
                data["builds"]["f44-base"]["builder_image"],
                "builders:f44-base",
            )
            self.assertEqual(
                data["builders"]["f44-flatpak-builder"]["flatpak"]["app"],
                "kate",
            )
            self.assertEqual(
                data["builds"]["f44-flatpak-kate-build"]["flatpak"]["specs"][0][
                    "spec"
                ],
                "kate.spec",
            )
            self.assertEqual(
                data["builds"]["f44-base"]["metadata"]["output_image"],
                ci_metadata.output_image,
            )
            self.assertEqual(data["images"][image_id]["path"], str(manifest))
            self.assertEqual(
                data["images"][image_id]["build"]["package_images"][
                    "f44-common"
                ]["block"],
                "common",
            )
            self.assertEqual(
                data["images"][image_id]["build"]["build_images"][
                    "f44-base"
                ]["block"],
                "base",
            )
            self.assertEqual(
                data["images"][image_id]["build"]["oci_images"]["kernel-f44"][
                    "image"
                ],
                "kernel:f44@sha256:kernel111",
            )
            self.assertEqual(
                data["images"][image_id]["build"]["oci_images"]["kernel-f44"][
                    "tagged_image"
                ],
                "kernel:f44",
            )
            self.assertNotIn(
                "requested_packages",
                data["images"][image_id]["build"],
            )
            self.assertNotIn(
                "resolved_packages",
                data["images"][image_id]["build"],
            )
            self.assertNotIn(
                "ccache_dir",
                data["images"][image_id]["build"],
            )
            self.assertEqual(
                data["flatpaks"]["f44-kate-output"]["source"],
                "flatpaks/kate/card.yaml",
            )
            self.assertEqual(
                data["flatpaks"]["f44-kate-output"]["ref"],
                "app/org.anatase.TextEditor/x86_64/stable",
            )
            self.assertEqual(
                data["flatpaks"]["f44-kate-output"]["images"]["builder"],
                "builders:f44-flatpak-builder",
            )
            self.assertEqual(
                data["flatpaks"]["f44-kate-output"]["specs"][0]["spec"],
                "kate.spec",
            )
            self.assertEqual(
                data["flatpaks"]["f44-kate-output"]["build"],
                data["images"][image_id]["build"],
            )
            self.assertIsInstance(
                data["flatpaks"]["f44-kate-output"]["flatpak_images"],
                dict,
            )
            source = output.read_text(encoding="utf-8")
            self.assertNotIn("ccache_dir:", source)
            self.assertIn("&id", source)
            self.assertIn("*id", source)
            restored = _metadata_from_seed_entry(
                output,
                image_id,
                data["images"][image_id],
            )
            self.assertEqual(restored.output_image, ci_metadata.output_image)
            self.assertEqual(restored.package_images, ci_metadata.package_images)
            self.assertEqual(restored.cache_card_envs, ci_metadata.cache_card_envs)
            self.assertIsNone(restored.ccache_dir)
            persistent_ccache = root / "persistent-ccache"
            stale_entry = {
                "build": dict(data["images"][image_id]["build"]),
            }
            stale_entry["build"]["ccache_dir"] = "/serialized/ccache"
            with patch.dict(
                "ludos.ci.os.environ",
                {"CCACHE_DIR": str(persistent_ccache)},
            ):
                restored_with_ccache = _metadata_from_seed_entry(
                    output,
                    image_id,
                    stale_entry,
                    ccache=True,
                )
            self.assertEqual(
                restored_with_ccache.ccache_dir,
                str(persistent_ccache),
            )
            self.assertTrue(persistent_ccache.is_dir())
            self.assertEqual(restored.build_images, ci_metadata.build_images)
            self.assertEqual(restored.oci_images, ci_metadata.oci_images)

    def test_prepare_ci_uses_combined_final_image_metadata_with_ci_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            output = cache / "ci" / "build.yml"
            encoded = output.with_suffix(".yml.encoded")
            encoded.parent.mkdir(parents=True)
            encoded.write_bytes(b"encoded")
            context = self._context(root)
            metadata = self._metadata(root, context)

            with (
                patch(
                    "ludos.ci.resolve_build_manifest_context",
                    return_value=context,
                ),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ),
                patch(
                    "ludos.ci._metadata_with_final_image",
                    wraps=_metadata_with_final_image,
                ) as final_metadata,
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=tuple(),
                ),
                patch(
                    "ludos.ci._write_ci_build_manifest",
                    return_value=(output, encoded),
                ),
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                result = prepare_ci((manifest,), cache_dir=cache, ci=True)

        self.assertEqual(result, output)
        final_metadata.assert_called_once_with(metadata, mode="combined")

    def test_prepare_ci_drops_already_built_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            metadata = self._metadata(root, context)
            plan = self._flatpak_plan(root, cache)

            with (
                patch("ludos.ci.resolve_build_manifest_context", return_value=context),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ),
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=(plan,),
                ),
                patch("ludos.ci._ci_remote_image_exists", return_value=True),
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                output = prepare_ci((manifest,), cache_dir=cache)

            data = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertEqual(data["cards"], {})
            self.assertEqual(data["builders"], {})
            self.assertEqual(data["builds"], {})
            self.assertEqual(data["images"], {})
            self.assertEqual(data["flatpaks"], {})

    def test_prepare_ci_inlines_metadata_when_only_flatpak_remains(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            metadata = self._metadata(root, context)
            plan = self._flatpak_plan(root, cache)

            def remote_exists(_podman: str, image: str, _registry: str) -> bool:
                return image != plan.output_image

            with (
                patch("ludos.ci.resolve_build_manifest_context", return_value=context),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ),
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=(plan,),
                ),
                patch(
                    "ludos.ci._ci_remote_image_exists",
                    side_effect=remote_exists,
                ),
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                output = prepare_ci((manifest,), cache_dir=cache)

            source = output.read_text(encoding="utf-8")
            data = yaml.safe_load(source)
            self.assertEqual(data["cards"], {})
            self.assertEqual(data["builders"], {})
            self.assertEqual(data["builds"], {})
            self.assertEqual(data["images"], {})
            self.assertEqual(tuple(data["flatpaks"]), ("f44-kate-output",))
            self.assertIsInstance(
                data["flatpaks"]["f44-kate-output"]["build"],
                dict,
            )
            self.assertNotIn("*id", source)

    def test_prepare_ci_lists_missing_dependencies_for_existing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            metadata = self._metadata(root, context)
            ci_metadata = _metadata_with_final_image(metadata, mode="separated")
            plan = self._flatpak_plan(root, cache)

            def remote_exists(_podman: str, image: str, _registry: str) -> bool:
                return image in {ci_metadata.output_image, plan.output_image}

            with (
                patch("ludos.ci.resolve_build_manifest_context", return_value=context),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ),
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=(plan,),
                ),
                patch(
                    "ludos.ci._ci_remote_image_exists",
                    side_effect=remote_exists,
                ),
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                output = prepare_ci((manifest,), cache_dir=cache)

            data = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertEqual(data["images"], {})
            self.assertEqual(data["flatpaks"], {})
            self.assertEqual(
                tuple(data["cards"]),
                ("f44-common",),
            )
            self.assertEqual(
                tuple(data["builders"]),
                ("f44-base", "f44-flatpak-builder"),
            )
            self.assertEqual(
                tuple(data["builds"]),
                ("f44-base", "f44-flatpak-kate-build"),
            )
            self.assertEqual(
                data["cards"]["f44-common"]["manifest"],
                str(manifest),
            )
            self.assertEqual(
                data["builders"]["f44-base"]["builder_packages"],
                ["rpm-build-0:1-1.fc44.x86_64"],
            )
            self.assertEqual(
                data["builds"]["f44-base"]["block"],
                "base",
            )
            self.assertEqual(
                data["builds"]["f44-flatpak-kate-build"]["flatpak"]["app"],
                "kate",
            )
            self.assertEqual(
                data["builds"]["f44-base"]["metadata"]["output_image"],
                ci_metadata.output_image,
            )

    def test_prepare_ci_full_keeps_already_built_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            metadata = self._metadata(root, context)
            ci_metadata = _metadata_with_final_image(metadata, mode="separated")
            plan = self._flatpak_plan(root, cache)

            with (
                patch("ludos.ci.resolve_build_manifest_context", return_value=context),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ),
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=(plan,),
                ),
                patch("ludos.ci._ci_remote_image_exists", return_value=True) as remote_exists,
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                output = prepare_ci((manifest,), cache_dir=cache, full=True)

            data = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertIn(
                ci_metadata.output_image.rsplit(":", 1)[-1],
                data["images"],
            )
            self.assertIn("f44-kate-output", data["flatpaks"])
            self.assertEqual(data["cards"], {})
            self.assertEqual(data["builders"], {})
            self.assertEqual(data["builds"], {})
            self.assertCountEqual(
                remote_exists.call_args_list,
                [
                    call(metadata.podman, "cards:f44-common", "ghcr.io/anatase-org"),
                    call(metadata.podman, "builders:f44-base", "ghcr.io/anatase-org"),
                    call(metadata.podman, "builds:f44-base", "ghcr.io/anatase-org"),
                    call(
                        context.podman,
                        "builders:f44-flatpak-builder",
                        "ghcr.io/anatase-org",
                    ),
                    call(
                        context.podman,
                        "builds:f44-flatpak-kate-build",
                        "ghcr.io/anatase-org",
                    ),
                ],
            )

    def test_prepare_ci_checks_remote_cache_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            context.ci_registry = "ghcr.io/anatase-org"
            metadata = replace(
                self._metadata(root, context),
                ci_registry="ghcr.io/anatase-org",
            )
            ci_metadata = _metadata_with_final_image(metadata, mode="separated")
            plan = self._flatpak_plan(root, cache)

            with (
                patch("ludos.ci.resolve_build_manifest_context", return_value=context),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ),
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=(plan,),
                ),
                patch(
                    "ludos.ci._ci_remote_image_exists",
                    return_value=False,
                ) as remote_exists,
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                prepare_ci((manifest,), cache_dir=cache)

        self.assertCountEqual(
            remote_exists.call_args_list,
            [
                call(
                    context.podman,
                    ci_metadata.output_image,
                    "ghcr.io/anatase-org",
                ),
                call(context.podman, plan.output_image, "ghcr.io/anatase-org"),
                call(context.podman, "cards:f44-common", "ghcr.io/anatase-org"),
                call(context.podman, "builders:f44-base", "ghcr.io/anatase-org"),
                call(context.podman, "builds:f44-base", "ghcr.io/anatase-org"),
                call(
                    context.podman,
                    "builders:f44-flatpak-builder",
                    "ghcr.io/anatase-org",
                ),
                call(
                    context.podman,
                    "builds:f44-flatpak-kate-build",
                    "ghcr.io/anatase-org",
                ),
            ],
        )

    def test_prepare_ci_compares_published_ludos_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            metadata = self._metadata(root, context)
            ci_metadata = _metadata_with_final_image(metadata, mode="separated")
            plan = self._flatpak_plan(root, cache)

            def inspect(ref: str) -> dict[str, str]:
                if ref == "i.anatase.org/anatase:rolling":
                    return {
                        "org.anatase.ludos.tag": ci_metadata.output_image.rsplit(
                            ":", 1
                        )[-1]
                    }
                return {"org.anatase.ludos.tag": "old-flatpak-output"}

            with (
                patch("ludos.ci.resolve_build_manifest_context", return_value=context),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ),
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=(plan,),
                ),
                patch(
                    "ludos.ci._remote_cache_image_exists",
                    return_value=True,
                ) as published_exists,
                patch(
                    "ludos.ci._inspect_remote_labels",
                    side_effect=inspect,
                ) as inspect_labels,
                patch(
                    "ludos.ci._ci_remote_image_exists",
                    return_value=True,
                ),
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                output = prepare_ci(
                    (manifest,),
                    cache_dir=cache,
                    prefix="rolling-",
                    tag="rolling",
                    registry="i.anatase.org/",
                )
            data = yaml.safe_load(output.read_text(encoding="utf-8"))

        self.assertEqual(data["images"], {})
        self.assertEqual(tuple(data["flatpaks"]), ("f44-kate-output",))
        expected_refs = [
            "i.anatase.org/anatase:rolling",
            "i.anatase.org/flatpaks/kate:rolling-f44-x86_64",
        ]
        self.assertEqual(
            [item.args[0] for item in published_exists.call_args_list],
            expected_refs,
        )
        self.assertEqual(
            [item.args[0] for item in inspect_labels.call_args_list],
            expected_refs,
        )

    def test_prepare_ci_reuses_matching_hash_despite_changed_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            metadata = replace(
                self._metadata(root, context),
                manifest_labels=(
                    ("org.opencontainers.image.version", "20260713.13"),
                ),
                cache_manifest_labels=(
                    ("org.opencontainers.image.version", "20260713"),
                ),
            )
            ci_metadata = _metadata_with_final_image(metadata, mode="separated")

            with (
                patch("ludos.ci.resolve_build_manifest_context", return_value=context),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    return_value=(metadata,),
                ),
                patch(
                    "ludos.ci.plan_manifest_flatpaks_with_context",
                    return_value=tuple(),
                ),
                patch("ludos.ci._remote_cache_image_exists", return_value=True),
                patch(
                    "ludos.ci._inspect_remote_labels",
                    return_value={
                        "org.anatase.ludos.tag": ci_metadata.output_image.rsplit(
                            ":", 1
                        )[-1],
                        "org.opencontainers.image.version": "20260713.12",
                    },
                ),
                patch("ludos.ci._ci_remote_image_exists", return_value=True),
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                output = prepare_ci(
                    (manifest,),
                    cache_dir=cache,
                    tag="rolling",
                    registry="i.anatase.org/",
                )
            data = yaml.safe_load(output.read_text(encoding="utf-8"))

        self.assertEqual(data["images"], {})

    def test_prepare_ci_keeps_existing_output_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            output = cache / "ci" / "build.yml"
            output.parent.mkdir(parents=True)
            output.write_text("existing: true\n", encoding="utf-8")
            context = self._context(root)

            with (
                patch("ludos.ci.resolve_build_manifest_context", return_value=context),
                patch(
                    "ludos.ci.resolve_build_manifests_from_contexts",
                    side_effect=ConfigError("boom"),
                ),
                patch("ludos.ci._remove_tree"),
            ):
                with self.assertRaisesRegex(ConfigError, "boom"):
                    prepare_ci((manifest,), cache_dir=cache)

            self.assertEqual(output.read_text(encoding="utf-8"), "existing: true\n")

    def test_prepare_ci_requires_manifest(self) -> None:
        with self.assertRaisesRegex(ConfigError, "at least one manifest"):
            prepare_ci(tuple())

    def test_prepare_ci_requires_positive_workers(self) -> None:
        with self.assertRaisesRegex(
            ConfigError,
            "workers must be a positive integer",
        ):
            prepare_ci((Path("anatase.yml"),), workers=0)

    def _context(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            root_dir=root,
            distro="f44-x86_64",
            dnf_workspace_dir=root / "cache" / "f44-x86_64" / "dnf" / "run-test",
            podman="podman",
            ci_registry="ghcr.io/anatase-org",
        )

    def _flatpak_plan(self, root: Path, cache: Path) -> SimpleNamespace:
        flatpak_dir = root / "flatpaks" / "kate"
        return SimpleNamespace(
            card_path=flatpak_dir / "card.yaml",
            flatpak_dir=flatpak_dir,
            app_name="kate",
            block="flatpak-kate",
            app_ref="app/org.anatase.TextEditor/x86_64/stable",
            branch="stable",
            flatpak_arch="x86_64",
            output_image="flatpaks:f44-kate-output",
            latest_image="flatpaks:kate",
            build_image="builds:f44-flatpak-kate-build",
            builder_image="builders:f44-flatpak-builder",
            spec_build_dir=cache / "f44-x86_64" / "flatpaks" / "kate" / "spec-build",
            artifact_cache_dir=cache / "f44-x86_64" / "build-artifacts" / "flatpaks" / "kate",
            final_build_dir=cache / "f44-x86_64" / "build" / "flatpaks" / "kate",
            build_env={"arch": "x86_64", "releasever": "44"},
            substitution_env={"arch": "x86_64", "releasever": "44"},
            specs=(
                SpecBuild(
                    spec="kate.spec",
                    packages={"*": ("kate",)},
                ),
            ),
            spec_revisions=(("kate.spec", "abc123"),),
            prepare_script="true",
            builder_packages=("rpm-build-0:1-1.fc44.x86_64",),
            rpmbuild_defines=("--define", "_flatpak 1"),
            metadata="[Application]\nname=org.anatase.TextEditor\n",
        )

    def _metadata(
        self,
        root: Path,
        context: SimpleNamespace,
    ) -> ResolvedBuildMetadata:
        cache = root / "cache" / "f44-x86_64"
        return ResolvedBuildMetadata(
            image="anatase",
            distro="f44-x86_64",
            releasever="44",
            arch="x86_64",
            root_dir=str(root),
            local_prefix="",
            orchestrator="orchestrator:f44",
            output_image="images:f44-anatase",
            manifest_labels=tuple(),
            manifest_env=(("arch", "x86_64"),),
            requested_packages=tuple(),
            resolved_packages=tuple(),
            common_packages=tuple(),
            bootstrap_packages=tuple(),
            card_order=tuple(),
            card_packages=tuple(),
            card_resolutions=tuple(),
            package_ids=tuple(),
            package_images=(
                PackageImagePlan(
                    block="common",
                    packages=("bash-0:1-1.fc44.x86_64",),
                    image="cards:f44-common",
                ),
            ),
            build_images=(
                BuildImagePlan(
                    block="base",
                    image="builds:f44-base",
                    builder_image="builders:f44-base",
                    builder_packages=("rpm-build-0:1-1.fc44.x86_64",),
                ),
            ),
            oci_images=(
                OciImagePlan(
                    block="base",
                    name="kernel",
                    image="kernel:f44",
                    digest="sha256:kernel111",
                    packages=("kernel-core",),
                ),
            ),
            package_dir=str(cache / "packages"),
            repo_dir=str(context.dnf_workspace_dir / "repos"),
            cache_dir=str(cache),
            build_dir=str(cache / "build" / "anatase"),
            card_build_dir=str(cache / "cards"),
            spec_source_cache_dir=str(root / "cache" / "spec-sources" / "git"),
            build_artifact_cache_dir=str(cache / "build-artifacts"),
            ccache_dir=None,
            dnf_workspace_dir=str(context.dnf_workspace_dir),
            dnf_cache_dir=str(context.dnf_workspace_dir / "cache"),
            dnf_persist_dir=str(context.dnf_workspace_dir / "persist"),
            dnf_log_dir=str(context.dnf_workspace_dir / "log"),
            dnf_resolve_dir=str(cache / "dnf" / "resolves"),
            podman=context.podman,
            buildah=None,
            cache_version="20260629",
            repo_images=tuple(),
            orchestrator_dnf_base=tuple(),
            package_blocks=tuple(),
            card_file_sets=tuple(),
            postprocess_blocks=tuple(),
            card_envs=tuple(),
            card_sources=tuple(),
            card_prepare_scripts=tuple(),
            card_builds=tuple(),
            card_specs=tuple(),
            spec_source_revisions=tuple(),
            latest_image="images:anatase",
            ci_registry=getattr(context, "ci_registry", ""),
        )


class BuildCiTests(unittest.TestCase):
    def test_restore_ci_oci_image_uses_ci_registry(self) -> None:
        metadata = SimpleNamespace(
            podman="podman",
            root_dir="/workspace",
            repo_images=tuple(),
            orchestrator="orchestrator:f44",
            ci_registry="ghcr.io/anatase-org",
            oci_images=(
                SimpleNamespace(image="kernel:f44-x86_64"),
            ),
        )
        restored_contexts = {
            (metadata.podman, metadata.root_dir, metadata.repo_images),
        }

        with patch("ludos.ci._ensure_image", return_value=True) as ensure:
            _restore_ci_build_context(
                metadata,
                restored_contexts,
                oci_images=True,
            )

        ensure.assert_called_once_with(
            "podman",
            "kernel:f44-x86_64",
            "ghcr.io/anatase-org",
        )

    def test_rebase_ci_entry_uses_current_checkout_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            prepared_root = base / "hosted" / "work" / "anatase"
            prepared_cache = base / "hosted" / "cache"
            current_root = base / "self-hosted" / "work" / "anatase"
            build_manifest = current_root / "cache" / "ci" / "build.yml"
            entry = {
                "metadata": {
                    "root_dir": str(prepared_root),
                    "cache_dir": str(prepared_cache / "f44-x86_64"),
                    "repo_dir": str(prepared_cache / "ci" / "dnf" / "repos"),
                    "card_sources": [
                        ["base-scx", str(prepared_root / "cards" / "base" / "scx" / "card.yml")]
                    ],
                    "card_file_sets": [
                        [
                            "base-scx",
                            str(prepared_root / "cards" / "base" / "scx" / "card.yml"),
                            [],
                        ]
                    ],
                    "orchestrator_dnf_base": [
                        f"{prepared_root}/repos:/workspace/repos:ro"
                    ],
                },
                "flatpak": {
                    "source": "flatpaks/okular/card.yaml",
                    "paths": {
                        "flatpak_dir": str(prepared_root / "flatpaks" / "okular"),
                        "spec_build_dir": str(
                            prepared_cache / "f44-x86_64" / "flatpaks" / "okular"
                        ),
                    },
                    "upstream": "https://example.com/source",
                },
            }

            with patch("ludos.ci.Path.cwd", return_value=current_root):
                rebased = _rebase_ci_entry(
                    build_manifest,
                    entry,
                    metadata_key="metadata",
                )

        self.assertIsInstance(rebased, dict)
        metadata = rebased["metadata"]
        flatpak = rebased["flatpak"]
        self.assertEqual(metadata["root_dir"], str(current_root))
        self.assertEqual(
            metadata["repo_dir"],
            str(current_root / "cache" / "ci" / "dnf" / "repos"),
        )
        self.assertEqual(
            metadata["card_sources"][0][1],
            str(current_root / "cards" / "base" / "scx" / "card.yml"),
        )
        self.assertEqual(
            metadata["card_file_sets"][0][1],
            str(current_root / "cards" / "base" / "scx" / "card.yml"),
        )
        self.assertEqual(
            metadata["orchestrator_dnf_base"][0],
            f"{current_root}/repos:/workspace/repos:ro",
        )
        self.assertEqual(
            flatpak["paths"]["flatpak_dir"],
            str(current_root / "flatpaks" / "okular"),
        )
        self.assertEqual(
            flatpak["paths"]["spec_build_dir"],
            str(current_root / "cache" / "f44-x86_64" / "flatpaks" / "okular"),
        )
        self.assertEqual(flatpak["upstream"], "https://example.com/source")

    def test_build_ci_runs_composable_selection_in_dependency_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = Path(temp) / "build.yml"
            build_manifest.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "builds": {"a": {}, "b": {}},
                        "images": {"image": {}},
                        "flatpaks": {"flatpak": {}},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            calls = []

            def record(section: str):
                def run(_manifest, build_id, _entry, **_kwargs):
                    calls.append(
                        (
                            section,
                            build_id,
                            _kwargs["upload"],
                            _kwargs.get("ci"),
                            _kwargs.get("cache"),
                            _kwargs["ccache"],
                        )
                    )

                return run

            with (
                patch("ludos.ci._build_ci_package", side_effect=record("build")),
                patch(
                    "ludos.ci._build_ci_manifest_image",
                    side_effect=record("image"),
                ),
                patch("ludos.ci._build_ci_flatpak", side_effect=record("flatpak")),
            ):
                build_ci(
                    ("image", "a", "0", "a"),
                    build_manifest=build_manifest,
                    builds=True,
                    flatpaks=True,
                    upload=True,
                    ci=True,
                    cache=True,
                    ccache=True,
                )

        self.assertEqual(
            calls,
            [
                ("build", "a", True, None, None, True),
                ("build", "b", True, None, None, True),
                ("image", "image", True, True, True, True),
                ("flatpak", "flatpak", True, None, None, True),
            ],
        )

    def test_build_ci_requires_a_selector(self) -> None:
        with self.assertRaisesRegex(ConfigError, "at least one CI build ID"):
            build_ci(tuple())

    def test_build_ci_rejects_unknown_and_ambiguous_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = Path(temp) / "build.yml"
            build_manifest.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "builds": {"duplicate": {}},
                        "images": {"duplicate": {}},
                        "flatpaks": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "unknown CI build ID"):
                build_ci(("missing",), build_manifest=build_manifest)
            with self.assertRaisesRegex(ConfigError, "ambiguous CI build ID"):
                build_ci(("duplicate",), build_manifest=build_manifest)

    def test_build_ci_zero_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = Path(temp) / "build.yml"
            build_manifest.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "builds": {},
                        "images": {},
                        "flatpaks": {},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("ludos.ci._build_ci_package") as package,
                patch("ludos.ci._build_ci_manifest_image") as image,
                patch("ludos.ci._build_ci_flatpak") as flatpak,
            ):
                build_ci(("0",), build_manifest=build_manifest)

        package.assert_not_called()
        image.assert_not_called()
        flatpak.assert_not_called()

    def test_build_ci_autoremoves_dependencies_after_building_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = Path(temp) / "build.yml"
            build_manifest.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "builds": {},
                        "images": {"image": {}},
                        "flatpaks": {},
                    }
                ),
                encoding="utf-8",
            )
            events = []

            def build(_manifest, _build_id, _entry, **kwargs):
                events.append("build")
                kwargs["cleanup_images"].add(
                    ("podman", "builds:f44-base", "registry.example")
                )

            def remove(images):
                events.append(("remove", images.copy()))

            with (
                patch("ludos.ci._build_ci_manifest_image", side_effect=build),
                patch(
                    "ludos.ci._remove_ci_dependency_images",
                    side_effect=remove,
                ),
            ):
                build_ci(
                    ("image",),
                    build_manifest=build_manifest,
                    autoremove=True,
                )

        self.assertEqual(
            events,
            [
                "build",
                (
                    "remove",
                    {("podman", "builds:f44-base", "registry.example")},
                ),
            ],
        )

    def test_build_ci_does_not_autoremove_dependencies_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = Path(temp) / "build.yml"
            build_manifest.write_text(
                yaml.safe_dump(
                    {
                        "version": 1,
                        "builds": {},
                        "images": {"image": {}},
                        "flatpaks": {},
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch(
                    "ludos.ci._build_ci_manifest_image",
                    side_effect=ConfigError("build failed"),
                ),
                patch("ludos.ci._remove_ci_dependency_images") as remove,
            ):
                with self.assertRaisesRegex(ConfigError, "build failed"):
                    build_ci(
                        ("image",),
                        build_manifest=build_manifest,
                        autoremove=True,
                    )

        remove.assert_not_called()

    def test_manifest_build_uses_lazy_pinned_dependencies(self) -> None:
        metadata = SimpleNamespace(
            podman="podman",
            ci_registry="registry.example",
            output_image="images:f44-anatase-output",
            package_images=(SimpleNamespace(image="cards:f44-base"),),
            build_images=(SimpleNamespace(image="builds:f44-base"),),
        )
        result = SimpleNamespace(
            output_image=metadata.output_image,
            latest_image="images:anatase",
        )
        cleanup_images = set()
        build_outputs = object()
        oci_outputs = object()
        dependency_images = {
            "cards:f44-base": "registry.example/cards:f44-base@sha256:card",
            "builds:f44-base": "registry.example/builds:f44-base@sha256:build",
        }
        with (
            patch("ludos.ci._metadata_from_seed_entry", return_value=metadata),
            patch(
                "ludos.ci._metadata_with_final_image",
                return_value=metadata,
            ) as final_metadata,
            patch("ludos.ci._restore_ci_build_context") as restore,
            patch(
                "ludos.ci._ci_final_dependency_images",
                return_value=dependency_images,
            ),
            patch(
                "ludos.ci._ci_final_build_outputs",
                return_value=build_outputs,
            ),
            patch(
                "ludos.ci._ci_final_oci_outputs",
                return_value=oci_outputs,
            ),
            patch("ludos.ci.build_build_images") as eager_build_outputs,
            patch(
                "ludos.ci.build_final_manifest_images",
                return_value=(result,),
            ) as final_build,
            patch("ludos.ci._upload_ci_output") as upload,
        ):
            _build_ci_manifest_image(
                Path("cache/ci/build.yml"),
                "anatase",
                {"build": {}},
                restored_contexts=set(),
                cleanup_images=cleanup_images,
                cache=True,
                autoremove=False,
            )

        self.assertEqual(
            cleanup_images,
            {
                (
                    "podman",
                    "registry.example/cards:f44-base@sha256:card",
                    "",
                ),
                (
                    "podman",
                    "registry.example/builds:f44-base@sha256:build",
                    "",
                ),
            },
        )
        eager_build_outputs.assert_not_called()
        restore.assert_called_once_with(metadata, set())
        final_metadata.assert_called_once_with(metadata, mode="separated")
        self.assertEqual(final_build.call_args.kwargs["mode"], "separated")
        self.assertIs(final_build.call_args.kwargs["build_outputs"], build_outputs)
        self.assertIs(final_build.call_args.kwargs["oci_outputs"], oci_outputs)
        self.assertEqual(
            final_build.call_args.kwargs["dependency_images"],
            dependency_images,
        )
        self.assertEqual(
            final_build.call_args.kwargs["build_cache"],
            "registry.example/cache",
        )
        upload.assert_not_called()

    def test_flatpak_build_marks_build_image_for_cleanup(self) -> None:
        metadata = SimpleNamespace(
            podman="podman",
            ci_registry="registry.example",
        )
        context = SimpleNamespace()
        plan = SimpleNamespace(build_image="builds:f44-flatpak-kate")
        result = SimpleNamespace(
            image="flatpaks:f44-kate-output",
            latest_image="flatpaks:kate",
        )
        cleanup_images = set()
        with (
            patch("ludos.ci._metadata_from_mapping", return_value=metadata),
            patch("ludos.ci._restore_ci_build_context"),
            patch("ludos.ci._prepared_flatpak_context", return_value=context),
            patch("ludos.ci._prepared_flatpak_plan", return_value=plan),
            patch(
                "ludos.ci._ensure_flatpak_rpm_builds",
                return_value=(plan,),
            ),
            patch(
                "ludos.ci._ensure_flatpak_images",
                return_value=(result,),
            ),
            patch("ludos.ci._upload_ci_output") as upload,
        ):
            _build_ci_flatpak(
                Path("cache/ci/build.yml"),
                "kate",
                {"build": {}},
                restored_contexts=set(),
                cleanup_images=cleanup_images,
                autoremove=False,
            )

        self.assertEqual(
            cleanup_images,
            {("podman", "builds:f44-flatpak-kate", "registry.example")},
        )
        upload.assert_not_called()

    def test_upload_ci_output_removes_aliases_only_after_upload(self) -> None:
        with (
            patch("ludos.ci._push_ci_image") as push,
            patch("ludos.ci._remove_image") as remove,
        ):
            _upload_ci_output(
                "podman",
                "images:exact",
                "registry.example",
                autoremove=True,
                aliases=("images:latest",),
            )

        push.assert_called_once_with("podman", "images:exact", "registry.example")
        self.assertEqual(
            remove.call_args_list,
            [call("podman", "images:latest"), call("podman", "images:exact")],
        )

    def test_remove_ci_dependency_images_removes_local_and_pulled_tags(self) -> None:
        with patch("ludos.ci._remove_image") as remove:
            _remove_ci_dependency_images(
                {
                    ("podman", "cards:f44-base", "ghcr.io/anatase-org"),
                    ("podman", "builds:f44-base", "ghcr.io/anatase-org"),
                }
            )

        self.assertEqual(
            remove.call_args_list,
            [
                call("podman", "builds:f44-base"),
                call("podman", "ghcr.io/anatase-org/builds:f44-base"),
                call("podman", "cards:f44-base"),
                call("podman", "ghcr.io/anatase-org/cards:f44-base"),
            ],
        )

    def test_flatpak_package_build_pulls_prepared_builder_directly(self) -> None:
        metadata = SimpleNamespace(
            podman="podman",
            ci_registry="registry.example",
        )
        context = SimpleNamespace(
            podman="podman",
            ci_registry="registry.example",
        )
        plan = SimpleNamespace(
            builder_image="builders:flatpak",
            build_image="builds:flatpak",
        )
        with (
            patch("ludos.ci._metadata_from_mapping", return_value=metadata),
            patch("ludos.ci._restore_ci_build_context") as restore_context,
            patch("ludos.ci._prepared_flatpak_context", return_value=context),
            patch("ludos.ci._prepared_flatpak_plan", return_value=plan),
            patch("ludos.ci._ensure_image", return_value=True) as ensure,
            patch("ludos.ci._ensure_flatpak_rpm_builds") as build,
            patch("ludos.ci._upload_ci_output") as upload,
        ):
            _build_ci_package(
                Path("cache/ci/build.yml"),
                "flatpak",
                {"metadata": {}, "flatpak": {}},
                upload=True,
                autoremove=False,
            )

        restore_context.assert_not_called()
        ensure.assert_called_once_with(
            "podman",
            "builders:flatpak",
            "registry.example",
        )
        build.assert_called_once_with(context, (plan,), cache_only=False)
        upload.assert_called_once_with(
            "podman",
            "builds:flatpak",
            "registry.example",
            autoremove=False,
        )

    def test_package_build_does_not_upload_by_default(self) -> None:
        metadata = SimpleNamespace(
            podman="podman",
            ci_registry="registry.example",
        )
        with (
            patch("ludos.ci._metadata_from_mapping", return_value=metadata),
            patch("ludos.ci.build_build_images") as build,
            patch("ludos.ci._upload_ci_output") as upload,
        ):
            _build_ci_package(
                Path("cache/ci/build.yml"),
                "package",
                {"metadata": {}, "image": "builds:package"},
                autoremove=True,
            )

        build.assert_called_once_with(
            (metadata,),
            targets=("builds:package",),
            cache_only=False,
        )
        upload.assert_not_called()


class PromoteCiTests(unittest.TestCase):
    def test_promote_ci_defaults_to_images_and_flatpaks(self) -> None:
        manifest = Path("anatase.yml").resolve()
        plan = SimpleNamespace(
            ref="flatpaks/kate",
            source_tag="rolling-f44-x86_64",
            target_tag="f44-x86_64",
        )
        promoted = (
            PromotedOciTag(
                "anatase",
                "rolling",
                "stable",
                "sha256:" + "a" * 64,
            ),
            PromotedOciTag(
                plan.ref,
                plan.source_tag,
                plan.target_tag,
                "sha256:" + "b" * 64,
            ),
        )
        with (
            patch("ludos.ci.Manifest.from_file"),
            patch(
                "ludos.ci.plan_flatpak_promotions",
                return_value=(plan,),
            ) as flatpak_plans,
            patch("ludos.ci.promote_oci_tags", return_value=promoted) as promote,
            patch(
                "ludos.ci.finish_flatpak_promotions",
                return_value=0,
            ) as finish,
        ):
            result = promote_ci(
                (Path("anatase.yml"),),
                prefix="rolling-",
                from_tag="rolling",
                to_tag="stable",
                refresh=True,
            )

        self.assertEqual(result, 0)
        flatpak_plans.assert_called_once_with(
            manifest,
            prefix="rolling-",
            arch=None,
        )
        promote.assert_called_once_with(
            (
                OciTagPromotion("anatase", "rolling", "stable"),
                OciTagPromotion(
                    "flatpaks/kate",
                    "rolling-f44-x86_64",
                    "f44-x86_64",
                ),
            )
        )
        finish.assert_called_once_with((plan,), promoted, refresh=True)

    def test_promote_ci_plans_flatpaks_for_each_requested_arch(self) -> None:
        manifest = Path("anatase.yml").resolve()
        x86 = SimpleNamespace(
            ref="flatpaks/kate",
            source_tag="rolling-f44-x86_64",
            target_tag="f44-x86_64",
        )
        arm = SimpleNamespace(
            ref="flatpaks/kate",
            source_tag="rolling-f44-aarch64",
            target_tag="f44-aarch64",
        )

        def plans(_manifest: Path, *, prefix: str, arch: str) -> tuple[object, ...]:
            self.assertEqual(prefix, "rolling-")
            return (x86,) if arch == "x86_64" else (arm,)

        promoted = (
            PromotedOciTag(x86.ref, x86.source_tag, x86.target_tag, "sha256:x86"),
            PromotedOciTag(arm.ref, arm.source_tag, arm.target_tag, "sha256:arm"),
        )
        with (
            patch("ludos.ci.Manifest.from_file"),
            patch("ludos.ci.plan_flatpak_promotions", side_effect=plans) as plan,
            patch("ludos.ci.promote_oci_tags", return_value=promoted) as promote,
            patch("ludos.ci.finish_flatpak_promotions", return_value=0) as finish,
        ):
            result = promote_ci(
                (Path("anatase.yml"),),
                prefix="rolling-",
                from_tag="rolling",
                to_tag="stable",
                arches=("x86_64", "aarch64"),
                flatpaks=True,
                refresh=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            plan.call_args_list,
            [
                call(manifest, prefix="rolling-", arch="x86_64"),
                call(manifest, prefix="rolling-", arch="aarch64"),
            ],
        )
        promote.assert_called_once_with(
            (
                OciTagPromotion(x86.ref, x86.source_tag, x86.target_tag),
                OciTagPromotion(arm.ref, arm.source_tag, arm.target_tag),
            )
        )
        finish.assert_called_once_with((x86, arm), promoted, refresh=True)

    def test_promote_ci_images_only_does_not_plan_flatpaks(self) -> None:
        with (
            patch("ludos.ci.Manifest.from_file"),
            patch("ludos.ci.plan_flatpak_promotions") as flatpak_plans,
            patch("ludos.ci.promote_oci_tags", return_value=tuple()) as promote,
        ):
            self.assertEqual(
                promote_ci(
                    (Path("anatase.yml"),),
                    prefix="rolling-",
                    from_tag="rolling",
                    to_tag="stable",
                    images=True,
                ),
                0,
            )

        flatpak_plans.assert_not_called()
        promote.assert_called_once_with(
            (OciTagPromotion("anatase", "rolling", "stable"),)
        )

    def test_promote_ci_validates_required_values(self) -> None:
        with self.assertRaisesRegex(ConfigError, "at least one manifest"):
            promote_ci(
                tuple(),
                prefix="rolling-",
                from_tag="rolling",
                to_tag="stable",
            )
        with self.assertRaisesRegex(ConfigError, "non-empty --prefix"):
            promote_ci(
                (Path("anatase.yml"),),
                prefix="",
                from_tag="rolling",
                to_tag="stable",
            )
        with self.assertRaisesRegex(ConfigError, "must differ"):
            promote_ci(
                (Path("anatase.yml"),),
                prefix="rolling-",
                from_tag="stable",
                to_tag="stable",
            )


class UploadCiTests(unittest.TestCase):
    def test_upload_ci_exports_local_image_with_explicit_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self._write_manifest(root)
            build_manifest = self._write_build_manifest(root, manifest)

            with (
                patch("ludos.ci.Path.cwd", return_value=root.resolve()),
                patch("ludos.ci._ensure_image", return_value=True) as ensure,
                patch("ludos.ci._export_bootc_images") as export,
                patch("ludos.ci.upload_oci", return_value=0) as upload,
            ):
                result = upload_ci(
                    ("f44-anatase",),
                    build_manifest=build_manifest,
                    tags=("candidate", "latest"),
                    previous_manifest="registry.example.test/anatase:stable",
                )

        self.assertEqual(result, 0)
        ensure.assert_called_once_with(
            "podman",
            "images:f44-anatase-output",
            "ghcr.io/example",
        )
        self.assertEqual(export.call_args.args[0], (manifest.resolve(),))
        self.assertEqual(
            export.call_args.kwargs["cache_dir"],
            (root / "cache").resolve(),
        )
        self.assertEqual(
            export.call_args.kwargs["previous_manifest"],
            "registry.example.test/anatase:stable",
        )
        upload.assert_called_once_with(
            (root / "cache" / "oci" / "anatase-f44-x86_64").resolve(),
            "anatase",
            ("candidate", "latest"),
            project_root=root.resolve(),
        )

    def test_upload_ci_uses_prepared_flatpak_image_and_refreshes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self._write_manifest(root)
            build_manifest = self._write_build_manifest(root, manifest)

            with (
                patch("ludos.ci.Path.cwd", return_value=root.resolve()),
                patch("ludos.ci._ensure_image", return_value=True) as ensure,
                patch("ludos.ci.upload_flatpaks", return_value=0) as upload,
                patch("ludos.ci.update_flatpak_index", return_value=0) as refresh,
            ):
                result = upload_ci(
                    ("f44-kate",),
                    build_manifest=build_manifest,
                    refresh=True,
                    prefix="rolling-",
                )

        self.assertEqual(result, 0)
        ensure.assert_called_once_with(
            "podman",
            "flatpaks:f44-kate-output",
            "ghcr.io/example",
        )
        upload.assert_called_once_with(
            manifest.resolve(),
            (Path("flatpaks/kate"),),
            build=False,
            cache_dir=(root / "cache").resolve(),
            cache_only=True,
            image_overrides={
                "flatpaks/kate": "flatpaks:f44-kate-output",
            },
            prefix="rolling-",
        )
        refresh.assert_called_once_with(manifest.resolve(), prefix="rolling-")

    def test_upload_ci_requires_an_output_selector(self) -> None:
        with self.assertRaisesRegex(ConfigError, "at least one CI upload ID"):
            upload_ci(tuple())

    def test_remove_ci_removes_selected_registry_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self._write_manifest(root)
            build_manifest = self._write_build_manifest(root, manifest)

            with (
                patch("ludos.ci.Path.cwd", return_value=root.resolve()),
                patch("ludos.ci._remove_ci_remote_image") as remove,
            ):
                result = remove_ci(
                    ("f44-anatase",),
                    build_manifest=build_manifest,
                    flatpaks=True,
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            remove.call_args_list,
            [
                call("images:f44-anatase-output", "ghcr.io/example"),
                call("flatpaks:f44-kate-output", "ghcr.io/example"),
            ],
        )

    def test_remove_ci_attempts_every_output_before_reporting_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self._write_manifest(root)
            build_manifest = self._write_build_manifest(root, manifest)

            with (
                patch("ludos.ci.Path.cwd", return_value=root.resolve()),
                patch(
                    "ludos.ci._remove_ci_remote_image",
                    side_effect=(ConfigError("first failed"), None),
                ) as remove,
                self.assertRaisesRegex(ConfigError, "first failed"),
            ):
                remove_ci(
                    tuple(),
                    build_manifest=build_manifest,
                    images=True,
                    flatpaks=True,
                )

        self.assertEqual(remove.call_count, 2)

    def test_remove_ci_requires_an_output_selector(self) -> None:
        with self.assertRaisesRegex(ConfigError, "at least one CI remove ID"):
            remove_ci(tuple())

    def test_remove_ci_remote_image_ignores_missing_manifest(self) -> None:
        with (
            patch("ludos.ci.shutil.which", return_value="/usr/bin/skopeo"),
            patch(
                "ludos.ci._run_streamed_command",
                return_value=(1, "manifest unknown"),
            ) as run,
        ):
            _remove_ci_remote_image("flatpaks:output", "ghcr.io/example")

        run.assert_called_once_with(
            [
                "/usr/bin/skopeo",
                "delete",
                "docker://ghcr.io/example/flatpaks:output",
            ]
        )

    def _write_manifest(self, root: Path) -> Path:
        manifest = root / "anatase.yml"
        manifest.write_text(
            "\n".join(
                [
                    "version: 1",
                    "env:",
                    "  arch: x86_64",
                    "  tag: ''",
                    "releasever: '44'",
                    "distro: f44-x86_64",
                    "tag: $tag",
                    "orchestrator: example/orchestrator:44",
                    "bootstrap: cards/bootstrap.yml",
                    "repos: []",
                    "cards:",
                    "  - cards/base.yml",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return manifest

    def _write_build_manifest(self, root: Path, manifest: Path) -> Path:
        metadata = {
            "image": "anatase",
            "distro": "f44-x86_64",
            "root_dir": str(root),
            "output_image": "images:f44-anatase-output",
            "latest_image": "images:anatase",
            "manifest_env": [["arch", "x86_64"], ["tag", "20260716"]],
            "manifest_labels": [
                ["org.opencontainers.image.version", "20260716"]
            ],
            "podman": "podman",
            "cache_version": "20260713",
            "ci_registry": "ghcr.io/example",
        }
        build_manifest = root / "cache" / "ci" / "build.yml"
        build_manifest.parent.mkdir(parents=True)
        build_manifest.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "builds": {},
                    "images": {
                        "f44-anatase": {
                            "path": str(manifest),
                            "build": metadata,
                        }
                    },
                    "flatpaks": {
                        "f44-kate": {
                            "manifest": str(manifest),
                            "source": "flatpaks/kate",
                            "images": {"output": "flatpaks:f44-kate-output"},
                            "build": metadata,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return build_manifest


class SeedCiTests(unittest.TestCase):
    def test_create_seed_builder_image_suppresses_builder_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            entries = _read_seed_entries(
                self._write_seed_manifest(Path(temp))
            )
            manifest = entries[1][1]

            with patch("ludos.ci._create_builder_image") as create:
                _create_seed_builder_image(
                    manifest,
                    "builders:f44-base",
                    ("rpm-build-1-1.fc44.x86_64.rpm",),
                )

            self.assertTrue(create.call_args.kwargs["quiet"])

    def test_prepare_seed_rpms_downloads_only_uncached_rpms_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entries = _read_seed_entries(self._write_seed_manifest(root))
            package_dir = root / "packages"
            package_dir.mkdir()
            (package_dir / "bash-1-1.fc44.x86_64.rpm").touch()
            sizes = {
                "rpm-build-1-1.fc44.x86_64.rpm": 1024,
                "flatpak-rpm-macros-1-1.fc44.x86_64.rpm": 2048,
            }

            with (
                patch("ludos.ci._seed_rpm_download_sizes", return_value=sizes) as query,
                patch(
                    "ludos.ci.shutil.disk_usage",
                    return_value=SimpleNamespace(free=10_000),
                ) as disk_usage,
                patch("ludos.ci._download_exact_packages") as download,
            ):
                rpm_files = _prepare_seed_rpms(entries, buffer_ratio=1.5)

            query.assert_called_once_with(
                ["podman", "run"],
                (
                    "rpm-build-0:1-1.fc44.x86_64",
                    "flatpak-rpm-macros-0:1-1.fc44.x86_64",
                ),
            )
            disk_usage.assert_called_once_with(package_dir.resolve())
            download.assert_called_once_with(
                ["podman", "run"],
                (
                    "rpm-build-0:1-1.fc44.x86_64",
                    "flatpak-rpm-macros-0:1-1.fc44.x86_64",
                ),
                "/ludos/packages",
            )
            self.assertEqual(rpm_files, self._seed_rpm_files())

    def test_prepare_seed_rpms_rejects_insufficient_disk_space(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entries = _read_seed_entries(self._write_seed_manifest(root))
            sizes = {
                "bash-1-1.fc44.x86_64.rpm": 100,
                "rpm-build-1-1.fc44.x86_64.rpm": 100,
                "flatpak-rpm-macros-1-1.fc44.x86_64.rpm": 100,
            }

            with (
                patch("ludos.ci._seed_rpm_download_sizes", return_value=sizes),
                patch(
                    "ludos.ci.shutil.disk_usage",
                    return_value=SimpleNamespace(free=449),
                ),
                patch("ludos.ci._download_exact_packages") as download,
            ):
                with self.assertRaisesRegex(
                    SeedDiskSpaceError,
                    r"449.0 B available, 450.0 B required \(1.5x buffer\)",
                ):
                    _prepare_seed_rpms(entries, buffer_ratio=1.5)

            download.assert_not_called()

    def test_seed_rpm_download_sizes_uses_repoquery_downloadsize(self) -> None:
        result = SimpleNamespace(
            stdout=(
                "Packages/b/bash-1-1.fc44.x86_64.rpm\t1234\n"
                "Packages/r/rpm-build-1-1.fc44.x86_64.rpm\t5678\n"
            )
        )

        with patch("ludos.ci.subprocess.run", return_value=result) as run:
            sizes = _seed_rpm_download_sizes(
                ["podman", "run"],
                (
                    "bash-0:1-1.fc44.x86_64",
                    "rpm-build-0:1-1.fc44.x86_64",
                ),
            )

        self.assertEqual(
            sizes,
            {
                "bash-1-1.fc44.x86_64.rpm": 1234,
                "rpm-build-1-1.fc44.x86_64.rpm": 5678,
            },
        )
        command = run.call_args.args[0]
        self.assertIn("repoquery", command)
        self.assertIn("%{location}\t%{downloadsize}\n", command)

    def test_seed_ci_rejects_invalid_workers_and_buffer_ratio(self) -> None:
        with self.assertRaisesRegex(ConfigError, "workers must be"):
            seed_ci(Path("build.yml"), workers=0)
        with self.assertRaisesRegex(ConfigError, "buffer ratio must be"):
            seed_ci(Path("build.yml"), buffer_ratio=0)

    def test_seed_ci_defaults_buffer_ratio_from_workers(self) -> None:
        with (
            patch("ludos.ci._read_seed_entries", return_value=tuple()),
            patch("ludos.ci._prepare_seed_rpms", return_value={}) as prepare,
        ):
            seed_ci(Path("build.yml"), workers=2)

        prepare.assert_called_once_with(
            tuple(),
            buffer_ratio=2 * DEFAULT_SEED_BUFFER_RATIO,
        )

    def test_seed_ci_uses_prefiltered_manifest_without_remote_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = self._write_seed_manifest(Path(temp))

            with (
                patch(
                    "ludos.ci._prepare_seed_rpms",
                    return_value=self._seed_rpm_files(),
                ),
                patch("ludos.ci._ci_remote_image_exists", return_value=True) as remote,
                patch("ludos.ci._local_image_exists", return_value=True),
                patch("ludos.ci._push_ci_image") as push,
                patch("ludos.ci._create_seed_package_image") as create_package,
                patch("ludos.ci._create_seed_builder_image") as create_builder,
                patch("ludos.ci.log") as log,
            ):
                seed_ci(build_manifest)

            remote.assert_not_called()
            self.assertCountEqual(
                [call.args[1] for call in push.call_args_list],
                [
                    "cards:f44-common",
                    "builders:f44-base",
                    "builders:f44-flatpak-kate",
                ],
            )
            create_package.assert_not_called()
            create_builder.assert_not_called()
            creating = [
                call.args[0]
                for call in log.call_args_list
                if "Creating" in call.args[0]
            ]
            self.assertEqual(
                [line.split(" ", 1)[0] for line in creating],
                ["(01/03)", "(02/03)", "(03/03)"],
            )
            self.assertCountEqual(
                [line.split(" ", 1)[1] for line in creating],
                [
                    "Creating cards:f44-common Image",
                    "Creating builders:f44-base Image",
                    "Creating builders:f44-flatpak-kate Image",
                ],
            )

    def test_seed_ci_creates_listed_images_missing_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = self._write_seed_manifest(Path(temp))

            with (
                patch(
                    "ludos.ci._prepare_seed_rpms",
                    return_value=self._seed_rpm_files(),
                ),
                patch("ludos.ci._local_image_exists", return_value=False),
                patch("ludos.ci._push_ci_image") as push,
                patch("ludos.ci._create_seed_package_image") as create_package,
                patch("ludos.ci._create_seed_builder_image") as create_builder,
                patch("ludos.ci.log") as log,
            ):
                seed_ci(build_manifest)

            self.assertEqual(create_package.call_count, 1)
            self.assertEqual(
                create_package.call_args.args[1:],
                ("cards:f44-common", ("bash-1-1.fc44.x86_64.rpm",)),
            )
            self.assertEqual(create_builder.call_count, 2)
            self.assertCountEqual(
                [item.args[1:] for item in create_builder.call_args_list],
                [
                    (
                        "builders:f44-base",
                        ("rpm-build-1-1.fc44.x86_64.rpm",),
                    ),
                    (
                        "builders:f44-flatpak-kate",
                        ("flatpak-rpm-macros-1-1.fc44.x86_64.rpm",),
                    ),
                ],
            )
            self.assertCountEqual(
                [call.args[1] for call in push.call_args_list],
                [
                    "cards:f44-common",
                    "builders:f44-base",
                    "builders:f44-flatpak-kate",
                ],
            )
            creating = [
                call.args[0]
                for call in log.call_args_list
                if "Creating" in call.args[0]
            ]
            self.assertEqual(
                [line.split(" ", 1)[0] for line in creating],
                ["(01/03)", "(02/03)", "(03/03)"],
            )
            self.assertCountEqual(
                [line.split(" ", 1)[1] for line in creating],
                [
                    "Creating cards:f44-common Image",
                    "Creating builders:f44-base Image",
                    "Creating builders:f44-flatpak-kate Image",
                ],
            )

    def test_seed_ci_autoremoves_uploaded_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = self._write_seed_manifest(Path(temp))

            with (
                patch(
                    "ludos.ci._prepare_seed_rpms",
                    return_value=self._seed_rpm_files(),
                ),
                patch("ludos.ci._local_image_exists", return_value=True),
                patch("ludos.ci._push_ci_image") as push,
                patch("ludos.ci._remove_image") as remove,
                patch("ludos.ci._create_seed_package_image") as create_package,
                patch("ludos.ci._create_seed_builder_image") as create_builder,
            ):
                seed_ci(build_manifest, autoremove=True)

            self.assertCountEqual(
                [call.args[1] for call in push.call_args_list],
                [
                    "cards:f44-common",
                    "builders:f44-base",
                    "builders:f44-flatpak-kate",
                ],
            )
            self.assertCountEqual(
                [call.args[1] for call in remove.call_args_list],
                [
                    "cards:f44-common",
                    "builders:f44-base",
                    "builders:f44-flatpak-kate",
                ],
            )
            create_package.assert_not_called()
            create_builder.assert_not_called()

    def test_seed_ci_ignores_unlisted_nested_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_manifest = self._write_seed_manifest(root)
            data = yaml.safe_load(build_manifest.read_text(encoding="utf-8"))
            data["cards"] = {}
            data["builders"] = {}
            build_manifest.write_text(
                yaml.safe_dump(data, sort_keys=False),
                encoding="utf-8",
            )

            with (
                patch("ludos.ci._prepare_seed_rpms", return_value={}),
                patch("ludos.ci._ci_remote_image_exists") as remote,
                patch("ludos.ci._local_image_exists") as local,
                patch("ludos.ci._push_ci_image") as push,
            ):
                seed_ci(build_manifest)

            remote.assert_not_called()
            local.assert_not_called()
            push.assert_not_called()

    @staticmethod
    def _seed_rpm_files() -> dict[str, tuple[str, ...]]:
        return {
            "cards:f44-common": ("bash-1-1.fc44.x86_64.rpm",),
            "builders:f44-base": ("rpm-build-1-1.fc44.x86_64.rpm",),
            "builders:f44-flatpak-kate": (
                "flatpak-rpm-macros-1-1.fc44.x86_64.rpm",
            ),
        }

    def _write_seed_manifest(self, root: Path) -> Path:
        build_manifest = root / "build.yml"
        metadata = {
            "image": "anatase",
            "distro": "f44",
            "releasever": "44",
            "arch": "x86_64",
            "root_dir": str(root),
            "orchestrator": "orchestrator:f44",
            "output_image": "images:f44-anatase",
            "package_dir": str(root / "packages"),
            "repo_dir": str(root / "repos"),
            "dnf_cache_dir": str(root / "dnf/cache"),
            "dnf_persist_dir": str(root / "dnf/persist"),
            "dnf_log_dir": str(root / "dnf/log"),
            "podman": "podman",
            "buildah": "buildah",
            "orchestrator_dnf_base": ["podman", "run"],
            "ci_registry": "ghcr.io/anatase-org",
        }
        build_manifest.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "cards": {
                        "f44-common": {
                            "image": "cards:f44-common",
                            "packages": ["bash-0:1-1.fc44.x86_64"],
                            "metadata": metadata,
                        }
                    },
                    "builders": {
                        "f44-base": {
                            "image": "builders:f44-base",
                            "packages": ["rpm-build-0:1-1.fc44.x86_64"],
                            "metadata": metadata,
                        },
                        "f44-flatpak-kate": {
                            "image": "builders:f44-flatpak-kate",
                            "packages": [
                                "flatpak-rpm-macros-0:1-1.fc44.x86_64"
                            ],
                            "metadata": metadata,
                        },
                    },
                    "images": {
                        "f44-anatase": {
                            "path": "anatase.yml",
                            "build": {
                                "image": "anatase",
                                "distro": "f44",
                                "releasever": "44",
                                "arch": "x86_64",
                                "root_dir": str(root),
                                "local_prefix": "",
                                "orchestrator": "orchestrator:f44",
                                "output_image": "images:f44-anatase",
                                "manifest_labels": [],
                                "manifest_env": [],
                                "common_packages": [],
                                "bootstrap_packages": [],
                                "card_order": [],
                                "card_packages": [],
                                "card_resolutions": [],
                                "package_ids": [],
                                "package_images": {
                                    "f44-common": {
                                        "block": "common",
                                        "packages": ["bash-0:1-1.fc44.x86_64"],
                                        "image": "cards:f44-common",
                                    }
                                },
                                "build_images": {
                                    "f44-base": {
                                        "block": "base",
                                        "image": "builds:f44-base",
                                        "builder_image": "builders:f44-base",
                                        "builder_packages": [
                                            "rpm-build-0:1-1.fc44.x86_64"
                                        ],
                                    }
                                },
                                "oci_images": {},
                                "package_dir": str(root / "packages"),
                                "repo_dir": str(root / "repos"),
                                "cache_dir": str(root),
                                "build_dir": str(root / "build"),
                                "card_build_dir": str(root / "cards"),
                                "spec_source_cache_dir": str(root / "spec-sources"),
                                "build_artifact_cache_dir": str(root / "artifacts"),
                                "dnf_workspace_dir": str(root / "dnf"),
                                "dnf_cache_dir": str(root / "dnf/cache"),
                                "dnf_persist_dir": str(root / "dnf/persist"),
                                "dnf_log_dir": str(root / "dnf/log"),
                                "dnf_resolve_dir": str(root / "dnf/resolves"),
                                "podman": "podman",
                                "buildah": "buildah",
                                "cache_version": "20260629",
                                "repo_images": ["repos:f44-fedora"],
                                "orchestrator_dnf_base": ["podman", "run"],
                                "package_blocks": [],
                                "card_file_sets": [],
                                "postprocess_blocks": [],
                                "card_envs": [],
                                "card_sources": [],
                                "card_prepare_scripts": [],
                                "card_builds": [],
                                "card_specs": [],
                                "spec_source_revisions": [],
                                "latest_image": "images:anatase",
                                "ci_registry": "ghcr.io/anatase-org",
                            },
                        }
                    },
                    "flatpaks": {
                        "kate": {
                            "images": {
                                "builder": "builders:f44-flatpak-kate",
                                "build": "builds:f44-flatpak-kate",
                            }
                        }
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return build_manifest

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

from ludos.__main__ import build_parser, ci_command
from ludos.build import (
    BuildImagePlan,
    OciImagePlan,
    PackageImagePlan,
    ResolvedBuildMetadata,
)
from ludos.ci import (
    DEFAULT_PREPARE_WORKERS,
    _ci_remote_image_exists,
    _inspect_remote_labels,
    _manifest_tag,
    init_ci,
    prepare_ci,
    seed_ci,
    write_ci_env,
)
from ludos.model import ConfigError, SpecBuild


class CiParserTests(unittest.TestCase):
    def test_parser_accepts_env_ci_options(self) -> None:
        args = build_parser().parse_args(
            ["ci", "env", "anatase.yml", "ghcr.io/test/anatase:latest"]
        )

        self.assertEqual(args.ci_action, "env")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertEqual(args.ref, "ghcr.io/test/anatase:latest")
        self.assertEqual(args.label, "org.opencontainers.image.version")

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
                "--no-ccache",
                "--recreate",
                "anatase.yml",
            ]
        )

        self.assertEqual(args.ci_action, "init")
        self.assertEqual(args.manifests, [Path("anatase.yml")])
        self.assertEqual(args.cache_dir, Path("cache"))
        self.assertEqual(args.version, "20260629")
        self.assertTrue(args.no_ccache)
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
                "--no-ccache",
                "--full",
                "--workers",
                "8",
                "anatase.yml",
            ]
        )

        self.assertEqual(args.ci_action, "prepare")
        self.assertEqual(args.manifests, [Path("anatase.yml")])
        self.assertEqual(args.cache_dir, Path("cache"))
        self.assertEqual(args.version, "20260629")
        self.assertTrue(args.no_ccache)
        self.assertTrue(args.full)
        self.assertEqual(args.workers, 8)

    def test_parser_defaults_prepare_workers(self) -> None:
        args = build_parser().parse_args(["ci", "prepare", "anatase.yml"])

        self.assertEqual(args.workers, DEFAULT_PREPARE_WORKERS)

    def test_parser_accepts_seed_ci_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["ci", "seed", "cache/ci/build.yml"])

        self.assertEqual(args.ci_action, "seed")
        self.assertEqual(args.build_manifest, Path("cache/ci/build.yml"))

    def test_parser_defaults_seed_ci_build_manifest(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["ci", "seed"])

        self.assertEqual(args.ci_action, "seed")
        self.assertIsNone(args.build_manifest)
        self.assertIsNone(args.cache_dir)

    def test_parser_accepts_seed_ci_cache_dir(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["ci", "seed", "--cache-dir", "out-cache", "--autoremove"]
        )

        self.assertEqual(args.ci_action, "seed")
        self.assertIsNone(args.build_manifest)
        self.assertEqual(args.cache_dir, Path("out-cache"))
        self.assertTrue(args.autoremove)

    def test_prepare_ci_rejects_cache_and_cards_dir(self) -> None:
        parser = build_parser()

        for option in ("--cache", "--cards-dir"):
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
                "--no-ccache",
                "--workers",
                "8",
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
            ccache=False,
            full=False,
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
                "--no-ccache",
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
            ccache=False,
            recreate=False,
        )

    def test_ci_command_calls_seed_ci(self) -> None:
        args = build_parser().parse_args(["ci", "seed", "cache/ci/build.yml"])

        with patch("ludos.__main__.seed_ci") as seed:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        seed.assert_called_once_with(
            Path("cache/ci/build.yml"),
            cache_dir=None,
            autoremove=False,
        )

    def test_ci_command_calls_seed_ci_with_default_build_manifest(self) -> None:
        args = build_parser().parse_args(["ci", "seed"])

        with patch("ludos.__main__.seed_ci") as seed:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        seed.assert_called_once_with(None, cache_dir=None, autoremove=False)

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
        )


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


class PrepareCiTests(unittest.TestCase):
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
            metadata = self._metadata(root, context)
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
                    ccache=False,
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
                    call(metadata.podman, metadata.output_image, "ghcr.io/anatase-org"),
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
                    "Creating images:f44-anatase Image",
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
                data["builders"]["f44-flatpak-builder"]["metadata"]["app"],
                "kate",
            )
            self.assertEqual(
                data["builds"]["f44-flatpak-kate-build"]["metadata"]["specs"][0][
                    "spec"
                ],
                "kate.spec",
            )
            self.assertEqual(
                data["builds"]["f44-base"]["metadata"]["output_image"],
                "images:f44-anatase",
            )
            self.assertEqual(data["images"]["f44-anatase"]["path"], str(manifest))
            self.assertEqual(
                data["images"]["f44-anatase"]["build"]["package_images"][
                    "f44-common"
                ]["block"],
                "common",
            )
            self.assertEqual(
                data["images"]["f44-anatase"]["build"]["build_images"][
                    "f44-base"
                ]["block"],
                "base",
            )
            self.assertEqual(
                data["images"]["f44-anatase"]["build"]["oci_images"]["kernel-f44"][
                    "image"
                ],
                "kernel:f44@sha256:kernel111",
            )
            self.assertEqual(
                data["images"]["f44-anatase"]["build"]["oci_images"]["kernel-f44"][
                    "tagged_image"
                ],
                "kernel:f44",
            )
            self.assertNotIn(
                "requested_packages",
                data["images"]["f44-anatase"]["build"],
            )
            self.assertNotIn(
                "resolved_packages",
                data["images"]["f44-anatase"]["build"],
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

    def test_prepare_ci_lists_missing_dependencies_for_existing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "anatase.yml"
            manifest.write_text("version: 1\n", encoding="utf-8")
            cache = root / "cache"
            context = self._context(root)
            metadata = self._metadata(root, context)
            plan = self._flatpak_plan(root, cache)

            def remote_exists(_podman: str, image: str, _registry: str) -> bool:
                return image in {metadata.output_image, plan.output_image}

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
                data["builds"]["f44-flatpak-kate-build"]["metadata"]["app"],
                "kate",
            )
            self.assertEqual(
                data["builds"]["f44-base"]["metadata"]["output_image"],
                metadata.output_image,
            )

    def test_prepare_ci_full_keeps_already_built_outputs(self) -> None:
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
                patch("ludos.ci._ci_remote_image_exists", return_value=True) as remote_exists,
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                output = prepare_ci((manifest,), cache_dir=cache, full=True)

            data = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertIn("f44-anatase", data["images"])
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
                call(context.podman, metadata.output_image, "ghcr.io/anatase-org"),
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


class SeedCiTests(unittest.TestCase):
    def test_seed_ci_skips_remote_images_without_pulling_or_creating(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = self._write_seed_manifest(Path(temp))

            with (
                patch("ludos.ci._ci_remote_image_exists", return_value=True) as remote,
                patch("ludos.ci._local_image_exists") as local,
                patch("ludos.ci._push_ci_image") as push,
                patch("ludos.ci._create_seed_package_image") as create_package,
                patch("ludos.ci._create_seed_builder_image") as create_builder,
                patch("ludos.ci.log"),
            ):
                seed_ci(build_manifest)

            self.assertEqual(
                [call.args[1] for call in remote.call_args_list],
                ["cards:f44-common", "builders:f44-base"],
            )
            local.assert_not_called()
            push.assert_not_called()
            create_package.assert_not_called()
            create_builder.assert_not_called()

    def test_seed_ci_pushes_local_images_missing_remotely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = self._write_seed_manifest(Path(temp))

            with (
                patch("ludos.ci._ci_remote_image_exists", return_value=False),
                patch("ludos.ci._local_image_exists", return_value=True),
                patch("ludos.ci._push_ci_image") as push,
                patch("ludos.ci._create_seed_package_image") as create_package,
                patch("ludos.ci._create_seed_builder_image") as create_builder,
            ):
                seed_ci(build_manifest)

            self.assertEqual(
                [call.args[1] for call in push.call_args_list],
                ["cards:f44-common", "builders:f44-base"],
            )
            create_package.assert_not_called()
            create_builder.assert_not_called()

    def test_seed_ci_creates_local_images_missing_remotely_and_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = self._write_seed_manifest(Path(temp))

            with (
                patch("ludos.ci._ci_remote_image_exists", return_value=False),
                patch("ludos.ci._local_image_exists", return_value=False),
                patch("ludos.ci._push_ci_image") as push,
                patch("ludos.ci._create_seed_package_image") as create_package,
                patch("ludos.ci._create_seed_builder_image") as create_builder,
            ):
                seed_ci(build_manifest)

            self.assertEqual(create_package.call_count, 1)
            self.assertEqual(create_builder.call_count, 1)
            self.assertEqual(
                [call.args[1] for call in push.call_args_list],
                ["cards:f44-common", "builders:f44-base"],
            )

    def test_seed_ci_autoremoves_uploaded_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = self._write_seed_manifest(Path(temp))

            with (
                patch("ludos.ci._ci_remote_image_exists", return_value=False),
                patch("ludos.ci._local_image_exists", return_value=True),
                patch("ludos.ci._push_ci_image") as push,
                patch("ludos.ci._remove_image") as remove,
                patch("ludos.ci._create_seed_package_image") as create_package,
                patch("ludos.ci._create_seed_builder_image") as create_builder,
            ):
                seed_ci(build_manifest, autoremove=True)

            self.assertEqual(
                [call.args[1] for call in push.call_args_list],
                ["cards:f44-common", "builders:f44-base"],
            )
            self.assertEqual(
                [call.args[1] for call in remove.call_args_list],
                ["cards:f44-common", "builders:f44-base"],
            )
            create_package.assert_not_called()
            create_builder.assert_not_called()

    def test_seed_ci_autoremove_skips_remote_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_manifest = self._write_seed_manifest(Path(temp))

            with (
                patch("ludos.ci._ci_remote_image_exists", return_value=True),
                patch("ludos.ci._local_image_exists"),
                patch("ludos.ci._push_ci_image"),
                patch("ludos.ci._remove_image") as remove,
                patch("ludos.ci.log"),
            ):
                seed_ci(build_manifest, autoremove=True)

            remove.assert_not_called()

    def _write_seed_manifest(self, root: Path) -> Path:
        build_manifest = root / "build.yml"
        build_manifest.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
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
                                "ccache_dir": None,
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

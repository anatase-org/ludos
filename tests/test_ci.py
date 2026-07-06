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
from ludos.ci import _ci_remote_image_exists, init_ci, prepare_ci, seed_ci
from ludos.model import ConfigError, SpecBuild


class CiParserTests(unittest.TestCase):
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
                "anatase.yml",
            ]
        )

        self.assertEqual(args.ci_action, "prepare")
        self.assertEqual(args.manifests, [Path("anatase.yml")])
        self.assertEqual(args.cache_dir, Path("cache"))
        self.assertEqual(args.version, "20260629")
        self.assertTrue(args.no_ccache)
        self.assertTrue(args.full)

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

        args = parser.parse_args(["ci", "seed", "--cache-dir", "out-cache"])

        self.assertEqual(args.ci_action, "seed")
        self.assertIsNone(args.build_manifest)
        self.assertEqual(args.cache_dir, Path("out-cache"))

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
        seed.assert_called_once_with(Path("cache/ci/build.yml"), cache_dir=None)

    def test_ci_command_calls_seed_ci_with_default_build_manifest(self) -> None:
        args = build_parser().parse_args(["ci", "seed"])

        with patch("ludos.__main__.seed_ci") as seed:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        seed.assert_called_once_with(None, cache_dir=None)

    def test_ci_command_calls_seed_ci_with_cache_dir(self) -> None:
        args = build_parser().parse_args(["ci", "seed", "--cache-dir", "out-cache"])

        with patch("ludos.__main__.seed_ci") as seed:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        seed.assert_called_once_with(None, cache_dir=Path("out-cache"))


class PrepareCiTests(unittest.TestCase):
    def test_remote_image_exists_checks_manifest_head(self) -> None:
        with (
            patch("ludos.ci._registry_basic_auth", return_value="basic-token"),
            patch("ludos.ci._registry_head", return_value=(200, {})) as head,
        ):
            exists = _ci_remote_image_exists(
                "podman",
                "orchestrator:f44",
                "ghcr.io/anatase-org",
            )

        self.assertTrue(exists)
        url, headers = head.call_args.args
        self.assertEqual(
            url,
            "https://ghcr.io/v2/anatase-org/orchestrator/manifests/f44",
        )
        self.assertEqual(headers["Authorization"], "Basic basic-token")
        self.assertIn(
            "application/vnd.oci.image.manifest.v1+json",
            headers["Accept"],
        )

    def test_remote_image_exists_uses_bearer_challenge(self) -> None:
        challenge = (
            'Bearer realm="https://ghcr.io/token",'
            'service="ghcr.io",'
            'scope="repository:anatase-org/orchestrator:pull"'
        )
        with (
            patch("ludos.ci._registry_basic_auth", return_value="basic-token"),
            patch(
                "ludos.ci._registry_bearer_token",
                return_value="bearer-token",
            ) as token,
            patch(
                "ludos.ci._registry_head",
                side_effect=[(401, {"www-authenticate": challenge}), (200, {})],
            ) as head,
        ):
            exists = _ci_remote_image_exists(
                "podman",
                "orchestrator:f44",
                "ghcr.io/anatase-org",
            )

        self.assertTrue(exists)
        token.assert_called_once_with(challenge, "basic-token")
        self.assertEqual(
            head.call_args_list[1].args[1]["Authorization"],
            "Bearer bearer-token",
        )

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
            )
            plan_flatpaks.assert_called_once_with(
                context,
                manifest_path=manifest,
                cache_only=False,
            )
            remote_exists.assert_has_calls(
                [
                    call(context.podman, plan.output_image, "ghcr.io/anatase-org"),
                    call(metadata.podman, metadata.output_image, "ghcr.io/anatase-org"),
                ]
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
            log.assert_any_call(
                "Wrote encoded CI build manifest: "
                f"{output.with_suffix('.yml.encoded')} "
                f"({(len(encoded) + 1023) // 1024} KiB)"
            )
            self.assertEqual(data["version"], 1)
            self.assertNotIn("manifests", data)
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
                "builders:f44-flatpak-kate-builder",
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
            self.assertEqual(data["images"], {})
            self.assertEqual(data["flatpaks"], {})

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
            remote_exists.assert_not_called()

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

        remote_exists.assert_has_calls(
            [
                call(context.podman, plan.output_image, "ghcr.io/anatase-org"),
                call(context.podman, metadata.output_image, "ghcr.io/anatase-org"),
            ]
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
            builder_image="builders:f44-flatpak-kate-builder",
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

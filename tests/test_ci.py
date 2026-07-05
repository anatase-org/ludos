from __future__ import annotations

import base64
import lzma
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from ludos.__main__ import build_parser, ci_command
from ludos.build import (
    BuildImagePlan,
    OciImagePlan,
    PackageImagePlan,
    ResolvedBuildMetadata,
)
from ludos.ci import prepare_ci
from ludos.model import ConfigError, SpecBuild


class CiParserTests(unittest.TestCase):
    def test_parser_accepts_prepare_ci_options(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "ci",
                "prepare",
                "--cache",
                "--cache-dir",
                "cache",
                "--version",
                "20260629",
                "--no-ccache",
                "anatase.yml",
            ]
        )

        self.assertEqual(args.ci_action, "prepare")
        self.assertEqual(args.manifests, [Path("anatase.yml")])
        self.assertEqual(args.cache_dir, Path("cache"))
        self.assertEqual(args.version, "20260629")
        self.assertTrue(args.cache)
        self.assertTrue(args.no_ccache)

    def test_ci_command_calls_prepare_ci(self) -> None:
        args = build_parser().parse_args(
            [
                "ci",
                "prepare",
                "--cache-dir",
                "cache",
                "--version",
                "20260629",
                "--cache",
                "--no-ccache",
                "anatase.yml",
            ]
        )

        with patch("ludos.__main__.prepare_ci") as create:
            exit_code = ci_command(args)

        self.assertEqual(exit_code, 0)
        create.assert_called_once_with(
            (Path("anatase.yml"),),
            cards_dir=None,
            cache_dir=Path("cache"),
            cache_version="20260629",
            cache_only=True,
            ccache=False,
        )


class PrepareCiTests(unittest.TestCase):
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
                patch("ludos.ci._image_exists", return_value=False) as image_exists,
                patch("ludos.ci.build_package_card_images") as build_cards,
                patch("ludos.ci._remove_tree") as remove_tree,
                patch("ludos.ci.log") as log,
            ):
                output = prepare_ci(
                    (manifest,),
                    cache_dir=cache,
                    cache_version="20260629",
                    cache_only=True,
                    ccache=False,
                )

            self.assertEqual(output, cache.resolve() / "ci" / "build.yml")
            resolve_context.assert_called_once_with(
                manifest,
                cards_dir=None,
                cache_dir=cache.resolve(),
                cache_version="20260629",
                cache_only=True,
                ccache=False,
                dnf_workspace_dirs=[],
            )
            resolve_build.assert_called_once_with(
                ((manifest, context),),
                cards_dir=None,
                cache_only=True,
            )
            build_cards.assert_called_once_with((metadata,), cache_only=True)
            plan_flatpaks.assert_called_once_with(
                context,
                manifest_path=manifest,
                cache_only=True,
            )
            image_exists.assert_called_once_with(context.podman, plan.output_image)
            remove_tree.assert_called_once_with(
                context.dnf_workspace_dir,
                podman=context.podman,
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

    def test_prepare_ci_drops_already_built_flatpaks(self) -> None:
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
                patch("ludos.ci._image_exists", return_value=True),
                patch("ludos.ci.build_package_card_images"),
                patch("ludos.ci._remove_tree"),
                patch("ludos.ci.log"),
            ):
                output = prepare_ci((manifest,), cache_dir=cache)

            data = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertEqual(data["flatpaks"], {})

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
                patch("ludos.ci._remove_tree") as remove_tree,
            ):
                with self.assertRaisesRegex(ConfigError, "boom"):
                    prepare_ci((manifest,), cache_dir=cache)

            self.assertEqual(output.read_text(encoding="utf-8"), "existing: true\n")
            remove_tree.assert_called_once_with(
                context.dnf_workspace_dir,
                podman=context.podman,
            )

    def test_prepare_ci_requires_manifest(self) -> None:
        with self.assertRaisesRegex(ConfigError, "at least one manifest"):
            prepare_ci(tuple())

    def _context(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            root_dir=root,
            dnf_workspace_dir=root / "cache" / "f44-x86_64" / "dnf" / "run-test",
            podman="podman",
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
        )

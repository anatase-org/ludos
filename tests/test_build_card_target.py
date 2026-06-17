from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, sentinel

from ludos.build import (
    BuildImageOutputs,
    BuildImagePlan,
    PackageImagePlan,
    ResolvedBuildMetadata,
    build_manifest,
    _cleanup_dnf_workspaces,
    _build_final_manifest_image,
    _resolve_manifest_metadata,
    _resolve_cache_key,
)
from ludos.model import ConfigError


class TargetCardBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = self.root / "anatase.yml"
        self.manifest.write_text("version: 1\n", encoding="utf-8")
        self.scx_source = self.root / "cards" / "base" / "scx" / "card.yml"
        self.scx_source.parent.mkdir(parents=True)
        self.scx_source.write_text("version: 1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_targeted_build_accepts_manifest_card_path_forms(self) -> None:
        for selector in (
            "cards/base/scx",
            "cards/base/scx/card.yml",
            "cards/base/scx/card.yml:scx.spec",
        ):
            with self.subTest(selector=selector):
                metadata = self._metadata()
                build_outputs = BuildImageOutputs(
                    images_by_block=(
                        ("base-scx", "localhost/builds:f44-x86_64-base-scx"),
                    ),
                )

                with (
                    patch(
                        "ludos.build.resolve_build_manifests",
                        return_value=(metadata,),
                    ),
                    patch("ludos.build.build_package_card_images") as package_images,
                    patch("ludos.build.build_builder_images") as builder_images,
                    patch(
                        "ludos.build.build_build_images",
                        return_value=build_outputs,
                    ) as build_images,
                    patch("ludos.build.build_final_manifest_images") as final_images,
                ):
                    result = build_manifest(self.manifest, card=selector)

                package_images.assert_not_called()
                final_images.assert_not_called()
                builder_images.assert_called_once_with(
                    (metadata,),
                    targets=("base-scx",),
                    cache_only=False,
                )
                build_images.assert_called_once_with(
                    (metadata,),
                    targets=("base-scx",),
                    cache_only=False,
                )
                self.assertEqual(result.package_images, ())
                self.assertEqual(
                    result.build_images,
                    ("localhost/builds:f44-x86_64-base-scx",),
                )
                self.assertEqual(result.build_blocks, ("base-scx",))
                self.assertEqual(
                    result.builder_images,
                    ("localhost/builders:f44-x86_64-builder",),
                )

    def test_targeted_build_rejects_card_not_listed_in_manifest(self) -> None:
        with (
            patch(
                "ludos.build.resolve_build_manifests",
                return_value=(self._metadata(),),
            ),
            patch("ludos.build.build_builder_images") as builder_images,
            patch("ludos.build.build_build_images") as build_images,
        ):
            with self.assertRaisesRegex(ConfigError, "card not listed in manifest"):
                build_manifest(self.manifest, card="cards/base/missing")

        builder_images.assert_not_called()
        build_images.assert_not_called()

    def test_targeted_build_rejects_card_without_build_output(self) -> None:
        base_source = self.root / "cards" / "base" / "base.yml"
        base_source.parent.mkdir(parents=True, exist_ok=True)
        base_source.write_text("version: 1\n", encoding="utf-8")
        metadata = self._metadata(
            build_images=(),
            card_sources=(("base-base", str(base_source)),),
        )

        with (
            patch("ludos.build.resolve_build_manifests", return_value=(metadata,)),
            patch("ludos.build.build_builder_images") as builder_images,
            patch("ludos.build.build_build_images") as build_images,
        ):
            with self.assertRaisesRegex(ConfigError, "card has no build or specs"):
                build_manifest(self.manifest, card="cards/base/base.yml")

        builder_images.assert_not_called()
        build_images.assert_not_called()

    def test_targeted_metadata_skips_package_resolution_and_non_target_builders(self) -> None:
        self._write_build_manifest()
        hhd_source = self.root / "cards" / "gaming" / "hhd.yml"

        def resolve_packages(
            _base, _releasever, packages, package_id_by_nevra, *_args, **_kwargs
        ):
            resolved = tuple(f"{package}-0:1-1.fc44.x86_64" for package in packages)
            for package, resolved_package in zip(packages, resolved):
                package_id_by_nevra[resolved_package] = (package, "x86_64")
            return resolved

        with (
            patch("ludos.build.shutil.which", side_effect=lambda command: command),
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._extract_image_paths"),
            patch(
                "ludos.build._card_specs_hash",
                return_value=("scxhash", tuple()),
            ) as specs_hash,
            patch("ludos.build._stage_card_specs", return_value=tuple()),
            patch(
                "ludos.build._resolve_staged_spec_builder_packages",
                return_value=("spec-builddep",),
            ),
            patch(
                "ludos.build._resolve_packages",
                side_effect=resolve_packages,
            ) as resolve,
        ):
            metadata = _resolve_manifest_metadata(
                self.manifest,
                target_card="cards/base/scx",
            )

        self.assertEqual([plan.block for plan in metadata.build_images], ["base-scx"])
        self.assertEqual(metadata.requested_packages, ())
        self.assertEqual(metadata.package_images, ())
        self.assertEqual(metadata.bootstrap_packages, ())
        self.assertEqual(metadata.resolved_packages, ())
        self.assertEqual(resolve.call_count, 2)
        requested_packages = [call.args[2] for call in resolve.call_args_list]
        self.assertEqual(requested_packages[0], ("rpm-build",))
        self.assertEqual(requested_packages[1], ("rpm-build", "spec-builddep"))
        specs_hash.assert_called_once()
        self.assertEqual(Path(specs_hash.call_args.args[0]), self.scx_source)
        self.assertNotEqual(Path(specs_hash.call_args.args[0]), hhd_source)

    def test_spec_hash_is_not_part_of_builder_image_hash(self) -> None:
        self._write_build_manifest()

        def resolve_packages(
            _base, _releasever, packages, package_id_by_nevra, *_args, **_kwargs
        ):
            resolved = tuple(f"{package}-0:1-1.fc44.x86_64" for package in packages)
            for package, resolved_package in zip(packages, resolved):
                package_id_by_nevra[resolved_package] = (package, "x86_64")
            return resolved

        builder_images = []
        for spec_hash in ("first", "second"):
            with (
                patch("ludos.build.shutil.which", side_effect=lambda command: command),
                patch("ludos.build._image_exists", return_value=True),
                patch("ludos.build._extract_image_paths"),
                patch(
                    "ludos.build._card_specs_hash",
                    return_value=(spec_hash, tuple()),
                ),
                patch("ludos.build._stage_card_specs", return_value=tuple()),
                patch(
                    "ludos.build._resolve_staged_spec_builder_packages",
                    return_value=("spec-builddep",),
                ),
                patch("ludos.build._resolve_packages", side_effect=resolve_packages),
            ):
                metadata = _resolve_manifest_metadata(
                    self.manifest,
                    target_card="cards/base/scx",
                )
            builder_images.append(metadata.build_images[0].builder_image)

        self.assertEqual(builder_images[0], builder_images[1])

    def test_targeted_spec_filters_build_specs_only(self) -> None:
        self._write_build_manifest(
            scx_specs=(
                ("scx.spec", "scx"),
                ("scx-tools.spec", "scx-tools"),
            ),
        )

        def resolve_packages(
            _base, _releasever, packages, package_id_by_nevra, *_args, **_kwargs
        ):
            resolved = tuple(f"{package}-0:1-1.fc44.x86_64" for package in packages)
            for package, resolved_package in zip(packages, resolved):
                package_id_by_nevra[resolved_package] = (package, "x86_64")
            return resolved

        with (
            patch("ludos.build.shutil.which", side_effect=lambda command: command),
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._extract_image_paths"),
            patch("ludos.build._stage_card_specs", return_value=tuple()) as stage,
            patch(
                "ludos.build._resolve_staged_spec_builder_packages",
                return_value=("spec-builddep",),
            ),
            patch("ludos.build._resolve_packages", side_effect=resolve_packages),
        ):
            metadata = _resolve_manifest_metadata(
                self.manifest,
                target_card="cards/base/scx:scx-tools.spec",
            )

        self.assertEqual(
            tuple(spec.spec for spec in stage.call_args.kwargs["specs"]),
            ("scx.spec", "scx-tools.spec"),
        )
        self.assertEqual(
            tuple(
                spec.spec
                for _card, specs in metadata.card_specs
                for spec in specs
            ),
            ("scx-tools.spec",),
        )

    def test_targeted_spec_accepts_patch_source_key(self) -> None:
        self._write_build_manifest(
            scx_specs=(
                ("gamescope/gamescope.spec", "gamescope"),
                ("xserver/xorg-x11-server-Xwayland.spec", "xorg-x11-server-Xwayland"),
            ),
            scx_patch_specs={"xserver/xorg-x11-server-Xwayland.spec"},
        )

        def resolve_packages(
            _base, _releasever, packages, package_id_by_nevra, *_args, **_kwargs
        ):
            resolved = tuple(f"{package}-0:1-1.fc44.x86_64" for package in packages)
            for package, resolved_package in zip(packages, resolved):
                package_id_by_nevra[resolved_package] = (package, "x86_64")
            return resolved

        with (
            patch("ludos.build.shutil.which", side_effect=lambda command: command),
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._extract_image_paths"),
            patch("ludos.build._stage_card_specs", return_value=tuple()),
            patch(
                "ludos.build._resolve_staged_spec_builder_packages",
                return_value=("spec-builddep",),
            ),
            patch("ludos.build._resolve_packages", side_effect=resolve_packages),
        ):
            metadata = _resolve_manifest_metadata(
                self.manifest,
                target_card="cards/base/scx:xserver",
            )

        self.assertEqual(
            tuple(
                spec.spec
                for _card, specs in metadata.card_specs
                for spec in specs
            ),
            ("xserver/xorg-x11-server-Xwayland.spec",),
        )

    def test_metadata_uses_random_dnf_workspace(self) -> None:
        self._write_build_manifest()

        def resolve_packages(
            _base, _releasever, packages, package_id_by_nevra, *_args, **_kwargs
        ):
            resolved = tuple(f"{package}-0:1-1.fc44.x86_64" for package in packages)
            for package, resolved_package in zip(packages, resolved):
                package_id_by_nevra[resolved_package] = (package, "x86_64")
            return resolved

        with (
            patch("ludos.build.shutil.which", side_effect=lambda command: command),
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._extract_image_paths"),
            patch(
                "ludos.build._card_specs_hash",
                return_value=("scxhash", tuple()),
            ),
            patch("ludos.build._stage_card_specs", return_value=tuple()),
            patch(
                "ludos.build._resolve_staged_spec_builder_packages",
                return_value=("spec-builddep",),
            ),
            patch("ludos.build._resolve_packages", side_effect=resolve_packages),
        ):
            metadata = _resolve_manifest_metadata(self.manifest)

        try:
            workspace = Path(metadata.dnf_workspace_dir)
            self.assertEqual(
                workspace.parent,
                self.root / "cache" / "f44-x86_64" / "dnf",
            )
            self.assertTrue(workspace.name.startswith("run-"))
            self.assertEqual(Path(metadata.repo_dir), workspace / "repos")
            self.assertEqual(Path(metadata.dnf_cache_dir), workspace / "cache")
            self.assertEqual(Path(metadata.dnf_persist_dir), workspace / "persist")
            self.assertEqual(Path(metadata.dnf_log_dir), workspace / "log")
            self.assertTrue(workspace.exists())
        finally:
            _cleanup_dnf_workspaces((metadata,))

    def test_build_manifest_removes_dnf_workspace_after_success(self) -> None:
        metadata = self._metadata()
        workspace = Path(metadata.dnf_workspace_dir)
        workspace.mkdir(parents=True)

        with (
            patch("ludos.build.resolve_build_manifests", return_value=(metadata,)),
            patch("ludos.build.build_package_card_images"),
            patch(
                "ludos.build.build_build_images",
                return_value=BuildImageOutputs(),
            ),
            patch(
                "ludos.build.build_final_manifest_images",
                return_value=(sentinel.result,),
            ),
        ):
            result = build_manifest(self.manifest)

        self.assertIs(result, sentinel.result)
        self.assertFalse(workspace.exists())

    def test_build_manifest_removes_dnf_workspace_after_failure(self) -> None:
        metadata = self._metadata()
        workspace = Path(metadata.dnf_workspace_dir)
        workspace.mkdir(parents=True)

        with (
            patch("ludos.build.resolve_build_manifests", return_value=(metadata,)),
            patch(
                "ludos.build.build_package_card_images",
                side_effect=RuntimeError("boom"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                build_manifest(self.manifest)

        self.assertFalse(workspace.exists())

    def test_final_manifest_image_tags_latest(self) -> None:
        metadata = self._metadata()
        metadata = replace(
            metadata,
            common_packages=("bash-0:1-1.fc44.x86_64",),
            bootstrap_packages=("bash-0:1-1.fc44.x86_64",),
            package_images=(
                PackageImagePlan(
                    block="common",
                    packages=("bash-0:1-1.fc44.x86_64",),
                    image="localhost/cards:f44-x86_64-common",
                ),
                *metadata.package_images,
            ),
            package_blocks=(
                ("common", ("bash-0:1-1.fc44.x86_64",)),
                *metadata.package_blocks,
            ),
        )
        Path(metadata.build_dir).mkdir(parents=True)

        with (
            patch("ludos.build._run_container_build") as container_build,
            patch("ludos.build.subprocess.run") as run,
        ):
            result = _build_final_manifest_image(
                metadata,
                build_outputs=BuildImageOutputs(),
                mode="separated",
                cache_only=False,
            )

        container_build.assert_called_once()
        run.assert_called_once_with(
            [
                "podman",
                "tag",
                "localhost/anatase:f44-x86_64",
                "localhost/anatase:latest",
            ],
            check=True,
        )
        self.assertEqual(result.output_image, "localhost/anatase:f44-x86_64")

    def test_resolve_cache_key_ignores_random_dnf_workspace(self) -> None:
        first = [
            "podman",
            "run",
            "--volume",
            "/tmp/cache/f44-x86_64/dnf/run-first/repos:/ludos/dnf/repos:ro",
            "--volume",
            "/tmp/cache/f44-x86_64/dnf/run-first/cache:/ludos/dnf/cache",
            "dnf5",
            "repoquery",
        ]
        second = [
            "podman",
            "run",
            "--volume",
            "/tmp/cache/f44-x86_64/dnf/run-second/repos:/ludos/dnf/repos:ro",
            "--volume",
            "/tmp/cache/f44-x86_64/dnf/run-second/cache:/ludos/dnf/cache",
            "dnf5",
            "repoquery",
        ]

        self.assertEqual(
            _resolve_cache_key(first, ("repo:tag",)),
            _resolve_cache_key(second, ("repo:tag",)),
        )

    def _metadata(
        self,
        *,
        build_images: tuple[BuildImagePlan, ...] | None = None,
        card_sources: tuple[tuple[str, str], ...] | None = None,
    ) -> ResolvedBuildMetadata:
        if build_images is None:
            build_images = (
                BuildImagePlan(
                    block="base-scx",
                    image="localhost/builds:f44-x86_64-base-scx",
                    builder_image="localhost/builders:f44-x86_64-builder",
                    builder_packages=("rpm-build-0:1-1.fc44.x86_64",),
                ),
            )
        if card_sources is None:
            card_sources = (("base-scx", str(self.scx_source)),)

        cache = self.root / "cache"
        return ResolvedBuildMetadata(
            image="anatase",
            distro="f44-x86_64",
            releasever="44",
            arch="x86_64",
            root_dir=str(self.root),
            local_prefix="",
            orchestrator="localhost/orchestrator:f44-x86_64-base",
            output_image="localhost/anatase:f44-x86_64",
            manifest_labels=(),
            manifest_env=(),
            requested_packages=("jq",),
            resolved_packages=("jq-0:1-1.fc44.x86_64",),
            common_packages=(),
            bootstrap_packages=(),
            card_order=tuple(block for block, _source in card_sources),
            card_packages=(("base-scx", ("jq-0:1-1.fc44.x86_64",)),),
            card_resolutions=(),
            package_ids=(),
            package_images=(
                PackageImagePlan(
                    block="base-scx",
                    packages=("jq-0:1-1.fc44.x86_64",),
                    image="localhost/cards:f44-x86_64-base-scx",
                ),
            ),
            build_images=build_images,
            package_dir=str(cache / "packages"),
            repo_dir=str(cache / "repos"),
            cache_dir=str(cache),
            build_dir=str(cache / "build"),
            card_build_dir=str(cache / "cards"),
            spec_source_cache_dir=str(cache / "spec-sources"),
            build_artifact_cache_dir=str(cache / "artifacts"),
            ccache_dir=str(cache / "ccache"),
            dnf_workspace_dir=str(cache / "dnf-run"),
            dnf_cache_dir=str(cache / "dnf-cache"),
            dnf_persist_dir=str(cache / "dnf-persist"),
            dnf_log_dir=str(cache / "dnf-log"),
            dnf_resolve_dir=str(cache / "dnf-resolves"),
            podman="podman",
            buildah="buildah",
            cache_version="20260616",
            repo_images=(),
            orchestrator_dnf_base=(),
            package_blocks=(("base-scx", ("jq-0:1-1.fc44.x86_64",)),),
            card_file_sets=(),
            postprocess_blocks=(),
            card_envs=tuple((block, tuple()) for block, _source in card_sources),
            card_sources=card_sources,
            card_prepare_scripts=(),
            card_builds=(),
            card_specs=(),
            spec_source_revisions=(),
        )

    def _write_build_manifest(
        self,
        *,
        scx_specs: tuple[tuple[str, str], ...] = (("scx.spec", "scx"),),
        scx_patch_specs: set[str] | None = None,
    ) -> None:
        scx_patch_specs = scx_patch_specs or set()
        (self.root / "repos").mkdir()
        (self.root / "repos" / "fedora.repo").write_text(
            "[fedora]\nname=Fedora\nbaseurl=https://example.com\n",
            encoding="utf-8",
        )
        self.manifest.write_text(
            "\n".join(
                (
                    "version: 1",
                    "releasever: '44'",
                    "distro: f$releasever-$arch",
                    "orchestrator: quay.io/fedora/fedora:$releasever",
                    "bootstrap: cards/bootstrap.yml",
                    "repos:",
                    "  - repo: fedora",
                    "    priority: 10",
                    "cards:",
                    "  - cards/base/scx",
                    "  - cards/gaming/hhd.yml",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self._write_card(
            self.root / "cards" / "bootstrap.yml",
            (
                "version: 1",
                "packages:",
                "  - bash",
            ),
        )
        self._write_card(
            self.scx_source,
            (
                "version: 1",
                "packages:",
                "  - jq",
                "build-deps:",
                "  - rpm-build",
                "specs:",
                *tuple(
                    line
                    for spec_file, package in scx_specs
                    for line in (
                        f"  - spec: {spec_file}",
                        "    packages:",
                        f"      - {package}",
                        *(
                            (
                                "    patch:",
                                "      type: git",
                                "      url: https://example.com/source.git",
                                "      ref: v${spec:Version}",
                                "      file: overrides.patch",
                            )
                            if spec_file in scx_patch_specs
                            else ()
                        ),
                    )
                ),
            ),
        )
        for spec_file, package in scx_specs:
            spec_path = self.scx_source.parent / spec_file
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text(
                f"Name: {package}\nVersion: 1\n",
                encoding="utf-8",
            )
        self._write_card(
            self.root / "cards" / "gaming" / "hhd.yml",
            (
                "version: 1",
                "packages:",
                "  - hhd",
                "build-deps:",
                "  - cargo",
                "specs:",
                "  - spec: hhd.spec",
                "    packages:",
                "      - hhd",
            ),
        )

    def _write_card(self, path: Path, lines: tuple[str, ...]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

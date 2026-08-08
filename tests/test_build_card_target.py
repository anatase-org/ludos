from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch, sentinel

from ludos.build import (
    BuildImageOutputs,
    BuildImagePlan,
    CardBuildOutput,
    ImageInfo,
    OciImagePlan,
    PackageImagePlan,
    ResolvedBuildMetadata,
    build_package_card_images,
    build_build_images,
    build_manifest,
    _cleanup_dnf_workspaces,
    _build_final_manifest_image,
    _inspect_oci_image,
    _render_final_containerfile,
    _final_manifest_hash,
    _ensure_image,
    _resolve_manifest_metadata,
    _resolve_cache_key,
    resolve_build_manifest_context,
)
from ludos.model import Card, ConfigError


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
                    patch(
                        "ludos.build.build_build_images",
                        return_value=build_outputs,
                    ) as build_images,
                    patch("ludos.build.build_final_manifest_images") as final_images,
                ):
                    result = build_manifest(self.manifest, card=selector)

                package_images.assert_not_called()
                final_images.assert_not_called()
                build_images.assert_called_once_with(
                    (metadata,),
                    targets=("base-scx",),
                    cache_only=False,
                    create_builders=True,
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
            patch("ludos.build.build_build_images") as build_images,
        ):
            with self.assertRaisesRegex(ConfigError, "card not listed in manifest"):
                build_manifest(self.manifest, card="cards/base/missing")

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
            patch("ludos.build.build_build_images") as build_images,
        ):
            with self.assertRaisesRegex(ConfigError, "card has no build or specs"):
                build_manifest(self.manifest, card="cards/base/base.yml")

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
            patch("ludos.build._apply_repo_priority"),
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

    def test_manifest_labels_expand_manifest_env(self) -> None:
        self._write_build_manifest()
        contents = self.manifest.read_text(encoding="utf-8")
        self.manifest.write_text(
            contents.replace(
                "version: 1\n",
                "version: 1\n"
                "env:\n"
                "  dist: ''\n"
                "  tag: $releasever.$version$dist\n"
                "labels:\n"
                "  org.opencontainers.image.version: $tag\n"
                "  org.opencontainers.image.title: Anatase\n",
                1,
            ),
            encoding="utf-8",
        )
        (self.root / ".env").write_text("dist=.9\n", encoding="utf-8")
        hhd_card = self.root / "cards" / "gaming" / "hhd.yml"
        hhd_card.write_text(
            hhd_card.read_text(encoding="utf-8").replace(
                "version: 1\n",
                "version: 1\nenv:\n  tag: $tag\n",
                1,
            ),
            encoding="utf-8",
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
            patch("ludos.build._apply_repo_priority"),
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
            metadata = _resolve_manifest_metadata(
                self.manifest,
                target_card="cards/base/scx",
                cache_version="20260622",
            )

        self.assertIn(
            ("org.opencontainers.image.version", "44.20260622.9"),
            metadata.manifest_labels,
        )
        self.assertEqual(dict(metadata.manifest_env)["dist"], ".9")
        self.assertIn(
            ("org.opencontainers.image.version", "44.20260622"),
            metadata.cache_manifest_labels,
        )
        self.assertIn(
            ("org.opencontainers.image.title", "Anatase"),
            metadata.manifest_labels,
        )
        self.assertEqual(
            dict(dict(metadata.card_envs)["gaming-hhd"])["tag"],
            "44.20260622.9",
        )
        self.assertEqual(
            dict(dict(metadata.cache_card_envs)["gaming-hhd"])["tag"],
            "44.20260622",
        )

    def test_final_manifest_hash_uses_dist_neutral_labels_and_card_envs(self) -> None:
        cache_labels = (("org.opencontainers.image.version", "20260713"),)
        cache_card_envs = (("base-finalize", (("tag", "20260713"),)),)
        first = replace(
            self._metadata(),
            manifest_labels=(("org.opencontainers.image.version", "20260713.12"),),
            cache_manifest_labels=cache_labels,
            card_envs=(("base-finalize", (("tag", "20260713.12"),)),),
            cache_card_envs=cache_card_envs,
        )
        second = replace(
            first,
            manifest_labels=(("org.opencontainers.image.version", "20260713.13"),),
            card_envs=(("base-finalize", (("tag", "20260713.13"),)),),
        )

        self.assertEqual(
            _final_manifest_hash(first, mode="combined"),
            _final_manifest_hash(second, mode="combined"),
        )
    def test_final_manifest_hash_canonicalizes_package_order(self) -> None:
        first = replace(
            self._metadata(),
            common_packages=("zlib-1", "bash-1"),
            bootstrap_packages=("systemd-1", "filesystem-1"),
            card_packages=(("base-scx", ("jq-1", "curl-1")),),
            card_resolutions=(("base-scx", ("zlib-1", "jq-1", "curl-1")),),
            package_ids=(
                ("zlib-1", "zlib", "x86_64"),
                ("jq-1", "jq", "x86_64"),
            ),
            build_images=(
                BuildImagePlan(
                    block="base-scx",
                    image="localhost/builds:f44-x86_64-base-scx",
                    builder_image="localhost/builders:f44-x86_64-builder",
                    builder_packages=("rpm-build-1",),
                    declared_package_ids=(("zlib", "x86_64"), ("jq", "x86_64")),
                ),
            ),
            oci_images=(
                OciImagePlan(
                    block="base-scx",
                    name="example",
                    image="localhost/oci:example",
                    digest="sha256:example",
                    packages=("zlib-1", "jq-1"),
                    declared_package_ids=(("zlib", "x86_64"), ("jq", "x86_64")),
                ),
            ),
        )
        second = replace(
            first,
            common_packages=tuple(reversed(first.common_packages)),
            bootstrap_packages=tuple(reversed(first.bootstrap_packages)),
            card_packages=tuple(
                (block, tuple(reversed(packages)))
                for block, packages in first.card_packages
            ),
            card_resolutions=tuple(
                (block, tuple(reversed(packages)))
                for block, packages in first.card_resolutions
            ),
            package_ids=tuple(reversed(first.package_ids)),
            build_images=(
                replace(
                    first.build_images[0],
                    declared_package_ids=tuple(
                        reversed(first.build_images[0].declared_package_ids)
                    ),
                ),
            ),
            oci_images=(
                replace(
                    first.oci_images[0],
                    packages=tuple(reversed(first.oci_images[0].packages)),
                    declared_package_ids=tuple(
                        reversed(first.oci_images[0].declared_package_ids)
                    ),
                ),
            ),
        )

        self.assertEqual(
            _final_manifest_hash(first, mode="combined"),
            _final_manifest_hash(second, mode="combined"),
        )
        changed_oci_digest = replace(
            first,
            oci_images=(
                replace(first.oci_images[0], digest="sha256:changed"),
            ),
        )
        self.assertNotEqual(
            _final_manifest_hash(first, mode="combined"),
            _final_manifest_hash(changed_oci_digest, mode="combined"),
        )

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
                patch("ludos.build._apply_repo_priority"),
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
            patch("ludos.build._apply_repo_priority"),
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
            patch("ludos.build._apply_repo_priority"),
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
            patch("ludos.build._apply_repo_priority"),
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

    def test_metadata_resolves_builder_cards_in_thread_pool(self) -> None:
        self._write_build_manifest()
        main_thread = threading.get_ident()
        resolver_threads = set()

        def resolve_packages(
            _base, _releasever, packages, package_id_by_nevra, *_args, **_kwargs
        ):
            if "rpm-build" in packages or "cargo" in packages:
                resolver_threads.add(threading.get_ident())
            resolved = tuple(f"{package}-0:1-1.fc44.x86_64" for package in packages)
            for package, resolved_package in zip(packages, resolved):
                package_id_by_nevra[resolved_package] = (package, "x86_64")
            return resolved

        with (
            patch("ludos.build.shutil.which", side_effect=lambda command: command),
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._extract_image_paths"),
            patch("ludos.build._apply_repo_priority"),
            patch(
                "ludos.build._card_specs_hash",
                return_value=("spechash", tuple()),
            ),
            patch("ludos.build._stage_card_specs", return_value=tuple()),
            patch(
                "ludos.build._resolve_staged_spec_builder_packages",
                return_value=("spec-builddep",),
            ),
            patch("ludos.build._resolve_packages", side_effect=resolve_packages),
        ):
            metadata = _resolve_manifest_metadata(self.manifest, workers=2)

        try:
            self.assertEqual(
                [plan.block for plan in metadata.build_images],
                ["base-scx", "gaming-hhd"],
            )
            self.assertTrue(resolver_threads)
            self.assertNotIn(main_thread, resolver_threads)
        finally:
            _cleanup_dnf_workspaces((metadata,))

    def test_build_manifest_removes_dnf_workspace_after_success(self) -> None:
        metadata = self._metadata()
        workspace = Path(metadata.dnf_workspace_dir)
        workspace.mkdir(parents=True)

        with (
            patch("ludos.build.resolve_build_manifests", return_value=(metadata,)),
            patch("ludos.build.build_package_card_images") as package_images,
            patch(
                "ludos.build.build_build_images",
                return_value=BuildImageOutputs(),
            ) as build_images,
            patch(
                "ludos.build.build_final_manifest_images",
                return_value=(sentinel.result,),
            ) as final_images,
        ):
            result = build_manifest(self.manifest)

        self.assertIs(result, sentinel.result)
        package_images.assert_called_once()
        prepared_metadata = package_images.call_args.args[0]
        self.assertEqual(
            package_images.call_args.kwargs,
            {"cache_only": False, "include_builders": False},
        )
        build_images.assert_called_once_with(
            prepared_metadata,
            cache_only=False,
            create_builders=True,
        )
        self.assertTrue(final_images.call_args.kwargs["load_oci_images"])
        self.assertFalse(workspace.exists())

    def test_cached_build_output_does_not_load_builder(self) -> None:
        metadata = self._metadata()
        plan = metadata.build_images[0]

        with (
            patch("ludos.build._ensure_image", return_value=True) as ensure_image,
            patch(
                "ludos.build._output_metadata_in_image",
                return_value=(("base-scx-1-1.x86_64.rpm",), False),
            ),
            patch("ludos.build._prepare_builder_image") as prepare_builder,
        ):
            outputs = build_build_images((metadata,), create_builders=True)

        ensure_image.assert_called_once_with(
            metadata.podman,
            plan.image,
            metadata.ci_registry,
        )
        prepare_builder.assert_not_called()
        self.assertEqual(outputs.images_by_block, ((plan.block, plan.image),))

    def test_missing_build_output_creates_builder_when_requested(self) -> None:
        metadata = replace(
            self._metadata(),
            card_builds=(("base-scx", "true"),),
        )
        plan = metadata.build_images[0]
        build_output = CardBuildOutput(
            rpm_files=("base-scx-1-1.x86_64.rpm",),
        )

        with (
            patch("ludos.build._ensure_image", return_value=False),
            patch("ludos.build._prepare_builder_image") as prepare_builder,
            patch(
                "ludos.build._build_card_output_image",
                return_value=build_output,
            ),
        ):
            outputs = build_build_images((metadata,), create_builders=True)

        prepare_builder.assert_called_once_with(
            metadata,
            plan,
            cache_only=False,
        )
        self.assertEqual(outputs.images_by_block, ((plan.block, plan.image),))

    def test_build_images_default_still_requires_prepared_builder(self) -> None:
        metadata = self._metadata()

        with (
            patch("ludos.build._ensure_image", return_value=False),
            patch("ludos.build._prepare_builder_image") as prepare_builder,
        ):
            with self.assertRaisesRegex(ConfigError, "builder image is missing"):
                build_build_images((metadata,))

        prepare_builder.assert_not_called()

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
            patch("ludos.build._image_exists", return_value=False),
            patch("ludos.build._run_container_build") as container_build,
            patch("ludos.build.subprocess.run") as run,
        ):
            result = _build_final_manifest_image(
                metadata,
                build_outputs=BuildImageOutputs(),
                mode="separated",
                cache_only=False,
                build_cache="ghcr.io/anatase-org/cache",
            )

        container_build.assert_called_once()
        build_command = container_build.call_args.args[0]
        cache_from = build_command.index("--cache-from")
        self.assertEqual(
            build_command[cache_from : cache_from + 4],
            [
                "--cache-from",
                "ghcr.io/anatase-org/cache",
                "--cache-to",
                "ghcr.io/anatase-org/cache",
            ],
        )
        containerfile = Path(metadata.build_dir) / "Containerfile"
        self.assertIn(
            f'LABEL "org.anatase.ludos.tag"="{result.output_image.rsplit(":", 1)[-1]}"',
            containerfile.read_text(encoding="utf-8"),
        )
        run.assert_called_once_with(
            [
                "podman",
                "tag",
                result.output_image,
                "images:anatase",
            ],
            check=True,
        )
        self.assertRegex(
            result.output_image,
            r"^images:f44-x86_64-anatase-[0-9a-f]{8}$",
        )
        self.assertEqual(result.latest_image, "images:anatase")

    def test_package_card_image_reuses_remote_cache_hit(self) -> None:
        metadata = replace(
            self._metadata(),
            ci_registry="ghcr.io/anatase-org",
            package_images=(
                PackageImagePlan(
                    block="base-scx",
                    packages=("jq-0:1-1.fc44.x86_64",),
                    image="cards:f44-x86_64-base-scx",
                ),
            ),
        )

        with (
            patch("ludos.build._ensure_image", return_value=True) as ensure_image,
            patch("ludos.build._download_block_packages") as download,
            patch("ludos.build._create_package_image") as create_image,
        ):
            build_package_card_images((metadata,), cache_only=False)

        ensure_image.assert_any_call(
            "podman",
            "cards:f44-x86_64-base-scx",
            "ghcr.io/anatase-org",
        )
        download.assert_not_called()
        create_image.assert_not_called()

    def test_install_heredocs_include_build_image_refs_for_cache(self) -> None:
        metadata = self._metadata()
        common_packages = ("bash-0:1-1.fc44.x86_64",)
        package_blocks = (
            ("common", common_packages),
            *metadata.package_blocks,
        )
        package_images_by_block = {
            "common": "localhost/cards:f44-x86_64-common-11111111",
            "base-scx": "localhost/cards:f44-x86_64-base-scx-22222222",
        }
        build_images_by_block = {
            "base-scx": "localhost/builds:f44-x86_64-base-scx-33333333",
        }
        metadata = replace(
            metadata,
            bootstrap_packages=common_packages,
            package_ids=(("jq-0:1-1.fc44.x86_64", "jq", "x86_64"),),
        )

        containerfile = _render_final_containerfile(
            metadata,
            mode="separated",
            package_blocks=package_blocks,
            package_images_by_block=package_images_by_block,
            build_images_by_block=build_images_by_block,
            build_rpm_files_by_block={"base-scx": ("base-scx-1-1.x86_64.rpm",)},
            card_file_cards=set(),
            build_file_blocks=set(),
        )

        self.assertNotIn(
            "# build-image: f44-x86_64-common-11111111",
            containerfile,
        )
        self.assertNotIn(
            "# build-image: f44-x86_64-base-scx-22222222",
            containerfile,
        )
        self.assertIn(
            "# build-image: f44-x86_64-base-scx-33333333",
            containerfile,
        )

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

    def test_card_parses_oci_strings_as_packages_and_mappings_as_inputs(self) -> None:
        card_path = self.root / "oci-card.yml"
        card_path.write_text(
            "\n".join(
                (
                    "version: 1",
                    "packages:",
                    "  - bash",
                    "oci:",
                    "  - efibootmgr",
                    "  - oci: kernel",
                    "    packages:",
                    "      x86_64:",
                    "        - kernel-core",
                    "        - kernel-devel",
                    "    env:",
                    "      nvidia: ${label:org.anatase.kernel.nvidia}",
                    "",
                )
            ),
            encoding="utf-8",
        )

        card = Card.from_file(card_path)

        self.assertEqual(card.packages["*"], ("bash", "efibootmgr"))
        self.assertEqual(len(card.oci), 1)
        self.assertEqual(card.oci[0].oci, "kernel")
        self.assertEqual(
            card.oci[0].packages["x86_64"],
            ("kernel-core", "kernel-devel"),
        )
        self.assertEqual(
            card.oci[0].env["nvidia"],
            "${label:org.anatase.kernel.nvidia}",
        )

    def test_card_rejects_invalid_oci_shape(self) -> None:
        card_path = self.root / "bad-oci-card.yml"
        card_path.write_text("version: 1\noci: kernel\n", encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "'oci' must be a list"):
            Card.from_file(card_path)

    def test_oci_label_env_is_inherited_by_later_cards(self) -> None:
        self._write_oci_manifest()

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
            patch("ludos.build._apply_repo_priority"),
            patch(
                "ludos.build._inspect_oci_image",
                return_value=ImageInfo(
                    digest="sha256:kernel111",
                    labels={"org.anatase.kernel.nvidia": "580.95.05"},
                ),
            ),
            patch("ludos.build._output_metadata_in_image") as oci_metadata,
            patch(
                "ludos.build._card_specs_hash",
                return_value=("nvidiaspechash", tuple()),
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
                target_card="cards/drivers/nvidia/nvidia.yml",
            )

        card_envs = {name: dict(values) for name, values in metadata.card_envs}
        self.assertEqual(card_envs["base-kernel"]["nvidia"], "580.95.05")
        self.assertEqual(card_envs["drivers-nvidia"]["version"], "580.95.05")
        self.assertEqual(metadata.oci_images[0].image, "kernel:f44-x86_64")
        oci_metadata.assert_not_called()

    def test_oci_digest_does_not_change_owning_build_image_hash(self) -> None:
        self._write_oci_manifest(scx_uses_oci=True)

        def resolve_packages(
            _base, _releasever, packages, package_id_by_nevra, *_args, **_kwargs
        ):
            resolved = tuple(f"{package}-0:1-1.fc44.x86_64" for package in packages)
            for package, resolved_package in zip(packages, resolved):
                package_id_by_nevra[resolved_package] = (package, "x86_64")
            return resolved

        build_images = []
        oci_digests = []
        for digest in ("sha256:first", "sha256:second"):
            with (
                patch("ludos.build.shutil.which", side_effect=lambda command: command),
                patch("ludos.build._image_exists", return_value=True),
                patch("ludos.build._extract_image_paths"),
                patch("ludos.build._apply_repo_priority"),
                patch(
                    "ludos.build._inspect_oci_image",
                    return_value=ImageInfo(digest=digest, labels={}),
                ),
                patch("ludos.build._output_metadata_in_image") as oci_metadata,
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
            build_images.append(metadata.build_images[0].image)
            oci_digests.append(metadata.oci_images[0].digest)
            oci_metadata.assert_not_called()

        self.assertEqual(build_images[0], build_images[1])
        self.assertEqual(oci_digests, ["sha256:first", "sha256:second"])

    def test_missing_oci_label_is_rejected(self) -> None:
        self._write_oci_manifest()

        with (
            patch("ludos.build.shutil.which", side_effect=lambda command: command),
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._extract_image_paths"),
            patch("ludos.build._apply_repo_priority"),
            patch(
                "ludos.build._inspect_oci_image",
                return_value=ImageInfo(digest="sha256:kernel111", labels={}),
            ),
            patch("ludos.build._output_metadata_in_image") as oci_metadata,
        ):
            with self.assertRaisesRegex(ConfigError, "does not define label"):
                _resolve_manifest_metadata(
                    self.manifest,
                    target_card="cards/drivers/nvidia/nvidia.yml",
                )
            oci_metadata.assert_not_called()

    def test_missing_oci_image_is_rejected(self) -> None:
        with patch("ludos.build._image_exists", return_value=False):
            with self.assertRaisesRegex(ConfigError, "OCI image is not cached"):
                _inspect_oci_image(
                    "podman",
                    "localhost/kernel:f44-x86_64",
                    source=self.scx_source,
                )

    def test_build_context_checks_ci_for_repo_and_orchestrator_images(self) -> None:
        context = sentinel.context
        with patch(
            "ludos.build.resolve_manifest_context",
            return_value=context,
        ) as resolve:
            result = resolve_build_manifest_context(
                self.manifest,
                check_ci_cache=True,
            )

        self.assertIs(result, context)
        image_exists = resolve.call_args.kwargs["image_exists"]
        with patch("ludos.build._ensure_image", return_value=True) as ensure:
            self.assertTrue(
                image_exists(
                    "podman",
                    "orchestrator:f44-x86_64-base",
                    "ghcr.io/anatase-org",
                )
            )

        ensure.assert_called_once_with(
            "podman",
            "orchestrator:f44-x86_64-base",
            "ghcr.io/anatase-org",
            check_ci=True,
            source=self.manifest,
        )

    def test_changed_ci_context_image_is_replaced_when_confirmed(self) -> None:
        local = ImageInfo(digest="sha256:local", labels={})
        remote = ImageInfo(digest="sha256:remote", labels={})
        with (
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._inspect_local_oci_image", return_value=local),
            patch("ludos.build._try_inspect_remote_oci_image", return_value=remote),
            patch("ludos.build.confirm", return_value=True) as confirm_replace,
            patch("ludos.build._replace_local_oci_image") as replace_image,
        ):
            exists = _ensure_image(
                "podman",
                "repos:f44-fedora",
                "ghcr.io/anatase-org",
                check_ci=True,
                source=self.manifest,
            )

        self.assertTrue(exists)
        confirm_replace.assert_called_once_with(
            "CI cache for repos:f44-fedora changed from sha256:local "
            "to sha256:remote. Replace the local image?",
            default=True,
        )
        replace_image.assert_called_once_with(
            "podman",
            "repos:f44-fedora",
            "ghcr.io/anatase-org/repos@sha256:remote",
            source=self.manifest,
        )

    def test_changed_ci_context_image_is_kept_when_declined(self) -> None:
        local = ImageInfo(digest="sha256:local", labels={})
        remote = ImageInfo(digest="sha256:remote", labels={})
        with (
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._inspect_local_oci_image", return_value=local),
            patch("ludos.build._try_inspect_remote_oci_image", return_value=remote),
            patch("ludos.build.confirm", return_value=False),
            patch("ludos.build._replace_local_oci_image") as replace_image,
        ):
            exists = _ensure_image(
                "podman",
                "orchestrator:f44-x86_64-base",
                "ghcr.io/anatase-org",
                check_ci=True,
                source=self.manifest,
            )

        self.assertTrue(exists)
        replace_image.assert_not_called()

    def test_remote_oci_image_is_inspected_with_skopeo(self) -> None:
        with (
            patch("ludos.build._image_exists", return_value=False),
            patch("ludos.build.shutil.which", return_value="skopeo"),
            patch("ludos.build._ensure_cached_image") as ensure,
            patch(
                "ludos.build.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=(
                        '{"Digest":"sha256:kernel111",'
                        '"Labels":{"org.anatase.kernel.nvidia":"580.95.05"}}'
                    ),
                ),
            ) as run,
        ):
            info = _inspect_oci_image(
                "podman",
                "kernel:f44-x86_64",
                source=self.scx_source,
                ci_registry="ghcr.io/anatase-org",
            )

        self.assertEqual(info.digest, "sha256:kernel111")
        self.assertEqual(info.labels["org.anatase.kernel.nvidia"], "580.95.05")
        ensure.assert_not_called()
        run.assert_called_once_with(
            [
                "skopeo",
                "inspect",
                "--no-tags",
                "docker://ghcr.io/anatase-org/kernel:f44-x86_64",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_changed_ci_oci_image_is_replaced_when_confirmed(self) -> None:
        local = ImageInfo(digest="sha256:local", labels={})
        remote = ImageInfo(digest="sha256:remote", labels={})
        with (
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._inspect_local_oci_image", return_value=local),
            patch("ludos.build._try_inspect_remote_oci_image", return_value=remote),
            patch("ludos.build.confirm", return_value=True) as confirm_replace,
            patch("ludos.build._replace_local_oci_image") as replace_image,
        ):
            info = _inspect_oci_image(
                "podman",
                "kernel:f44-x86_64",
                source=self.scx_source,
                ci_registry="ghcr.io/anatase-org",
                check_ci=True,
            )

        self.assertEqual(info, remote)
        confirm_replace.assert_called_once_with(
            "CI cache for kernel:f44-x86_64 changed from sha256:local "
            "to sha256:remote. Replace the local image?",
            default=True,
        )
        replace_image.assert_called_once_with(
            "podman",
            "kernel:f44-x86_64",
            "ghcr.io/anatase-org/kernel@sha256:remote",
            source=self.scx_source,
        )

    def test_changed_ci_oci_image_is_kept_when_declined(self) -> None:
        local = ImageInfo(digest="sha256:local", labels={})
        remote = ImageInfo(digest="sha256:remote", labels={})
        with (
            patch("ludos.build._image_exists", return_value=True),
            patch("ludos.build._inspect_local_oci_image", return_value=local),
            patch("ludos.build._try_inspect_remote_oci_image", return_value=remote),
            patch("ludos.build.confirm", return_value=False),
            patch("ludos.build._replace_local_oci_image") as replace_image,
        ):
            info = _inspect_oci_image(
                "podman",
                "kernel:f44-x86_64",
                source=self.scx_source,
                ci_registry="ghcr.io/anatase-org",
                check_ci=True,
            )

        self.assertEqual(info, local)
        replace_image.assert_not_called()

    def test_final_build_rejects_missing_oci_package(self) -> None:
        metadata = replace(
            self._metadata(),
            oci_images=(
                OciImagePlan(
                    block="base-scx",
                    name="kernel",
                    image="localhost/kernel:f44-x86_64",
                    digest="sha256:kernel111",
                    packages=("kernel-core",),
                    declared_package_ids=(("kernel-core", "x86_64"),),
                ),
            ),
        )
        Path(metadata.build_dir).mkdir(parents=True)

        with (
            patch(
                "ludos.build._ensure_image",
                side_effect=(False, True),
            ) as ensure_image,
            patch(
                "ludos.build._output_metadata_in_image",
                return_value=(tuple(), False),
            ),
            patch("ludos.build._run_container_build") as container_build,
        ):
            with self.assertRaisesRegex(
                ConfigError,
                "does not contain listed package 'kernel-core'",
            ):
                _build_final_manifest_image(
                    metadata,
                    build_outputs=BuildImageOutputs(),
                    mode="separated",
                    cache_only=False,
                    load_oci_images=True,
                )

        self.assertEqual(len(ensure_image.call_args_list), 2)
        self.assertEqual(
            ensure_image.call_args_list[1],
            call(
                metadata.podman,
                "localhost/kernel:f44-x86_64",
                metadata.ci_registry,
            ),
        )
        container_build.assert_not_called()

    def test_containerfile_mounts_only_selected_oci_rpms_and_files(self) -> None:
        metadata = self._metadata()
        bootstrap_packages = ("bash-0:1-1.fc44.x86_64",)
        common_packages = ("kernel-core-0:1-1.fc44.x86_64",)
        metadata = replace(
            metadata,
            bootstrap_packages=bootstrap_packages,
            common_packages=common_packages,
            card_resolutions=(
                (
                    "base-scx",
                    (
                        "kernel-core-0:1-1.fc44.x86_64",
                        "jq-0:1-1.fc44.x86_64",
                    ),
                ),
            ),
            package_ids=(
                ("kernel-core-0:1-1.fc44.x86_64", "kernel-core", "x86_64"),
                ("jq-0:1-1.fc44.x86_64", "jq", "x86_64"),
            ),
            postprocess_blocks=(("base-scx", "touch /files/seen"),),
            oci_images=(
                OciImagePlan(
                    block="base-scx",
                    name="kernel",
                    image="localhost/kernel:f44-x86_64",
                    digest="sha256:kernel111",
                    packages=("kernel-core",),
                    declared_package_ids=(("kernel-core", "x86_64"),),
                ),
            ),
        )
        package_blocks = (
            ("common", (*bootstrap_packages, *common_packages)),
            *metadata.package_blocks,
        )

        containerfile = _render_final_containerfile(
            metadata,
            mode="separated",
            package_blocks=package_blocks,
            package_images_by_block={
                "common": "localhost/cards:f44-x86_64-common-11111111",
                "base-scx": "localhost/cards:f44-x86_64-base-scx-22222222",
            },
            build_images_by_block={},
            build_rpm_files_by_block={},
            card_file_cards=set(),
            build_file_blocks=set(),
            oci_rpm_files_by_index={
                0: ("kernel-core-1-1.x86_64.rpm",),
            },
            oci_file_indexes={0},
        )

        self.assertIn("FROM localhost/kernel:f44-x86_64 AS oci_base_scx_kernel_0", containerfile)
        self.assertIn("/rpms/base_scx-oci-kernel/kernel-core-1-1.x86_64.rpm", containerfile)
        self.assertNotIn("/rpms/common/kernel-core-0:1-1.fc44.x86_64.rpm", containerfile)
        self.assertNotIn("kernel-extra-1-1.x86_64.rpm", containerfile)
        self.assertIn("from=oci_base_scx_kernel_0,source=/files,target=/ludos/oci-files/0,ro", containerfile)
        self.assertIn("# build-image: sha256:kernel111", containerfile)

    def test_combined_containerfile_omits_card_rpm_replaced_by_later_build(self) -> None:
        metadata = replace(
            self._metadata(),
            card_order=("de-kde", "gaming-gamemode"),
            card_packages=(
                ("de-kde", ("xwayland-0:1-1.fc44.x86_64",)),
                ("gaming-gamemode", tuple()),
            ),
            package_ids=(
                ("xwayland-0:1-1.fc44.x86_64", "xwayland", "x86_64"),
            ),
            build_images=(
                BuildImagePlan(
                    block="gaming-gamemode",
                    image="localhost/builds:f44-x86_64-gaming-gamemode",
                    builder_image="localhost/builders:f44-x86_64-builder",
                    builder_packages=tuple(),
                    declared_package_ids=(("xwayland", "x86_64"),),
                ),
            ),
        )
        package_blocks = (
            ("common", tuple()),
            ("de-kde", ("xwayland-0:1-1.fc44.x86_64",)),
            ("gaming-gamemode", tuple()),
        )

        containerfile = _render_final_containerfile(
            metadata,
            mode="combined",
            package_blocks=package_blocks,
            package_images_by_block={
                "common": "localhost/cards:f44-x86_64-common-11111111",
                "de-kde": "localhost/cards:f44-x86_64-de-kde-22222222",
            },
            build_images_by_block={
                "gaming-gamemode": "localhost/builds:f44-x86_64-gaming-gamemode",
            },
            build_rpm_files_by_block={
                "gaming-gamemode": ("xwayland-1-1.x86_64.rpm",),
            },
            card_file_cards=set(),
            build_file_blocks=set(),
        )

        self.assertNotIn(
            "/rpms/de_kde/xwayland-0:1-1.fc44.x86_64.rpm",
            containerfile,
        )
        self.assertIn(
            "/rpms/gaming_gamemode-build/xwayland-1-1.x86_64.rpm",
            containerfile,
        )

    def test_postprocess_heredoc_defines_requested_card_env(self) -> None:
        metadata = replace(
            self._metadata(),
            postprocess_blocks=(
                ("base-scx", "set -eux\nprintf '%s\\n' \"$distro\""),
            ),
            card_envs=(
                (
                    "base-scx",
                    (
                        ("arch", "x86_64"),
                        ("distro", "f44-x86_64"),
                        ("releasever", "44"),
                    ),
                ),
            ),
        )
        package_blocks = (("common", tuple()),)

        for mode in ("separated", "combined"):
            with self.subTest(mode=mode):
                containerfile = _render_final_containerfile(
                    metadata,
                    mode=mode,
                    package_blocks=package_blocks,
                    package_images_by_block={
                        "common": "localhost/cards:f44-x86_64-common-11111111",
                    },
                    build_images_by_block={},
                    build_rpm_files_by_block={},
                    card_file_cards=set(),
                    build_file_blocks=set(),
                    oci_rpm_files_by_index={},
                    oci_file_indexes=set(),
                )

                self.assertIn(
                    "arch=x86_64\ndistro=f44-x86_64\nreleasever=44\n",
                    containerfile,
                )
                if mode == "separated":
                    self.assertIn("releasever=44\nset -eux\n", containerfile)
                else:
                    self.assertIn("releasever=44\nrm -rf /files\n", containerfile)

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
            oci_images=(),
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

    def _write_oci_manifest(self, *, scx_uses_oci: bool = False) -> None:
        (self.root / "repos").mkdir()
        (self.root / "repos" / "fedora.repo").write_text(
            "[fedora]\nname=Fedora\nbaseurl=https://example.com\n",
            encoding="utf-8",
        )
        cards = ["  - cards/base/kernel"]
        if scx_uses_oci:
            cards.append("  - cards/base/scx")
        else:
            cards.append("  - cards/drivers/nvidia/nvidia.yml")
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
                    *cards,
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
        kernel_lines = [
            "version: 1",
            "oci:",
            "  - oci: kernel",
            "    packages:",
            "      - kernel-core",
        ]
        if not scx_uses_oci:
            kernel_lines.extend(
                (
                    "    env:",
                    "      nvidia: ${label:org.anatase.kernel.nvidia}",
                )
            )
        self._write_card(
            self.root / "cards" / "base" / "kernel.yml",
            tuple(kernel_lines),
        )
        if scx_uses_oci:
            self._write_card(
                self.scx_source,
                (
                    "version: 1",
                    "oci:",
                    "  - oci: kernel",
                    "    packages:",
                    "      - kernel-core",
                    "build-deps:",
                    "  - rpm-build",
                    "specs:",
                    "  - spec: scx.spec",
                    "    packages:",
                    "      - scx",
                ),
            )
            (self.scx_source.parent / "scx.spec").write_text(
                "Name: scx\nVersion: 1\n",
                encoding="utf-8",
            )
            return
        nvidia_source = self.root / "cards" / "drivers" / "nvidia" / "nvidia.yml"
        self._write_card(
            nvidia_source,
            (
                "version: 1",
                "env:",
                "  version: $nvidia",
                "build-deps:",
                "  - rpm-build",
                "specs:",
                "  - spec: nvidia-driver.spec",
                "    packages:",
                "      - nvidia-driver",
            ),
        )
        (nvidia_source.parent / "nvidia-driver.spec").write_text(
            "Name: nvidia-driver\nVersion: 1\n",
            encoding="utf-8",
        )

    def _write_card(self, path: Path, lines: tuple[str, ...]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ludos.__main__ import build_parser, cleanup_command
from ludos.cleanup import (
    CleanupTarget,
    _installer_latest_from_image_latest,
    _keep_named_image,
    _manifest_cleanup_targets,
    _purge_local_images,
    _stale_local_images,
    cleanup_local_images,
)


class CleanupCommandTests(unittest.TestCase):
    def test_parser_accepts_purge(self) -> None:
        args = build_parser().parse_args(
            [
                "cleanup",
                "--purge",
                "--dry-run",
                "--local-prefix",
                "test-",
                "anatase.yml",
            ]
        )

        self.assertEqual(args.command, "cleanup")
        self.assertTrue(args.purge)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.local_prefix, "test-")
        self.assertEqual(args.manifests, [Path("anatase.yml")])

    def test_parser_accepts_cache(self) -> None:
        args = build_parser().parse_args(["cleanup", "--cache", "anatase.yml"])

        self.assertTrue(args.cache)
        self.assertEqual(args.manifests, [Path("anatase.yml")])

    def test_cleanup_command_passes_purge(self) -> None:
        args = build_parser().parse_args(["cleanup", "--purge", "anatase.yml"])

        with patch("ludos.__main__.cleanup_local_images", return_value=0) as cleanup:
            self.assertEqual(cleanup_command(args), 0)

        cleanup.assert_called_once_with(
            version=None,
            local_prefix="",
            manifests=(Path("anatase.yml"),),
            dry_run=False,
            purge=True,
            cache_only=False,
        )

    def test_cleanup_command_passes_cache_only(self) -> None:
        args = build_parser().parse_args(["cleanup", "--cache", "anatase.yml"])

        with patch("ludos.__main__.cleanup_local_images", return_value=0) as cleanup:
            self.assertEqual(cleanup_command(args), 0)

        cleanup.assert_called_once_with(
            version=None,
            local_prefix="",
            manifests=(Path("anatase.yml"),),
            dry_run=False,
            purge=False,
            cache_only=True,
        )


class CleanupImageKeepTests(unittest.TestCase):
    def test_keeps_orchestrator_latest_tag(self) -> None:
        self.assertTrue(
            _keep_named_image(
                "localhost/orchestrator:latest",
                "localhost/orchestrator",
                "latest",
                {"localhost/orchestrator", "localhost/repos"},
                {"localhost/orchestrator"},
                {"localhost/cards"},
                set(),
                set(),
                "-20260616",
            )
        )

    def test_removes_other_stale_versioned_tags(self) -> None:
        self.assertFalse(
            _keep_named_image(
                "localhost/orchestrator:fedora-base-20260615",
                "localhost/orchestrator",
                "fedora-base-20260615",
                {"localhost/orchestrator", "localhost/repos"},
                {"localhost/orchestrator"},
                {"localhost/cards"},
                set(),
                set(),
                "-20260616",
            )
        )

    def test_removes_installer_cache_tags(self) -> None:
        self.assertFalse(
            _keep_named_image(
                "localhost/installer:cache-oci-anatase-f44_x86_64",
                "localhost/installer",
                "cache-oci-anatase-f44_x86_64",
                {"localhost/orchestrator", "localhost/repos"},
                {"localhost/orchestrator", "localhost/installer"},
                {"localhost/cards", "localhost/builds", "localhost/builders", "localhost/installer"},
                set(),
                set(),
                "-20260616",
            )
        )

    def test_keeps_installer_latest_tag(self) -> None:
        self.assertTrue(
            _keep_named_image(
                "localhost/installer:latest",
                "localhost/installer",
                "latest",
                {"localhost/orchestrator", "localhost/repos"},
                {"localhost/orchestrator", "localhost/installer"},
                {"localhost/cards", "localhost/builds", "localhost/builders", "localhost/installer"},
                set(),
                set(),
                "-20260616",
            )
        )

    def test_keeps_flatpak_build_output_images(self) -> None:
        self.assertTrue(
            _keep_named_image(
                "localhost/builds:f44-x86_64-flatpak-browser-abc123",
                "localhost/builds",
                "f44-x86_64-flatpak-browser-abc123",
                {"localhost/orchestrator", "localhost/repos"},
                {"localhost/orchestrator", "localhost/installer"},
                {"localhost/cards", "localhost/builds", "localhost/builders", "localhost/installer"},
                set(),
                {"localhost/builds:f44-x86_64-flatpak-browser-abc123"},
                "-20260616",
            )
        )

    def test_keeps_flatpak_builder_images(self) -> None:
        self.assertTrue(
            _keep_named_image(
                "localhost/builders:f44-x86_64-flatpak-browser-def456",
                "localhost/builders",
                "f44-x86_64-flatpak-browser-def456",
                {"localhost/orchestrator", "localhost/repos"},
                {"localhost/orchestrator", "localhost/installer"},
                {"localhost/cards", "localhost/builds", "localhost/builders", "localhost/installer"},
                set(),
                {"localhost/builders:f44-x86_64-flatpak-browser-def456"},
                "-20260616",
            )
        )

    def test_installer_latest_uses_image_alias_name(self) -> None:
        self.assertEqual(
            _installer_latest_from_image_latest("localhost/images:anatase"),
            "localhost/installers:anatase",
        )
        self.assertEqual(
            _installer_latest_from_image_latest("localhost/test-images:anatase"),
            "localhost/test-installers:anatase",
        )

    def test_removes_stale_flatpak_cache_images(self) -> None:
        self.assertFalse(
            _keep_named_image(
                "localhost/builders:f44-x86_64-flatpak-browser-oldhash",
                "localhost/builders",
                "f44-x86_64-flatpak-browser-oldhash",
                {"localhost/orchestrator", "localhost/repos"},
                {"localhost/orchestrator", "localhost/installer"},
                {"localhost/cards", "localhost/builds", "localhost/builders", "localhost/installer"},
                set(),
                {"localhost/builders:f44-x86_64-flatpak-browser-current"},
                "-20260616",
            )
        )

    def test_manifest_targets_include_current_flatpak_cache_images(self) -> None:
        build_result = SimpleNamespace(
            output_image="localhost/anatase:f44-x86_64",
            orchestrator="localhost/orchestrator:f44-x86_64-base-20260616",
            repo_images=("localhost/repos:f44-x86_64-fedora-20260616",),
            package_images=("localhost/cards:f44-x86_64-base-abc123",),
            build_images=("localhost/builds:f44-x86_64-scx-def456",),
            builder_images=("localhost/builders:f44-x86_64-ghi789",),
        )
        flatpak_result = SimpleNamespace(
            output_images=("localhost/flatpaks:f44-x86_64-browser",),
            build_images=("localhost/builds:f44-x86_64-flatpak-browser-jkl012",),
            builder_images=("localhost/builders:f44-x86_64-flatpak-browser-mno345",),
        )

        with (
            patch("ludos.cleanup.resolve_manifest_images", return_value=build_result),
            patch(
                "ludos.cleanup.resolve_manifest_flatpak_images",
                return_value=flatpak_result,
            ),
        ):
            targets = _manifest_cleanup_targets(Path("anatase.yml"), "20260616")

        self.assertIn("localhost/builds:f44-x86_64-flatpak-browser-jkl012", targets)
        self.assertIn("localhost/builders:f44-x86_64-flatpak-browser-mno345", targets)
        self.assertIn("localhost/flatpaks:f44-x86_64-browser", targets)

    def test_manifest_targets_allow_cache_creation_by_default(self) -> None:
        build_result = SimpleNamespace(
            output_image="localhost/images:f44-anatase-abc12345",
            latest_image="localhost/images:anatase",
            orchestrator="localhost/orchestrator:f44-x86_64-base-2026.27",
            repo_images=(),
            package_images=(),
            build_images=(),
            builder_images=(),
        )
        flatpak_result = SimpleNamespace(
            output_images=(),
            latest_images=(),
            build_images=(),
            builder_images=(),
        )

        with (
            patch("ludos.cleanup.resolve_manifest_images", return_value=build_result) as resolve_images,
            patch(
                "ludos.cleanup.resolve_manifest_flatpak_images",
                return_value=flatpak_result,
            ) as resolve_flatpaks,
        ):
            targets = _manifest_cleanup_targets(Path("anatase.yml"), "2026.27")

        resolve_images.assert_called_once_with(
            Path("anatase.yml"),
            cache_version="2026.27",
            cache_only=False,
        )
        resolve_flatpaks.assert_called_once_with(
            Path("anatase.yml"),
            cache_version="2026.27",
            cache_only=False,
        )
        self.assertIn("localhost/installers:anatase", targets)

    def test_manifest_targets_can_require_cache(self) -> None:
        build_result = SimpleNamespace(
            output_image="localhost/images:f44-anatase-abc12345",
            latest_image="localhost/images:anatase",
            orchestrator="localhost/orchestrator:f44-x86_64-base-2026.27",
            repo_images=(),
            package_images=(),
            build_images=(),
            builder_images=(),
        )
        flatpak_result = SimpleNamespace(
            output_images=(),
            latest_images=(),
            build_images=(),
            builder_images=(),
        )

        with (
            patch("ludos.cleanup.resolve_manifest_images", return_value=build_result) as resolve_images,
            patch(
                "ludos.cleanup.resolve_manifest_flatpak_images",
                return_value=flatpak_result,
            ) as resolve_flatpaks,
        ):
            _manifest_cleanup_targets(
                Path("anatase.yml"),
                "2026.27",
                cache_only=True,
            )

        resolve_images.assert_called_once_with(
            Path("anatase.yml"),
            cache_version="2026.27",
            cache_only=True,
        )
        resolve_flatpaks.assert_called_once_with(
            Path("anatase.yml"),
            cache_version="2026.27",
            cache_only=True,
        )

    def test_stale_local_images_removes_hashed_installer_tags(self) -> None:
        images = [
            {
                "Id": "current",
                "Names": [
                    "localhost/installers:anatase",
                    "localhost/installers:f44-x86_64-anatase-abc12345",
                ],
                "Size": 1024,
            },
            {
                "Id": "old",
                "Names": ["localhost/installers:f44-x86_64-anatase-deadbeef"],
                "Size": 2048,
            },
            {
                "Id": "other",
                "Names": ["localhost/other:f44-x86_64-anatase-deadbeef"],
                "Size": 4096,
            },
        ]

        with patch(
            "ludos.cleanup.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["podman", "images", "--format", "json"],
                0,
                stdout=json.dumps(images),
            ),
        ):
            targets = _stale_local_images(
                "podman",
                "2026.27",
                "",
                ("localhost/installers:anatase",),
            )

        self.assertEqual(
            tuple(target.ref for target in targets),
            (
                "localhost/installers:f44-x86_64-anatase-abc12345",
                "localhost/installers:f44-x86_64-anatase-deadbeef",
            ),
        )


class CleanupPurgeTests(unittest.TestCase):
    def test_purge_collects_all_ludos_managed_images(self) -> None:
        images = [
            {
                "Id": "image1",
                "Names": ["images:anatase", "other:keep"],
                "Size": 1024,
            },
            {
                "Id": "image2",
                "Names": ["flatpaks:browser"],
                "Size": 2048,
            },
            {
                "Id": "image3",
                "Names": ["docker.io/library/fedora:latest"],
                "Size": 4096,
            },
            {
                "Id": "image4",
                "Names": [],
                "Dangling": True,
                "History": ["builds:f44-x86_64-base-oldhash"],
                "Size": 512,
            },
            {
                "Id": "image5",
                "Names": [],
                "Dangling": True,
                "History": ["docker.io/library/fedora:old"],
                "Size": 512,
            },
        ]

        with patch(
            "ludos.cleanup.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["podman", "images", "--format", "json"],
                0,
                stdout=json.dumps(images),
            ),
        ):
            targets = _purge_local_images("podman", "")

        self.assertEqual(
            tuple(target.ref for target in targets),
            (
                "images:anatase",
                "flatpaks:browser",
                "image4",
            ),
        )

    def test_purge_uses_local_prefix(self) -> None:
        images = [
            {
                "Id": "image1",
                "Names": ["images:anatase"],
                "Size": 1024,
            },
            {
                "Id": "image2",
                "Names": ["test-images:anatase"],
                "Size": 1024,
            },
        ]

        with patch(
            "ludos.cleanup.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["podman", "images", "--format", "json"],
                0,
                stdout=json.dumps(images),
            ),
        ):
            targets = _purge_local_images("podman", "test-")

        self.assertEqual(
            tuple(target.ref for target in targets),
            ("test-images:anatase",),
        )

    def test_cleanup_purge_skips_manifest_resolution(self) -> None:
        target = CleanupTarget(
            "localhost/images:anatase",
            "localhost/images:anatase",
            1024,
            "image1",
        )

        with (
            patch(
                "ludos.cleanup.shutil.which",
                side_effect=lambda name: "podman" if name == "podman" else None,
            ),
            patch(
                "ludos.cleanup._purge_local_images",
                return_value=(target,),
            ) as purge_images,
            patch("ludos.cleanup.resolve_manifest_images") as resolve_manifest,
            patch("ludos.cleanup.resolve_manifest_flatpak_images") as resolve_flatpaks,
        ):
            self.assertEqual(
                cleanup_local_images(
                    version="bad/name",
                    manifests=(Path("anatase.yml"),),
                    dry_run=True,
                    purge=True,
                ),
                0,
            )

        purge_images.assert_called_once_with("podman", "")
        resolve_manifest.assert_not_called()
        resolve_flatpaks.assert_not_called()

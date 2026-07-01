from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ludos.cleanup import _keep_named_image, _manifest_cleanup_targets


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

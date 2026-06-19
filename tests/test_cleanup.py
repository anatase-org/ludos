from __future__ import annotations

import unittest

from ludos.cleanup import _keep_named_image


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

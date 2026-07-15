from __future__ import annotations

import unittest
from unittest.mock import ANY, call, patch

from ludos.build import _create_orchestrator_image, _create_scratch_image
from ludos.model import ConfigError


class BuildOrchestratorImageTests(unittest.TestCase):
    def test_quiet_scratch_build_reports_captured_failure_output(self) -> None:
        with patch(
            "ludos.build._run_streamed_command",
            return_value=(1, "transaction failed\ndisk full\n"),
        ) as stream:
            with self.assertRaisesRegex(
                ConfigError,
                "(?s)localhost/builders:test.*transaction failed\\ndisk full",
            ):
                _create_scratch_image(
                    buildah="buildah",
                    image="localhost/builders:test",
                    body=[],
                    quiet=True,
                )

        self.assertTrue(stream.call_args.kwargs["quiet"])

    def test_tags_latest_when_retagging_base_orchestrator(self) -> None:
        source = "quay.io/fedora/fedora:42"
        image = "localhost/orchestrator:fedora-base-20260616"

        with (
            patch("ludos.build._run_streamed_command", return_value=(0, "")) as stream,
            patch("ludos.build.subprocess.run") as run,
        ):
            _create_orchestrator_image(
                podman="podman",
                buildah=None,
                source=source,
                image=image,
                packages=(),
            )

        stream.assert_called_once_with(["podman", "pull", source])
        self.assertEqual(
            run.call_args_list,
            [
                call(["podman", "tag", source, image], check=True),
                call(["podman", "tag", image, "localhost/orchestrator:latest"], check=True),
            ],
        )

    def test_tags_latest_after_building_orchestrator_with_dependencies(self) -> None:
        source = "quay.io/fedora/fedora:42"
        image = "localhost/orchestrator:fedora-12345678-20260616"

        with (
            patch(
                "ludos.build._run_streamed_command",
                side_effect=((0, ""), (0, "")),
            ) as stream,
            patch("ludos.build.subprocess.run") as run,
        ):
            _create_orchestrator_image(
                podman="podman",
                buildah="buildah",
                source=source,
                image=image,
                packages=("rpm-build",),
            )

        self.assertEqual(stream.call_args_list[0], call(["podman", "pull", source]))
        self.assertEqual(
            stream.call_args_list[1],
            call(["buildah", "unshare", "/bin/sh", "-s"], input_text=ANY),
        )
        self.assertIn(
            'buildah commit --rm --quiet --format oci "$container" '
            "localhost/orchestrator:fedora-12345678-20260616 >/dev/null",
            stream.call_args_list[1].kwargs["input_text"],
        )
        self.assertIn(
            'rm -rf "$mount_path/var/cache/dnf" '
            '"$mount_path/var/cache/libdnf5"',
            stream.call_args_list[1].kwargs["input_text"],
        )
        run.assert_called_once_with(
            ["podman", "tag", image, "localhost/orchestrator:latest"],
            check=True,
        )

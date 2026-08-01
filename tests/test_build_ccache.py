from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.__main__ import build_parser
from ludos.build import (
    CCACHE_CONTAINER_DIR,
    CCACHE_PATH_PREFIX,
    CCACHE_SLOPPINESS,
    SCCACHE_CONTAINER_DIR,
    _add_ccache_builder_options,
    _ccache_build_prelude,
    _run_build_output_image_build,
)


class BuildCcacheTests(unittest.TestCase):
    def test_builder_options_mount_compiler_caches_and_pass_maxsize(self) -> None:
        command = ["podman", "run"]

        with patch.dict(
            os.environ,
            {"CCACHE_MAXSIZE": "20G", "SCCACHE_CACHE_SIZE": "30G"},
            clear=True,
        ):
            _add_ccache_builder_options(command, Path("/tmp/cache/ccache"))

        self.assertEqual(
            command,
            [
                "podman",
                "run",
                "--volume",
                f"/tmp/cache/ccache:{CCACHE_CONTAINER_DIR}",
                "--env",
                f"CCACHE_DIR={CCACHE_CONTAINER_DIR}",
                "--env",
                f"SCCACHE_DIR={SCCACHE_CONTAINER_DIR}",
                "--env",
                f"CCACHE_SLOPPINESS={CCACHE_SLOPPINESS}",
                "--env",
                "CCACHE_MAXSIZE=20G",
                "--env",
                "SCCACHE_CACHE_SIZE=30G",
            ],
        )

    def test_builder_options_skip_maxsize_when_unset(self) -> None:
        command = ["podman", "run"]

        with patch.dict(os.environ, {}, clear=True):
            _add_ccache_builder_options(command, Path("/tmp/cache/ccache"))

        self.assertEqual(
            command,
            [
                "podman",
                "run",
                "--volume",
                f"/tmp/cache/ccache:{CCACHE_CONTAINER_DIR}",
                "--env",
                f"CCACHE_DIR={CCACHE_CONTAINER_DIR}",
                "--env",
                f"SCCACHE_DIR={SCCACHE_CONTAINER_DIR}",
                "--env",
                f"CCACHE_SLOPPINESS={CCACHE_SLOPPINESS}",
            ],
        )

    def test_builder_options_respect_sloppiness_override(self) -> None:
        command = ["podman", "run"]

        with patch.dict(os.environ, {"CCACHE_SLOPPINESS": "time_macros"}, clear=True):
            _add_ccache_builder_options(command, Path("/tmp/cache/ccache"))

        self.assertIn("--env", command)
        self.assertIn("CCACHE_SLOPPINESS=time_macros", command)

    def test_ccache_disabled_noops(self) -> None:
        command = ["podman", "run"]

        _add_ccache_builder_options(command, None)

        self.assertEqual(command, ["podman", "run"])
        self.assertEqual(_ccache_build_prelude(None), "")

    def test_ccache_prelude_prepends_wrapper_dirs(self) -> None:
        self.assertEqual(
            _ccache_build_prelude(Path("/tmp/cache/ccache")),
            (
                f"export PATH={CCACHE_PATH_PREFIX}:$PATH\n"
                f"mkdir -p {SCCACHE_CONTAINER_DIR}\n"
                "if command -v sccache >/dev/null 2>&1; then\n"
                "  export RUSTC_WRAPPER=sccache\n"
                "fi\n"
            ),
        )

    def test_build_parser_defaults_to_ccache_enabled(self) -> None:
        parser = build_parser()

        enabled = parser.parse_args(["build", "manifest.yml"])
        disabled = parser.parse_args(["build", "--no-ccache", "manifest.yml"])

        self.assertFalse(enabled.no_ccache)
        self.assertTrue(disabled.no_ccache)

    def test_build_parser_accepts_target_card(self) -> None:
        parser = build_parser()

        default = parser.parse_args(["build", "manifest.yml"])
        targeted = parser.parse_args(
            ["build", "--card", "cards/base/scx", "manifest.yml"]
        )

        self.assertIsNone(default.card)
        self.assertEqual(targeted.card, "cards/base/scx")

    def test_podman_build_output_mounts_artifact_podman_and_compiler_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build_dir = root / "build"
            build_dir.mkdir()
            (build_dir / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
            artifact_cache = root / "artifacts"
            artifact_cache.mkdir()
            podman_cache = artifact_cache / "podman"
            podman_cache.mkdir()
            ccache = root / "ccache"
            ccache.mkdir()

            with (
                patch("ludos.build.os.path.exists", return_value=True),
                patch(
                    "ludos.build._run_streamed_command", return_value=(0, "")
                ) as run,
            ):
                _run_build_output_image_build(
                    podman="podman",
                    build_dir=build_dir,
                    image="localhost/builds:test",
                    artifact_cache_dir=artifact_cache,
                    ccache_dir=ccache,
                    podman_cache_dir=podman_cache,
                    source_dir=root / "card",
                    workspace_dir=build_dir / "workspace",
                    auth_secret="secret-token",
                )

        command = run.call_args.args[0]
        self.assertNotIn("--privileged", command)
        self.assertIn("--cap-add", command)
        self.assertIn("all", command)
        self.assertIn("--security-opt", command)
        self.assertIn("label=disable", command)
        self.assertIn("--device", command)
        self.assertIn("/dev/fuse", command)
        self.assertIn("--layers", command)
        self.assertIn("--pull=false", command)
        self.assertIn(f"{artifact_cache}:/cache/artifacts", command)
        self.assertIn(f"{podman_cache}:/cache/podman", command)
        self.assertIn(f"{ccache}:{CCACHE_CONTAINER_DIR}", command)
        self.assertIn("localhost/builds:test", command)
        self.assertIn("id=auth_secret,env=AUTH_SECRET", command)
        self.assertNotIn("secret-token", command)
        self.assertEqual(run.call_args.kwargs["env"]["AUTH_SECRET"], "secret-token")

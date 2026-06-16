from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.__main__ import build_parser
from ludos.build import (
    CCACHE_CONTAINER_DIR,
    CCACHE_PATH_PREFIX,
    SCCACHE_CONTAINER_DIR,
    _add_ccache_builder_options,
    _ccache_build_prelude,
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
            ],
        )

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

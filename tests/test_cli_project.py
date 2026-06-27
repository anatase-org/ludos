from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ludos.__main__ import main


class CliProjectTests(unittest.TestCase):
    def test_build_from_subdirectory_uses_discovered_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            original_cwd = Path.cwd()
            root = Path(temp)
            cards_dir = root / "cards"
            cards_dir.mkdir()
            (root / "ludos.yml").write_text(
                "version: 1\nname: Anatase\n",
                encoding="utf-8",
            )
            os.chdir(cards_dir)
            try:
                result = SimpleNamespace(
                    output_image="localhost/anatase:latest",
                    image="anatase",
                    distro="fedora",
                    podman="/usr/bin/podman",
                    orchestrator="localhost/orchestrator",
                    package_blocks=(),
                    build_blocks=(),
                )
                seen: dict[str, object] = {}

                def build_manifest(manifest: Path, **_kwargs: object) -> object:
                    seen["cwd"] = Path.cwd()
                    seen["manifest"] = manifest
                    return result

                with (
                    patch.object(
                        sys,
                        "argv",
                        ["ludos", "build", "anatase.yml"],
                    ),
                    patch("ludos.__main__.build_manifest", side_effect=build_manifest),
                    patch("ludos.__main__.configure_logging"),
                    patch("ludos.__main__.log") as log,
                ):
                    exit_code = main()

                self.assertEqual(exit_code, 0)
                self.assertEqual(seen["cwd"], root)
                self.assertEqual(seen["manifest"], Path("anatase.yml"))
                self.assertEqual(Path.cwd(), cards_dir)

                messages = [call.args[0] for call in log.call_args_list]
                self.assertEqual(messages[1], "Starting Ludos...")
                self.assertEqual(messages[2], f"Using project: Anatase at {root}")
            finally:
                os.chdir(original_cwd)

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ludos.__main__ import main
from ludos.model import ConfigError, Project


class CliProjectTests(unittest.TestCase):
    def test_project_parser_accepts_ci_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Anatase",
                        "ci:",
                        "  registry: ghcr.io/anatase-org/",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            project = Project.from_file(config)

        self.assertEqual(project.ci.registry, "ghcr.io/anatase-org")

    def test_project_parser_defaults_to_empty_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text("version: 1\nname: Anatase\n", encoding="utf-8")

            project = Project.from_file(config)

        self.assertEqual(project.ci.registry, "")

    def test_project_parser_rejects_invalid_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text("version: 1\nci: enabled\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "'ci' must be a mapping"):
                Project.from_file(config)

    def test_project_parser_rejects_unknown_ci_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "version: 1\nci:\n  registry: ghcr.io/test\n  surprise: nope\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "ci.surprise"):
                Project.from_file(config)

    def test_project_parser_accepts_flatpak_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Anatase",
                        "flatpaks:",
                        "  images:",
                        "    uri: https://flatpaks.example.test/icons/",
                        "    s3: icons/",
                        "    overlay: ./flatpaks/overlay.png",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            project = Project.from_file(config)

        self.assertEqual(project.name, "Anatase")
        self.assertEqual(
            project.flatpak_images.uri,
            "https://flatpaks.example.test/icons/",
        )
        self.assertEqual(project.flatpak_images.s3, "icons/")
        self.assertEqual(project.flatpak_images.overlay, "./flatpaks/overlay.png")

    def test_project_parser_accepts_flatpak_gpg(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Test",
                        "flatpaks:",
                        "  gpg:",
                        "    identity: https://flatpaks.example.test/",
                        "    lookaside: gpg",
                        "    verify: ./keys/test.pub.asc",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            project = Project.from_file(config)

        self.assertEqual(project.flatpak_gpg.identity, "https://flatpaks.example.test/")
        self.assertEqual(project.flatpak_gpg.lookaside, "gpg")
        self.assertEqual(project.flatpak_gpg.verify, "./keys/test.pub.asc")

    def test_project_parser_rejects_incomplete_flatpak_gpg(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "version: 1\nflatpaks:\n  gpg:\n    lookaside: gpg\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "'identity'"):
                Project.from_file(config)

            config.write_text(
                "version: 1\nflatpaks:\n  gpg:\n    identity: https://example.test/\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "'lookaside'"):
                Project.from_file(config)

    def test_project_parser_rejects_unknown_flatpak_gpg_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "flatpaks:",
                        "  gpg:",
                        "    identity: https://example.test/",
                        "    lookaside: gpg",
                        "    surprise: nope",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "flatpaks.gpg.surprise"):
                Project.from_file(config)

    def test_project_parser_accepts_oci_cosign(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Test",
                        "oci:",
                        "  cosign:",
                        "    registry: https://flatpaks.example.test/",
                        "    identity: cosign.example.test",
                        "    verify: ./keys/root.pem",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            project = Project.from_file(config)

        self.assertEqual(project.oci_cosign.registry, "https://flatpaks.example.test/")
        self.assertEqual(project.oci_cosign.identity, "cosign.example.test")
        self.assertEqual(project.oci_cosign.verify, "./keys/root.pem")

    def test_project_parser_rejects_incomplete_oci_cosign(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "version: 1\noci:\n  cosign:\n    identity: cosign.example.test\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "'registry'"):
                Project.from_file(config)

            config.write_text(
                "version: 1\noci:\n  cosign:\n    registry: https://example.test/\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "'identity'"):
                Project.from_file(config)

    def test_project_parser_rejects_unknown_oci_cosign_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "ludos.yml"
            config.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "oci:",
                        "  cosign:",
                        "    registry: https://example.test/",
                        "    identity: cosign.example.test",
                        "    verify: ./keys/root.pem",
                        "    surprise: nope",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "oci.cosign.surprise"):
                Project.from_file(config)

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

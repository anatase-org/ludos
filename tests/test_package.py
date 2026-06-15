from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from ludos.__main__ import build_parser
from ludos.contrib.package import fork_package


class PackageForkTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "dist-git"
        self._git(["init", str(self.repo)], cwd=self.root)
        self._git(["config", "user.email", "test@example.com"], cwd=self.repo)
        self._git(["config", "user.name", "Test User"], cwd=self.repo)
        self._git(["config", "commit.gpgsign", "false"], cwd=self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_creates_location_card_when_card_is_omitted(self) -> None:
        self._write_repo("pkg.spec", "Name: pkg\nVersion: 1\n")
        self._write_repo("sources", "hash  pkg.tar.xz\n")
        self._commit("package")
        location = self.root / "cards" / "pkg"

        fork_package(self.repo.as_uri(), location)

        self.assertFalse((location / ".git").exists())
        self.assertEqual((location / "pkg.spec").read_text(encoding="utf-8"), "Name: pkg\nVersion: 1\n")
        card = self._load_yaml(location / "card.yml")
        card_text = (location / "card.yml").read_text(encoding="utf-8")
        self.assertEqual(card["version"], 1)
        self.assertEqual(len(card["specs"]), 1)
        self.assertIn("specs:\n  - spec: pkg.spec\n", card_text)
        self.assertIn("files:\n      - pkg.spec\n      - sources\n", card_text)
        self.assertIn("packages:\n      - pkg\n", card_text)
        self.assertEqual(
            card["specs"][0],
            {
                "spec": "pkg.spec",
                "files": ["pkg.spec", "sources"],
                "packages": ["pkg"],
                "upstream": {
                    "type": "dist-git",
                    "url": self.repo.as_uri(),
                    "branch": "rawhide",
                },
            },
        )

    def test_appends_to_existing_external_card(self) -> None:
        self._write_repo("pkg.spec", "Name: pkg\nVersion: 1\n")
        self._commit("package")
        card_path = self.root / "cards" / "gaming.yml"
        card_path.parent.mkdir(parents=True)
        card_path.write_text(
            "version: 1\npackages:\n- existing\nspecs:\n- spec: existing/existing.spec\n",
            encoding="utf-8",
        )
        location = self.root / "cards" / "gaming" / "pkg"

        fork_package(self.repo.as_uri(), location, card=card_path)

        card = self._load_yaml(card_path)
        self.assertEqual(card["packages"], ["existing"])
        self.assertEqual(card["specs"][0], {"spec": "existing/existing.spec"})
        self.assertEqual(card["specs"][1]["spec"], "gaming/pkg/pkg.spec")
        self.assertEqual(card["specs"][1]["packages"], ["pkg"])
        self.assertIn("upstream", card["specs"][1])
        self.assertNotIn("patch", card["specs"][1])

    def test_adds_multiple_specs_in_stable_order_with_only_first_upstream(self) -> None:
        self._write_repo("zeta/zeta.spec", "Name: zeta\nVersion: 1\n")
        self._write_repo("alpha/alpha.spec", "Name: alpha\nVersion: 1\n")
        self._write_repo("alpha/source.txt", "source\n")
        self._commit("packages")
        location = self.root / "cards" / "packages"

        fork_package(self.repo.as_uri(), location)

        card = self._load_yaml(location / "card.yml")
        self.assertEqual(
            [spec["spec"] for spec in card["specs"]],
            ["alpha/alpha.spec", "zeta/zeta.spec"],
        )
        self.assertEqual(card["specs"][0]["files"], ["alpha.spec", "source.txt"])
        self.assertEqual(card["specs"][0]["packages"], ["alpha"])
        self.assertEqual(card["specs"][1]["packages"], ["zeta"])
        self.assertIn("upstream", card["specs"][0])
        self.assertNotIn("upstream", card["specs"][1])
        self.assertNotIn("patch", card["specs"][0])
        self.assertNotIn("patch", card["specs"][1])

    def test_duplicate_spec_entry_fails_without_copying(self) -> None:
        self._write_repo("pkg.spec", "Name: pkg\nVersion: 1\n")
        self._commit("package")
        location = self.root / "cards" / "pkg"
        card_path = self.root / "cards" / "card.yml"
        card_path.parent.mkdir(parents=True)
        card_path.write_text(
            "version: 1\nspecs:\n- spec: pkg/pkg.spec\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "duplicate spec entries: pkg/pkg.spec"):
            fork_package(self.repo.as_uri(), location, card=card_path)

        self.assertFalse((location / "pkg.spec").exists())

    def test_non_empty_destination_fails(self) -> None:
        self._write_repo("pkg.spec", "Name: pkg\nVersion: 1\n")
        self._commit("package")
        location = self.root / "cards" / "pkg"
        location.mkdir(parents=True)
        (location / "local.txt").write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "exists but is not empty"):
            fork_package(self.repo.as_uri(), location)

    def test_package_fork_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "package",
                "fork",
                "https://src.fedoraproject.org/rpms/pkg",
                "./cards/pkg",
                "--card",
                "./cards/gaming.yml",
            ]
        )

        self.assertEqual(args.package_action, "fork")
        self.assertEqual(args.git_url, "https://src.fedoraproject.org/rpms/pkg")
        self.assertEqual(args.location, Path("./cards/pkg"))
        self.assertEqual(args.card, Path("./cards/gaming.yml"))

    def _write_repo(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit(self, message: str) -> None:
        self._git(["add", "."], cwd=self.repo)
        self._git(["commit", "-m", message], cwd=self.repo)

    def _git(self, args: list[str], *, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def _load_yaml(self, path: Path) -> dict:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        return data

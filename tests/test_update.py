from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.model import Card, SpecBuild, UpstreamRef
from ludos.contrib.update import (
    LUDOS_BRANCH,
    UpstreamSource,
    _confirm_update,
    _merge_dist_git_update,
    _target_cards,
    _upstream_sources,
)


class UpdateConfirmationTests(unittest.TestCase):
    def test_assume_yes_skips_prompt(self) -> None:
        with patch("ludos.contrib.update.confirm") as confirm:
            self.assertTrue(_confirm_update("card:pkg", assume_yes=True))

        confirm.assert_not_called()

    def test_confirm_update_delegates_to_logging_confirm(self) -> None:
        with patch("ludos.contrib.update.confirm", return_value=True) as confirm:
            self.assertTrue(_confirm_update("card:pkg", assume_yes=False))

        confirm.assert_called_once_with("Update card:pkg")


class UpstreamSourceTests(unittest.TestCase):
    def test_upstream_ref_expands_manifest_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "kde"
            root.mkdir()
            (root / "plasma-setup.spec").write_text(
                "Name: plasma-setup\n",
                encoding="utf-8",
            )
            card_path = root / "card.yml"
            card_path.write_text(
                """
version: 1
specs:
  - spec: plasma-setup.spec
    upstream:
      type: dist-git
      url: https://example.test/plasma-setup
      branch: f$releasever
""".lstrip(),
                encoding="utf-8",
            )

            sources = _upstream_sources(
                Card.from_file(card_path),
                env={"releasever": "44"},
            )

        self.assertEqual(sources[0].upstream.branch, "f44")

    def test_single_root_spec_uses_card_directory_name_for_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "scx"
            root.mkdir()
            (root / "scx-tools.spec").write_text("Name: scx-tools\n", encoding="utf-8")
            card_path = root / "card.yml"
            card_path.write_text(
                """
version: 1
specs:
  - spec: scx-tools.spec
    upstream:
      type: dist-git
      url: https://example.test/scx
""".lstrip(),
                encoding="utf-8",
            )

            sources = _upstream_sources(Card.from_file(card_path))

        self.assertEqual([source.key for source in sources], ["scx"])

    def test_duplicate_root_spec_sources_use_spec_names_for_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "scx"
            root.mkdir()
            (root / "scx-tools.spec").write_text("Name: scx-tools\n", encoding="utf-8")
            (root / "scx-scheds.spec").write_text("Name: scx-scheds\n", encoding="utf-8")
            card_path = root / "card.yml"
            card_path.write_text(
                """
version: 1
specs:
  - spec: scx-tools.spec
    upstream:
      type: dist-git
      url: https://example.test/scx
      subdir: sources/scx-tools
  - spec: scx-scheds.spec
    upstream:
      type: dist-git
      url: https://example.test/scx
      subdir: sources/scx-scheds
""".lstrip(),
                encoding="utf-8",
            )

            sources = _upstream_sources(Card.from_file(card_path))

        self.assertEqual(
            [source.key for source in sources],
            ["scx-tools", "scx-scheds"],
        )


class ManifestUpdateTargetTests(unittest.TestCase):
    def test_targeted_manifest_card_carries_releasever_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "anatase.yml"
            bootstrap = root / "cards" / "bootstrap.yml"
            kde = root / "cards" / "de" / "kde"
            setup = kde / "setup"
            bootstrap.parent.mkdir(parents=True)
            setup.mkdir(parents=True)
            bootstrap.write_text("version: 1\n", encoding="utf-8")
            (setup / "plasma-setup.spec").write_text(
                "Name: plasma-setup\n",
                encoding="utf-8",
            )
            (kde / "card.yml").write_text(
                """
version: 1
specs:
  - spec: setup/plasma-setup.spec
    upstream:
      type: dist-git
      url: https://example.test/plasma-setup
      branch: f$releasever
""".lstrip(),
                encoding="utf-8",
            )
            manifest_path.write_text(
                """
version: 1
env:
  releasever: 44
releasever: $releasever
distro: f$releasever-$arch
orchestrator: quay.io/fedora/fedora:$releasever
bootstrap: cards/bootstrap.yml
cards:
  - cards/de/kde
""".lstrip(),
                encoding="utf-8",
            )

            targets = _target_cards((manifest_path,), card="cards/de/kde")
            sources = _upstream_sources(targets[0].card, env=targets[0].env)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].env["releasever"], "44")
        self.assertEqual(sources[0].upstream.branch, "f44")


class DistGitUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "dist-git"
        self.card_dir = self.root / "card" / "pkg"
        self.card_dir.mkdir(parents=True)
        self._git(["init", str(self.repo)], cwd=self.root)
        self._git(["config", "user.email", "test@example.com"], cwd=self.repo)
        self._git(["config", "user.name", "Test User"], cwd=self.repo)
        self._git(["config", "commit.gpgsign", "false"], cwd=self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_conflicted_dist_git_merge_treats_local_changes_as_incoming(self) -> None:
        self._write_repo("pkg.spec", "Name: pkg\nVersion: 1\n")
        self._commit("initial")
        old_sha = self._rev_parse("HEAD")

        self._write_repo("pkg.spec", "Name: pkg\nVersion: 2-upstream\n")
        self._commit("upstream update")
        new_sha = self._rev_parse("HEAD")

        (self.card_dir / "pkg.spec").write_text(
            "Name: pkg\nVersion: 2-local\n",
            encoding="utf-8",
        )
        source = UpstreamSource(
            key="pkg",
            source_dir=self.card_dir,
            spec=SpecBuild(spec="pkg.spec", files=("pkg.spec",)),
            upstream=UpstreamRef(type="dist-git", url=self.repo.as_uri()),
        )

        conflicts = _merge_dist_git_update(
            repo_dir=self.repo,
            source=source,
            old_sha=old_sha,
            new_sha=new_sha,
        )

        self.assertEqual(conflicts, ("pkg.spec",))
        self.assertEqual(self._current_branch(), LUDOS_BRANCH)
        self.assertEqual(self._rev_parse("HEAD"), new_sha)
        conflict_text = (self.repo / "pkg.spec").read_text(encoding="utf-8")
        self.assertIn(
            "<<<<<<< HEAD\nVersion: 2-upstream\n"
            "=======\nVersion: 2-local\n>>>>>>>",
            conflict_text,
        )

    def test_dist_git_merge_uses_upstream_subdir(self) -> None:
        self._write_repo("sources/scx-tools/scx.spec", "Name: scx\nVersion: 1\n")
        self._write_repo("outside.spec", "Name: outside\nVersion: 1\n")
        self._commit("initial")
        old_sha = self._rev_parse("HEAD")

        self._write_repo(
            "sources/scx-tools/scx.spec",
            "Name: scx\nVersion: 2-upstream\n",
        )
        self._commit("upstream update")
        new_sha = self._rev_parse("HEAD")

        (self.card_dir / "scx.spec").write_text(
            "Name: scx\nVersion: 2-local\n",
            encoding="utf-8",
        )
        source = UpstreamSource(
            key="scx",
            source_dir=self.card_dir,
            spec=SpecBuild(spec="scx.spec", files=("scx.spec",)),
            upstream=UpstreamRef(
                type="dist-git",
                url=self.repo.as_uri(),
                subdir="sources/scx-tools",
            ),
        )

        conflicts = _merge_dist_git_update(
            repo_dir=self.repo,
            source=source,
            old_sha=old_sha,
            new_sha=new_sha,
        )

        self.assertEqual(conflicts, ("scx.spec",))
        self.assertFalse((self.repo / "scx.spec").exists())
        conflict_text = (self.repo / "sources" / "scx-tools" / "scx.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "<<<<<<< HEAD\nVersion: 2-upstream\n"
            "=======\nVersion: 2-local\n>>>>>>>",
            conflict_text,
        )
        self.assertEqual(
            (self.repo / "outside.spec").read_text(encoding="utf-8"),
            "Name: outside\nVersion: 1\n",
        )

    def _write_repo(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit(self, message: str) -> None:
        self._git(["add", "."], cwd=self.repo)
        self._git(["commit", "-m", message], cwd=self.repo)

    def _current_branch(self) -> str:
        return self._git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=self.repo,
            capture=True,
        ).stdout.strip()

    def _rev_parse(self, rev: str) -> str:
        return self._git(["rev-parse", rev], cwd=self.repo, capture=True).stdout.strip()

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=check,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        )

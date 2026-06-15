from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ludos.model import SpecBuild, UpstreamRef
from ludos.contrib.update import LUDOS_BRANCH, UpstreamSource, _merge_dist_git_update


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

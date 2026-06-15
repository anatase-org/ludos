from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from ludos.__main__ import build_parser
from ludos.contrib.patchwork import apply_patch, checkout_patch, init_patch


class GitPatchworkTestCase(unittest.TestCase):
    def _write_upstream(self, relative: str, text: str) -> None:
        path = self.upstream / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit(self, message: str) -> None:
        self._git(["add", "."], cwd=self.upstream)
        self._git(["commit", "-m", message], cwd=self.upstream)

    def _current_branch(self, repo: Path) -> str:
        return self._git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repo,
            capture=True,
        ).stdout.strip()

    def _is_ancestor(self, repo: Path, ancestor: str, descendant: str) -> bool:
        return (
            self._git(
                ["merge-base", "--is-ancestor", ancestor, descendant],
                cwd=repo,
                check=False,
            ).returncode
            == 0
        )

    def _rev_parse(self, repo: Path, rev: str) -> str:
        return self._git(["rev-parse", rev], cwd=repo, capture=True).stdout.strip()

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

    def _load_yaml(self, path: Path) -> dict:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        return data


class PatchworkCommandTests(GitPatchworkTestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.upstream = self.root / "upstream"
        self.card_dir = self.root / "card"
        self.spec_dir = self.card_dir / "pkg"
        self.patchwork_dir = self.root / "patchwork"
        self.spec_dir.mkdir(parents=True)
        self._git(["init", str(self.upstream)], cwd=self.root)
        self._git(["config", "user.email", "test@example.com"], cwd=self.upstream)
        self._git(["config", "user.name", "Test User"], cwd=self.upstream)
        self._git(["config", "commit.gpgsign", "false"], cwd=self.upstream)

        self._write_upstream("hello.txt", "hello\n")
        self._commit("initial")
        self.base_sha = self._rev_parse(self.upstream, "HEAD")
        self._write_upstream("hello.txt", "hello\npatched\n")
        self._commit("patch hello")
        self.patch_sha = self._rev_parse(self.upstream, "HEAD")
        patch_text = self._git(
            [
                "format-patch",
                "--stdout",
                "--zero-commit",
                "--no-renames",
                "-k",
                self.base_sha,
            ],
            cwd=self.upstream,
            capture=True,
        ).stdout
        self._git(["reset", "--hard", self.base_sha], cwd=self.upstream)

        (self.spec_dir / "test.spec").write_text("Name: test\nVersion: 1\n", encoding="utf-8")
        (self.spec_dir / "overrides.patch").write_text(patch_text, encoding="utf-8")
        self.card_path = self.card_dir / "card.yml"
        self.card_path.write_text(
            "\n".join(
                (
                    "version: 1",
                    "specs:",
                    "  - spec: pkg/test.spec",
                    "    packages:",
                    "      - test",
                    "    patch:",
                    "      type: git",
                    f"      url: {self.upstream.as_uri()}",
                    "      ref: HEAD",
                    "      file: overrides.patch",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (self.card_dir / "card.lock.yml").write_text(
            f"pkg:\n  patch-sha: {self.base_sha}\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_checkout_recreates_ludos_branch_and_apply_updates_patch_file(self) -> None:
        checkout_patch(f"{self.card_path}:pkg", patchwork_dir=self.patchwork_dir)
        repo = self.patchwork_dir / "pkg"

        self.assertEqual(self._current_branch(repo), "ludos")
        self.assertTrue(self._is_ancestor(repo, self.base_sha, "HEAD"))
        self.assertEqual((repo / "hello.txt").read_text(encoding="utf-8"), "hello\npatched\n")

        self._git(["config", "user.email", "test@example.com"], cwd=repo)
        self._git(["config", "user.name", "Test User"], cwd=repo)
        self._git(["config", "commit.gpgsign", "false"], cwd=repo)
        (repo / "hello.txt").write_text("hello\npatched\nagain\n", encoding="utf-8")
        self._git(["add", "hello.txt"], cwd=repo)
        self._git(["commit", "-m", "patch again"], cwd=repo)

        apply_patch(f"{self.card_path}:pkg", patchwork_dir=self.patchwork_dir)

        patch_text = (self.spec_dir / "overrides.patch").read_text(encoding="utf-8")
        self.assertIn("+again", patch_text)

    def test_checkout_and_apply_use_patch_name_as_repo_key(self) -> None:
        self.card_path.write_text(
            "\n".join(
                (
                    "version: 1",
                    "specs:",
                    "  - spec: pkg/test.spec",
                    "    packages:",
                    "      - test",
                    "    patch:",
                    "      type: git",
                    f"      url: {self.upstream.as_uri()}",
                    "      ref: HEAD",
                    "      file: overrides.patch",
                    "      name: named-repo",
                    "",
                )
            ),
            encoding="utf-8",
        )
        (self.card_dir / "card.lock.yml").write_text(
            f"named-repo:\n  patch-sha: {self.base_sha}\n",
            encoding="utf-8",
        )

        checkout_patch(f"{self.card_path}:named-repo", patchwork_dir=self.patchwork_dir)
        repo = self.patchwork_dir / "named-repo"
        self.assertTrue(repo.is_dir())
        self.assertEqual(self._current_branch(repo), "ludos")

        self._git(["config", "user.email", "test@example.com"], cwd=repo)
        self._git(["config", "user.name", "Test User"], cwd=repo)
        self._git(["config", "commit.gpgsign", "false"], cwd=repo)
        (repo / "hello.txt").write_text("hello\npatched\nnamed\n", encoding="utf-8")
        self._git(["add", "hello.txt"], cwd=repo)
        self._git(["commit", "-m", "patch named"], cwd=repo)

        apply_patch(f"{self.card_path}:named-repo", patchwork_dir=self.patchwork_dir)

        patch_text = (self.spec_dir / "overrides.patch").read_text(encoding="utf-8")
        self.assertIn("+named", patch_text)


class PatchInitCommandTests(GitPatchworkTestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.upstream = self.root / "upstream"
        self.card_dir = self.root / "card"
        self.spec_dir = self.card_dir / "pkg"
        self.patchwork_dir = self.root / "patchwork"
        self.spec_dir.mkdir(parents=True)
        self._git(["init", str(self.upstream)], cwd=self.root)
        self._git(["config", "user.email", "test@example.com"], cwd=self.upstream)
        self._git(["config", "user.name", "Test User"], cwd=self.upstream)
        self._git(["config", "commit.gpgsign", "false"], cwd=self.upstream)
        self._write_upstream("hello.txt", "hello\n")
        self._commit("initial")
        self.base_sha = self._rev_parse(self.upstream, "HEAD")
        self._git(["tag", "v1"], cwd=self.upstream)
        self._git(["tag", "1"], cwd=self.upstream)

        (self.spec_dir / "test.spec").write_text(
            "Name: test\nVersion: 1\n",
            encoding="utf-8",
        )
        self.card_path = self.card_dir / "card.yml"
        self.card_path.write_text(
            "\n".join(
                (
                    "version: 1",
                    "specs:",
                    "  - spec: pkg/test.spec",
                    "    packages:",
                    "      - test",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_init_adds_patch_field_empty_file_lock_and_checkout(self) -> None:
        init_patch(
            f"{self.card_path}:test.spec",
            self.upstream.as_uri(),
            patchwork_dir=self.patchwork_dir,
        )

        card = self._load_yaml(self.card_path)
        patch = card["specs"][0]["patch"]
        self.assertEqual(
            patch,
            {
                "type": "git",
                "url": self.upstream.as_uri(),
                "ref": "${spec:Version}",
                "file": "overrides.patch",
            },
        )
        self.assertEqual(
            (self.spec_dir / "overrides.patch").read_text(encoding="utf-8"),
            "",
        )
        lock = self._load_yaml(self.card_dir / "card.lock.yml")
        self.assertEqual(lock["pkg"]["patch-sha"], self.base_sha)
        repo = self.patchwork_dir / "pkg"
        self.assertEqual(self._current_branch(repo), "ludos")
        self.assertEqual(self._rev_parse(repo, "HEAD"), self.base_sha)

    def test_init_supports_custom_file_and_ref(self) -> None:
        init_patch(
            f"{self.card_path}:pkg",
            self.upstream.as_uri(),
            patchwork_dir=self.patchwork_dir,
            file="custom.patch",
            ref="v${spec:Version}",
            name="xorg-server",
        )

        card = self._load_yaml(self.card_path)
        self.assertEqual(card["specs"][0]["patch"]["file"], "custom.patch")
        self.assertEqual(card["specs"][0]["patch"]["ref"], "v${spec:Version}")
        self.assertEqual(card["specs"][0]["patch"]["name"], "xorg-server")
        self.assertTrue((self.spec_dir / "custom.patch").is_file())
        lock = self._load_yaml(self.card_dir / "card.lock.yml")
        self.assertEqual(lock["xorg-server"]["patch-sha"], self.base_sha)
        self.assertTrue((self.patchwork_dir / "xorg-server").is_dir())

    def test_init_refuses_existing_patch_field(self) -> None:
        init_patch(
            f"{self.card_path}:test",
            self.upstream.as_uri(),
            patchwork_dir=self.patchwork_dir,
        )

        with self.assertRaisesRegex(ValueError, "already has patch"):
            init_patch(
                f"{self.card_path}:test",
                self.upstream.as_uri(),
                patchwork_dir=self.patchwork_dir,
            )

    def test_patch_init_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "patch",
                "init",
                "cards/gaming/gamemode.yml:xorg-x11-server-Xwayland.spec",
                "https://gitlab.freedesktop.org/xorg/xserver",
                "--ref",
                "xwayland-${spec:Version}",
                "--file",
                "xwayland.patch",
                "--name",
                "xorg-server",
            ]
        )

        self.assertEqual(args.patch_action, "init")
        self.assertEqual(
            args.target,
            "cards/gaming/gamemode.yml:xorg-x11-server-Xwayland.spec",
        )
        self.assertEqual(args.url, "https://gitlab.freedesktop.org/xorg/xserver")
        self.assertEqual(args.ref, "xwayland-${spec:Version}")
        self.assertEqual(args.file, "xwayland.patch")
        self.assertEqual(args.name, "xorg-server")

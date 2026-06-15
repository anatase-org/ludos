from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.build import _copy_git_file_source, _copy_http_file_source
from ludos.model import ConfigError


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class FileSourceCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_http_source_cache_only_reuses_cached_file(self) -> None:
        cache_path = self.root / "cache" / "file"
        target = self.root / "target"

        with patch(
            "ludos.build.urllib.request.urlopen",
            return_value=_Response(b"cached contents"),
        ) as urlopen:
            _copy_http_file_source(
                "https://example.com/file",
                target,
                cache_path,
                cache_only=False,
            )

        self.assertEqual(target.read_bytes(), b"cached contents")
        self.assertEqual(cache_path.read_bytes(), b"cached contents")
        urlopen.assert_called_once_with("https://example.com/file")

        target.unlink()
        with patch("ludos.build.urllib.request.urlopen") as urlopen:
            _copy_http_file_source(
                "https://example.com/file",
                target,
                cache_path,
                cache_only=True,
            )

        self.assertEqual(target.read_bytes(), b"cached contents")
        urlopen.assert_not_called()

    def test_http_source_cache_only_requires_cached_file(self) -> None:
        with self.assertRaisesRegex(ConfigError, "file source is not cached"):
            _copy_http_file_source(
                "https://example.com/file",
                self.root / "target",
                self.root / "cache" / "file",
                cache_only=True,
            )

    def test_git_source_cache_only_reuses_cached_fetch(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")

        repo = self.root / "source"
        self._git(["init", str(repo)], cwd=self.root)
        self._git(["config", "user.email", "test@example.com"], cwd=repo)
        self._git(["config", "user.name", "Test User"], cwd=repo)
        self._git(["config", "commit.gpgsign", "false"], cwd=repo)
        (repo / "file.txt").write_text("cached\n", encoding="utf-8")
        self._git(["add", "file.txt"], cwd=repo)
        self._git(["commit", "-m", "initial"], cwd=repo)

        source = f"git+{repo.as_uri()}"
        cache_dir = self.root / "cache" / "git-source"
        first_target = self.root / "first"
        second_target = self.root / "second"

        _copy_git_file_source(source, first_target, cache_dir, cache_only=False)
        self.assertEqual((first_target / "file.txt").read_text(encoding="utf-8"), "cached\n")

        (repo / "file.txt").write_text("remote update\n", encoding="utf-8")
        self._git(["commit", "-am", "update"], cwd=repo)

        _copy_git_file_source(source, second_target, cache_dir, cache_only=True)

        self.assertEqual((second_target / "file.txt").read_text(encoding="utf-8"), "cached\n")

    def test_git_source_cache_only_requires_cached_repo(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")

        with self.assertRaisesRegex(ConfigError, "git file source is not cached"):
            _copy_git_file_source(
                "git+file:///missing",
                self.root / "target",
                self.root / "cache" / "git-source",
                cache_only=True,
            )

    def _git(self, args: list[str], *, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

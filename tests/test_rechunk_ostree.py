from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.rechunk.ostree import _count_repo_file_objects, _ostree_ls_command


class RechunkOstreeTests(unittest.TestCase):
    def test_count_repo_file_objects_counts_file_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            objects = repo / "objects" / "00"
            objects.mkdir(parents=True)
            (objects / "a.file").write_text("", encoding="utf-8")
            (objects / "b.file").write_text("", encoding="utf-8")
            (objects / "c.dirtree").write_text("", encoding="utf-8")

            self.assertEqual(_count_repo_file_objects(repo), 2)

    def test_ostree_ls_command_uses_host_ostree_without_sudo(self) -> None:
        self.assertEqual(
            _ostree_ls_command("/repo", "master"),
            [
                "ostree",
                "ls",
                "-C",
                "-R",
                "--repo",
                "/repo",
                "master",
            ],
        )

    def test_ostree_ls_command_uses_image_when_supplied(self) -> None:
        with patch("ludos.rechunk.ostree.Path.resolve", return_value=Path("/abs/repo")):
            self.assertEqual(
                _ostree_ls_command(
                    "/repo",
                    "master",
                    ostree_image="localhost/anatase:f44",
                    podman="/usr/bin/podman",
                ),
                [
                    "/usr/bin/podman",
                    "run",
                    "--rm",
                    "--volume",
                    "/abs/repo:/ludos/ostree:ro",
                    "localhost/anatase:f44",
                    "ostree",
                    "ls",
                    "-C",
                    "-R",
                    "--repo",
                    "/ludos/ostree",
                    "master",
                ],
            )


if __name__ == "__main__":
    unittest.main()

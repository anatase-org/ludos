from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from ludos.__main__ import build_parser
from ludos.bootc import (
    _parse_commit,
    _parse_progress_total,
    _count_repo_objects,
    _read_ostree_stderr,
    _update_object_progress,
    _short_digest,
    ostree_import,
)


class BootcCommandTests(unittest.TestCase):
    def test_parser_accepts_ostree_import(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "ostree-import",
                "--cache-dir",
                "custom-cache",
                "--orchestrator",
                "localhost/orchestrator:latest",
                "--ostree-ref",
                "anatase",
                "localhost/anatase:latest",
            ]
        )

        self.assertEqual(args.command, "bootc")
        self.assertEqual(args.bootc_action, "ostree-import")
        self.assertEqual(args.cache_dir, Path("custom-cache"))
        self.assertEqual(args.orchestrator, "localhost/orchestrator:latest")
        self.assertEqual(args.ostree_ref, "anatase")
        self.assertEqual(args.ref, "localhost/anatase:latest")

    def test_ostree_import_mounts_image_and_cache_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache-root"
            ostree_dir = cache_dir / "ostree"
            image_exists = subprocess.CompletedProcess(
                ["podman", "image", "exists"], 0
            )
            inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout=(
                    '{"RepoTags":["localhost/orchestrator:latest"],'
                    '"RepoDigests":["localhost/orchestrator@sha256:'
                    'abcdef1234567890"]}\n'
                ),
            )

            with (
                patch("ludos.bootc.shutil.which", return_value="podman"),
                patch("ludos.bootc.subprocess.run") as run,
                patch("ludos.bootc.log") as log,
                patch(
                    "ludos.bootc._run_ostree_import_command",
                    return_value=(
                        0,
                        "abcdef1234567890abcdef1234567890"
                        "abcdef1234567890abcdef1234567890\n",
                    ),
                ) as run_import,
            ):
                run.side_effect = [image_exists, image_exists, inspect]

                result = ostree_import(
                    "localhost/anatase:latest",
                    cache_dir=cache_dir,
                    orchestrator="orchestrator",
                    ostree_ref="anatase",
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    ["podman", "image", "exists", "localhost/anatase:latest"],
                    check=False,
                ),
                call(["podman", "image", "exists", "orchestrator"], check=False),
                call(
                    [
                        "podman",
                        "image",
                        "inspect",
                        "orchestrator",
                        "--format",
                        "{{json .}}",
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ),
            ],
        )
        log.assert_any_call(
            "Using orchestrator image: orchestrator (localhost/orchestrator:latest)"
        )
        run_import.assert_called_once()
        command, repo = run_import.call_args.args
        self.assertEqual(repo, ostree_dir.resolve())
        self.assertEqual(command[:3], ["podman", "run", "--rm"])
        self.assertIn(
            "type=image,source=localhost/anatase:latest,target=/ludos/source",
            command,
        )
        self.assertIn(
            f"type=bind,source={ostree_dir.resolve()},target=/ludos/ostree",
            command,
        )
        self.assertIn("LUDOS_OSTREE_REF=anatase", command)
        self.assertIn("orchestrator", command)
        script = command[-1]
        self.assertIn('ostree --repo="$repo" init --mode=bare-user', script)
        self.assertIn(
            'commit=$(env -u G_MESSAGES_DEBUG ostree --repo="$repo" commit -v \\',
            script,
        )
        self.assertIn('__LUDOS_OSTREE_APPROX_TOTAL__', script)
        self.assertIn('printf "%s\\n" "$commit"', script)
        self.assertIn('--tree=dir="$source"', script)

    def test_short_digest_handles_repo_digest_and_image_id(self) -> None:
        self.assertEqual(
            _short_digest("localhost/orchestrator@sha256:abcdef1234567890"),
            "sha256:abcdef123456",
        )
        self.assertEqual(
            _short_digest("abcdef1234567890"),
            "abcdef123456",
        )

    def test_parse_commit_returns_last_checksum(self) -> None:
        first = "0" * 64
        second = "1" * 64
        self.assertEqual(_parse_commit(f"{first}\n{second}\n"), second)

    def test_parse_progress_total(self) -> None:
        self.assertEqual(
            _parse_progress_total("__LUDOS_OSTREE_APPROX_TOTAL__ 42"),
            42,
        )
        self.assertIsNone(_parse_progress_total("__LUDOS_OSTREE_APPROX_TOTAL__ nope"))

    def test_read_ostree_stderr_updates_progress_total_and_streams_output(self) -> None:
        class Process:
            stderr = io.StringIO(
                "__LUDOS_OSTREE_APPROX_TOTAL__ 42\n"
                "OT: using fuse: 0\n"
                "OT: Preparing transaction\n"
            )

        class Progress:
            total: int | None = None

            def __init__(self) -> None:
                self.refresh_count = 0

            def refresh(self) -> None:
                self.refresh_count += 1

        progress = Progress()

        with patch("ludos.bootc.pstream") as pstream:
            _read_ostree_stderr(Process(), progress)  # type: ignore[arg-type]

        self.assertEqual(progress.total, 42)
        self.assertEqual(progress.refresh_count, 1)
        self.assertEqual(
            pstream.call_args_list,
            [
                call("OT: using fuse: 0"),
                call("OT: Preparing transaction"),
            ],
        )

    def test_count_repo_objects_ignores_tmp_and_unrelated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ostree_dir = Path(tmp) / "ostree"
            objects = ostree_dir / "objects"
            (objects / "00").mkdir(parents=True)
            (objects / "tmp").mkdir()
            (objects / "00" / "a.file").write_text("", encoding="utf-8")
            (objects / "00" / "b.dirtree").write_text("", encoding="utf-8")
            (objects / "00" / "c.dirmeta").write_text("", encoding="utf-8")
            (objects / "00" / "d.commit").write_text("", encoding="utf-8")
            (objects / "00" / "e.detachedmeta").write_text("", encoding="utf-8")
            (objects / "tmp" / "f.file").write_text("", encoding="utf-8")

            self.assertEqual(_count_repo_objects(ostree_dir), 4)

    def test_update_object_progress_only_moves_forward(self) -> None:
        class Progress:
            def __init__(self) -> None:
                self.n = 3
                self.updates: list[int] = []

            def update(self, amount: int) -> None:
                self.updates.append(amount)
                self.n += amount

        with tempfile.TemporaryDirectory() as tmp:
            ostree_dir = Path(tmp) / "ostree"
            objects = ostree_dir / "objects" / "00"
            objects.mkdir(parents=True)
            for index in range(5):
                (objects / f"{index}.file").write_text("", encoding="utf-8")

            progress = Progress()
            _update_object_progress(ostree_dir, baseline=1, progress=progress)
            _update_object_progress(ostree_dir, baseline=3, progress=progress)

        self.assertEqual(progress.updates, [1])
        self.assertEqual(progress.n, 4)


if __name__ == "__main__":
    unittest.main()

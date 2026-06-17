from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import call, patch

from ludos.__main__ import build_parser
from ludos.bootc import _short_digest, ostree_import


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
                    "ludos.bootc._run_streamed_command",
                    return_value=(0, "Imported OSTree commit: abc\n"),
                ) as streamed,
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
        streamed.assert_called_once()
        command = streamed.call_args.args[0]
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
        self.assertIn('ostree --repo="$repo" commit \\', script)
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


if __name__ == "__main__":
    unittest.main()

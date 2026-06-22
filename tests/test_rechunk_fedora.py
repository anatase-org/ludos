from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ludos.rechunk.fedora import CONTAINER_RPMDB_DIR, get_packages


class RechunkFedoraTests(unittest.TestCase):
    def test_get_packages_runs_host_rpm_by_default(self) -> None:
        with patch(
            "ludos.rechunk.fedora.subprocess.run",
            return_value=SimpleNamespace(stdout=b""),
        ) as run:
            self.assertEqual(get_packages("/rpmdb"), [])

        command = run.call_args.args[0]
        self.assertEqual(command[0], "rpm")
        self.assertIn("--dbpath", command)
        self.assertEqual(command[command.index("--dbpath") + 1], "/rpmdb")
        self.assertEqual(run.call_args.kwargs["check"], True)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)

    def test_get_packages_runs_rpm_in_image_when_supplied(self) -> None:
        with (
            patch("ludos.rechunk.fedora.Path.resolve", return_value=Path("/abs/rpmdb")),
            patch(
                "ludos.rechunk.fedora.subprocess.run",
                return_value=SimpleNamespace(stdout=b""),
            ) as run,
        ):
            self.assertEqual(
                get_packages(
                    "/rpmdb",
                    rpm_image="localhost/anatase:f44",
                    podman="/usr/bin/podman",
                ),
                [],
            )

        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "/usr/bin/podman",
                "run",
                "--rm",
                "--volume",
                f"/abs/rpmdb:{CONTAINER_RPMDB_DIR}:rw",
                "localhost/anatase:f44",
                "rpm",
                "-qa",
                "--queryformat",
                "M2Dqm7H6\n[%{FILESIZES} %{FILENAMES}\n]"
                "7mhjAuF8%{NAME} %{NEVRA} %{VERSION} %{RELEASE} %{SIZE}\n",
                "--changes",
                "--dbpath",
                CONTAINER_RPMDB_DIR,
            ],
        )
        self.assertEqual(run.call_args.kwargs["check"], True)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()

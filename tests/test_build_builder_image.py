from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.build import _create_builder_image


class BuildBuilderImageTests(unittest.TestCase):
    def test_offline_sysusers_override_is_scoped_to_installer_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rpm_file = "ccache-1-1.aarch64.rpm"
            (root / rpm_file).touch()
            with patch("ludos.build._create_scratch_image") as create:
                _create_builder_image(
                    podman="podman",
                    buildah="buildah",
                    orchestrator="orchestrator:test",
                    root_dir=root,
                    repo_dir=root / "repos",
                    dnf_cache_dir=root / "cache",
                    dnf_persist_dir=root / "persist",
                    dnf_log_dir=root / "log",
                    image="builders:test",
                    package_dir=root,
                    rpm_files=(rpm_file,),
                    releasever="45",
                    arch="aarch64",
                )

        body = create.call_args.kwargs["body"]
        command = shlex.split(next(line for line in body if line.startswith("podman run ")))
        shell_index = command.index("/bin/sh")
        self.assertEqual(command[shell_index + 1], "-ceu")
        installer = command[shell_index + 2]
        self.assertIn("%__systemd_sysusers /usr/lib/rpm/sysusers.sh", installer)
        self.assertIn("> /etc/rpm/macros.ludos-sysusers", installer)
        self.assertNotIn("/target/etc/rpm", installer)
        self.assertTrue(installer.endswith('exec "$@"'))
        self.assertEqual(command[shell_index + 3:shell_index + 5],
                         ["ludos-builder-install", "dnf5"])
        self.assertIn("--installroot=/target", command)
        self.assertIn(f"/rpms/{rpm_file}", command)
        self.assertNotIn("--noscripts", command)
        self.assertNotIn("--nosysusers", command)
        self.assertFalse(any("/target/etc/rpm" in line for line in body))


if __name__ == "__main__":
    unittest.main()

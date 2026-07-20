from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos import postprocess


class PostprocessTests(unittest.TestCase):
    def test_prepare_projection_adds_altfiles_to_regular_nsswitch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            base = root / "base"
            final = root / "final"
            (source / "etc").mkdir(parents=True)
            (source / "etc/nsswitch.conf").write_text(
                "passwd:     sss files systemd\n"
                "group:      sss files systemd\n"
                "hosts:      files dns\n",
                encoding="utf-8",
            )

            postprocess._prepare_projection(source, base, final)

            self.assertEqual(
                (final / "usr/etc/nsswitch.conf").read_text(encoding="utf-8"),
                "passwd: sss files altfiles systemd\n"
                "group: sss files altfiles systemd\n"
                "hosts:      files dns\n",
            )

    def test_prepare_projection_ignores_symlinked_nsswitch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            base = root / "base"
            final = root / "final"
            (source / "etc/authselect").mkdir(parents=True)
            (source / "etc/authselect/nsswitch.conf").write_text(
                "passwd: files altfiles systemd\n",
                encoding="utf-8",
            )
            (source / "etc/nsswitch.conf").symlink_to("authselect/nsswitch.conf")

            postprocess._prepare_projection(source, base, final)

            self.assertFalse((final / "usr/etc/nsswitch.conf").exists())

    def test_add_altfiles_is_idempotent(self) -> None:
        contents = "passwd: files altfiles systemd\ngroup: files altfiles systemd\n"

        self.assertEqual(postprocess._add_altfiles(contents), contents)

    def test_authselect_altfiles_assert_ignores_regular_nsswitch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "etc").mkdir()
            (source / "etc/nsswitch.conf").write_text(
                "passwd: files systemd\ngroup: files systemd\n",
                encoding="utf-8",
            )

            postprocess._assert_authselect_altfiles(source)

    def test_authselect_altfiles_assert_requires_symlinked_image_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "etc/authselect").mkdir(parents=True)
            (source / "etc/nsswitch.conf").symlink_to("authselect/nsswitch.conf")
            (source / "etc/authselect/nsswitch.conf").write_text(
                "passwd: files altfiles systemd\n"
                "group: files altfiles systemd\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "with-altfiles"):
                postprocess._assert_authselect_altfiles(source)

            vendor = source / "usr/share/authselect/vendor/local"
            vendor.mkdir(parents=True)
            (vendor / "README").write_text("with-altfiles::\n", encoding="utf-8")

            postprocess._assert_authselect_altfiles(source)

    def test_prepare_source_overrides_moves_selinux_store_to_etc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "etc/selinux").mkdir(parents=True)
            (source / "etc/selinux/semanage.conf").write_text(
                "module-store = direct\n",
                encoding="utf-8",
            )
            store = source / "var/lib/selinux/targeted"
            (store / "active/modules").mkdir(parents=True)
            (store / "active/file_contexts").write_text("policy\n", encoding="utf-8")
            (store / "semanage.read.LOCK").write_text("", encoding="utf-8")
            subs = source / "etc/selinux/targeted/contexts/files/file_contexts.subs_dist"
            subs.parent.mkdir(parents=True)
            subs.write_text("/etc/example /var/example\n", encoding="utf-8")
            semodule = source / "usr/sbin/semodule"
            semodule.parent.mkdir(parents=True)
            semodule.write_text("", encoding="utf-8")

            with patch("ludos.postprocess.subprocess.run") as run:
                postprocess._prepare_source_overrides(source)

            self.assertEqual(
                (source / "etc/selinux/semanage.conf").read_text(encoding="utf-8"),
                "module-store = direct\n\nstore-root=/etc/selinux\n",
            )
            self.assertEqual(
                (source / "etc/selinux/targeted/active/file_contexts").read_text(
                    encoding="utf-8"
                ),
                "policy\n",
            )
            self.assertFalse((source / "etc/selinux/targeted/semanage.read.LOCK").exists())
            subs_contents = subs.read_text(encoding="utf-8")
            self.assertIn("/usr/etc /etc\n", subs_contents)
            self.assertIn("/usr/etc/example /var/example\n", subs_contents)
            run.assert_called_once_with(
                ["chroot", str(source), "/usr/sbin/semodule", "-nB"],
                check=True,
            )

    def test_collect_var_tmpfiles_skips_selinux_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            (source / "var/lib/selinux/targeted/active/modules").mkdir(parents=True)
            (source / "var/lib/rpm").mkdir(parents=True)
            (source / "var/lib/rpm-state/kernel").mkdir(parents=True)
            (source / "var/lib/NetworkManager").mkdir(parents=True)

            contents = postprocess._collect_var_tmpfiles(source)

            self.assertNotIn("/var/lib/selinux", contents)
            self.assertNotIn("/var/lib/rpm ", contents)
            self.assertIn("d /var/lib/NetworkManager 0755", contents)
            self.assertIn("d /var/lib/rpm-state 0755", contents)

    def test_split_passwd_keeps_uid_zero_in_etc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            passwd = Path(tmp) / "passwd"
            passwd.write_text(
                "root:x:0:0:root:/root:/bin/bash\n"
                "bin:x:1:1:bin:/bin:/usr/sbin/nologin\n",
                encoding="utf-8",
            )

            etc_passwd, usr_passwd = postprocess._split_passwd_file(passwd)

            self.assertEqual(etc_passwd, "root:x:0:0:root:/root:/bin/bash\n")
            self.assertEqual(usr_passwd, "bin:x:1:1:bin:/bin:/usr/sbin/nologin\n")

    def test_split_group_preserves_wheel_in_etc_and_altfiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp) / "group"
            group.write_text(
                "root:x:0:\n"
                "wheel:x:10:\n"
                "bin:x:1:\n",
                encoding="utf-8",
            )

            etc_group, usr_group = postprocess._split_group_file(group, ("wheel",))

            self.assertEqual(etc_group, "root:x:0:\nwheel:x:10:\n")
            self.assertEqual(usr_group, "wheel:x:10:\nbin:x:1:\n")

    def test_run_ostree_commit_adds_version_metadata(self) -> None:
        with patch("ludos.postprocess.subprocess.run") as run:
            run.return_value.returncode = 0
            postprocess._run_ostree_commit(
                Path("/repo"),
                "master",
                Path("/source"),
                ["--tree=tar=/work/tree.tar"],
                "44.20260622",
            )

        command = run.call_args.args[0]
        self.assertIn("--add-metadata-string=version=44.20260622", command)
        self.assertLess(
            command.index("--add-metadata-string=version=44.20260622"),
            command.index("--bootable"),
        )


if __name__ == "__main__":
    unittest.main()

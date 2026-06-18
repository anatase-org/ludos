from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

    def test_add_altfiles_is_idempotent(self) -> None:
        contents = "passwd: files altfiles systemd\ngroup: files altfiles systemd\n"

        self.assertEqual(postprocess._add_altfiles(contents), contents)

    def test_prepare_projection_skips_symlinked_nsswitch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            base = root / "base"
            final = root / "final"
            (source / "etc/authselect").mkdir(parents=True)
            (source / "etc/authselect/nsswitch.conf").write_text(
                "passwd: files systemd\n",
                encoding="utf-8",
            )
            (source / "etc/authselect/authselect.conf").write_text(
                "local\nwith-silent-lastlog\n",
                encoding="utf-8",
            )
            (source / "etc/nsswitch.conf").symlink_to("authselect/nsswitch.conf")

            postprocess._prepare_projection(source, base, final)

            self.assertFalse((final / "usr/etc/nsswitch.conf").exists())
            self.assertEqual(
                (final / "usr/etc/authselect/authselect.conf").read_text(encoding="utf-8"),
                "local\nwith-silent-lastlog\nwith-altfiles\n",
            )

    def test_authselect_altfiles_feature_is_idempotent(self) -> None:
        contents = "local\nwith-altfiles\nwith-silent-lastlog\n"

        self.assertEqual(
            postprocess._add_authselect_feature(contents, "with-altfiles"),
            contents,
        )

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


if __name__ == "__main__":
    unittest.main()

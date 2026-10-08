from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.build import _download_block_packages, _download_exact_packages
from ludos.model import ConfigError
from ludos.rpmverify import (
    RepositoryRpm,
    _key_import_lines,
    _repository_settings,
    _requires_signature,
    repository_rpms,
    verify_repository_rpms,
)


PACKAGE = "example-0:1-1.fc45.x86_64"
FILENAME = "example-1-1.fc45.x86_64.rpm"
RECORD = RepositoryRpm(PACKAGE, "fedora", FILENAME)


def completed(output: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, output, "")


class RepositoryQueryTests(unittest.TestCase):
    def query(self, output: str) -> tuple[RepositoryRpm, ...]:
        with patch("ludos.rpmverify.subprocess.run", return_value=completed(output)):
            return repository_rpms(["podman", "run", "image", "dnf5"], (PACKAGE,))

    def test_maps_exact_nevra_repository_and_location(self) -> None:
        self.assertEqual(self.query(f"{PACKAGE}\tfedora\thttps://mirror.test/{FILENAME}\n"), (RECORD,))

    def test_missing_or_ambiguous_repository_fails(self) -> None:
        for output in ("", f"{PACKAGE}\tfedora\t/{FILENAME}\n{PACKAGE}\tother\t/{FILENAME}\n"):
            with self.subTest(output=output), self.assertRaises(ConfigError):
                self.query(output)

    def test_wrong_nevra_does_not_match(self) -> None:
        with self.assertRaises(ConfigError):
            self.query(f"other-0:1-1.fc45.x86_64\tfedora\t/{FILENAME}\n")

    def test_encoded_path_separator_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.query(f"{PACKAGE}\tfedora\t/escape%2f{FILENAME}\n")


class RepositoryPolicyTests(unittest.TestCase):
    def test_effective_settings_keep_only_verification_fields(self) -> None:
        settings = _repository_settings('''======== "fedora" repository configuration: ========
gpgcheck = 0
pkg_gpgcheck = 1
gpgkey = file://./gpg/key
password = secret
======== "unsigned" repository configuration: ========
gpgcheck = 0
''')
        self.assertTrue(_requires_signature(settings["fedora"], "fedora"))
        self.assertFalse(_requires_signature(settings["unsigned"], "unsigned"))
        self.assertNotIn("password", settings["fedora"])

    def test_old_dnf_fallback_and_missing_policy(self) -> None:
        self.assertTrue(_requires_signature({"gpgcheck": "1"}, "fedora"))
        with self.assertRaises(ConfigError):
            _requires_signature({}, "fedora")

    def test_multiple_local_keys_and_relative_file_url(self) -> None:
        script = "\n".join(_key_import_lines("file://./gpg/key file:///etc/pki/key", "fedora"))
        self.assertIn("cp -- ./gpg/key", script)
        self.assertIn("cp -- /etc/pki/key", script)
        self.assertEqual(script.count("--import"), 2)

    def test_remote_key_retrieval_does_not_expose_credentials(self) -> None:
        with patch("ludos.rpmverify.urllib.request.urlopen", side_effect=OSError("secret")):
            with self.assertRaisesRegex(ConfigError, "failed to retrieve GPG key for repository fedora") as error:
                _key_import_lines("https://user:secret@keys.test/key", "fedora")
        self.assertNotIn("secret", str(error.exception))

    def test_remote_key_is_transferred_without_url_in_script(self) -> None:
        with patch("ludos.rpmverify.urllib.request.urlopen") as open_url:
            open_url.return_value.__enter__.return_value.read.return_value = b"public key"
            script = "\n".join(_key_import_lines("https://keys.test/key", "fedora"))
        self.assertIn("base64 -d", script)
        self.assertNotIn("https://", script)

    def test_enabled_policy_requires_keys(self) -> None:
        with self.assertRaises(ConfigError):
            _key_import_lines("", "fedora")


class RepositoryVerificationTests(unittest.TestCase):
    def verify(self, root: Path, policy: str = "1", exitcode: int = 0) -> str:
        (root / FILENAME).touch()
        base = ["podman", "run", "--volume", f"{root}:/ludos/packages", "image", "dnf5"]
        config = f'''======== "fedora" repository configuration: ========
gpgcheck = {policy}
gpgkey = file://./gpg/key
'''
        with (
            patch("ludos.rpmverify.repository_rpms", return_value=(RECORD,)),
            patch("ludos.rpmverify.subprocess.run", side_effect=[completed(config), completed(returncode=exitcode)]) as run,
        ):
            verify_repository_rpms(base, (PACKAGE,))
        self.assertIn("--interactive", run.call_args.args[0])
        return run.call_args.kwargs["input"]

    def test_isolated_keyring_signature_enforcement_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script = self.verify(Path(temp))
        self.assertIn("mktemp -d /ludos/dnf/persist/", script)
        self.assertIn("trap", script)
        self.assertIn('--root "$verify_root"', script)
        self.assertIn("_pkgverify_level all", script)
        self.assertIn("_keyring rpmdb", script)
        self.assertIn(PACKAGE, script)
        self.assertIn("%{EPOCHNUM}", script)
        self.assertIn(f"rm -f -- /ludos/packages/{FILENAME}", script)

    def test_opt_out_keeps_digest_and_identity_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script = self.verify(Path(temp), "0")
        self.assertNotIn("--import", script)
        self.assertNotIn("_pkgverify_level all", script)
        self.assertIn("_pkgverify_level digest", script)
        self.assertIn("--checksig", script)

    def test_verification_failure_stops_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ConfigError, "fedora"):
                self.verify(Path(temp), exitcode=1)


class DownloadVerificationTests(unittest.TestCase):
    def test_exact_download_verifies_before_returning(self) -> None:
        events = []
        with (
            patch("ludos.build._run_logged_command", side_effect=lambda *args: events.append("download")),
            patch("ludos.build._verify_repository_rpms", side_effect=lambda *args: events.append("verify")),
        ):
            _download_exact_packages(["dnf5"], (PACKAGE,), "/ludos/packages")
        self.assertEqual(events, ["download", "verify"])

    def test_existing_cached_builder_rpm_is_verified_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / FILENAME).touch()
            with (
                patch("ludos.build._package_rpm_files", return_value=(FILENAME,)),
                patch("ludos.build._download_exact_packages") as download,
                patch("ludos.build._verify_repository_rpms") as verify,
            ):
                result = _download_block_packages(["dnf5"], (PACKAGE,), package_dir=root, resolve_dependencies=True)
        self.assertEqual(result, (FILENAME,))
        download.assert_not_called()
        verify.assert_called_once_with(["dnf5"], (PACKAGE,))

    def test_uncached_normal_and_builder_downloads_share_verifier(self) -> None:
        for dependencies in (False, True):
            with self.subTest(dependencies=dependencies), tempfile.TemporaryDirectory() as temp:
                with (
                    patch("ludos.build._package_rpm_files", return_value=(FILENAME,)),
                    patch("ludos.build._run_logged_command"),
                    patch("ludos.build._verify_repository_rpms") as verify,
                ):
                    _download_block_packages(["dnf5"], (PACKAGE,), package_dir=Path(temp), resolve_dependencies=dependencies)
                verify.assert_called_once_with(["dnf5"], (PACKAGE,), "/ludos/packages")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from ludos.__main__ import build_parser, main
from ludos.model import ConfigError
from ludos.upload import gpg
from ludos.upload.gpg import (
    GpgSigningConfig,
    config_from_env,
    sign_attached_data,
    sign_detached_file,
    sign_file,
)


ROOT = Path(__file__).resolve().parents[2]
CERT_REL = Path("ludos/tests/fixtures/fake-gpg.pub.asc")
CERT = ROOT / CERT_REL
KEY_BASE_URI = "gcloud://projects/example/locations/global/keyRings/test/cryptoKeys/openpgp"
KEY_URI = KEY_BASE_URI + "/cryptoKeyVersions/7"


class UploadGpgTests(unittest.TestCase):
    def test_registry_gpg_sign_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "gpg",
                "sign",
                "cache/anatase.iso",
                "cache/anatase.iso.gpg",
                "--verify",
            ]
        )

        self.assertEqual(args.registry_action, "gpg")
        self.assertEqual(args.registry_gpg_action, "sign")
        self.assertEqual(args.input_path, Path("cache/anatase.iso"))
        self.assertEqual(args.output_path, Path("cache/anatase.iso.gpg"))
        self.assertTrue(args.verify)

    def test_registry_gpg_sign_detached_parser(self) -> None:
        args = build_parser().parse_args(
            ["registry", "gpg", "sign-detached", "cache/anatase.iso", "--verify"]
        )

        self.assertEqual(args.registry_action, "gpg")
        self.assertEqual(args.registry_gpg_action, "sign-detached")
        self.assertEqual(args.input_path, Path("cache/anatase.iso"))
        self.assertTrue(args.verify)

    def test_registry_gpg_sign_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "gpg",
                "sign",
                "cache/anatase.iso",
                "cache/anatase.iso.gpg",
                "--verify",
            ]
        )
        args.project = SimpleNamespace(root=ROOT)

        with patch("ludos.__main__.sign_file", return_value=0) as sign:
            self.assertEqual(args.func(args), 0)

        sign.assert_called_once_with(
            Path("cache/anatase.iso"),
            Path("cache/anatase.iso.gpg"),
            verify=True,
            project_root=ROOT,
        )

    def test_registry_gpg_sign_detached_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "gpg",
                "sign-detached",
                "cache/anatase.iso",
                "--verify",
            ]
        )
        args.project = SimpleNamespace(root=ROOT)

        with patch("ludos.__main__.sign_detached", return_value=0) as sign:
            self.assertEqual(args.func(args), 0)

        sign.assert_called_once_with(
            Path("cache/anatase.iso"),
            verify=True,
            project_root=ROOT,
        )

    def test_registry_gpg_main_uses_discovered_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subdir = root / "subdir"
            subdir.mkdir()
            (root / "ludos.yml").write_text("version: 1\nname: Test\n")

            with (
                patch("sys.argv", ["ludos", "registry", "gpg", "sign-detached", "artifact.iso"]),
                patch("ludos.__main__.sign_detached", return_value=0) as sign,
                patch("ludos.__main__.configure_logging"),
                patch("pathlib.Path.cwd", return_value=subdir),
                patch("os.chdir"),
            ):
                self.assertEqual(main(), 0)

            sign.assert_called_once_with(
                Path("artifact.iso"),
                verify=False,
                project_root=root,
            )

    def test_registry_gpg_rejects_missing_action(self) -> None:
        with patch("sys.stderr", new=StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["registry", "gpg"])

    def test_config_requires_environment(self) -> None:
        with self.assertRaisesRegex(ConfigError, "LUDOS_GPG_CERT"):
            config_from_env({})
        with self.assertRaisesRegex(ConfigError, "LUDOS_GPG_KEY"):
            config_from_env({"LUDOS_GPG_CERT": str(CERT)})

    def test_config_rejects_bad_protocol_and_key_path(self) -> None:
        env = {"LUDOS_GPG_CERT": str(CERT), "LUDOS_GPG_KEY": "file:///key"}
        with self.assertRaisesRegex(ConfigError, "gcloud://"):
            config_from_env(env)

        env = {"LUDOS_GPG_CERT": str(CERT), "LUDOS_GPG_KEY": "gcloud://bad"}
        with self.assertRaisesRegex(ConfigError, "invalid gcloud"):
            config_from_env(env)

        env = {"LUDOS_GPG_CERT": str(CERT), "LUDOS_GPG_KEY": KEY_BASE_URI}
        with self.assertRaisesRegex(ConfigError, "invalid gcloud"):
            config_from_env(env)

    def test_config_rejects_bad_subkey_selector(self) -> None:
        env = {"LUDOS_GPG_CERT": f"{CERT}:s3", "LUDOS_GPG_KEY": KEY_URI}
        with self.assertRaisesRegex(ConfigError, "s3"):
            config_from_env(env)

        env = {"LUDOS_GPG_CERT": f"{CERT}:s0", "LUDOS_GPG_KEY": KEY_URI}
        with self.assertRaisesRegex(ConfigError, "one-based"):
            config_from_env(env)

    def test_public_cert_selects_primary_by_default(self) -> None:
        config = config_from_env(
            {
                "LUDOS_GPG_CERT": str(CERT_REL),
                "LUDOS_GPG_KEY": KEY_URI,
            },
            project_root=ROOT,
        )

        self.assertEqual(
            config.public_key.fingerprint_hex,
            "DCE112B5F2DB39500522C6F8B0CB49751A699FD3",
        )
        self.assertEqual(config.cert_path, CERT)

    def test_public_cert_selects_signing_subkeys(self) -> None:
        first = config_from_env({"LUDOS_GPG_CERT": f"{CERT}:s1", "LUDOS_GPG_KEY": KEY_URI})
        second = config_from_env({"LUDOS_GPG_CERT": f"{CERT}:s2", "LUDOS_GPG_KEY": KEY_URI})

        self.assertEqual(
            first.public_key.fingerprint_hex,
            "394CA3EE1563AFBC1A5C8F0DA837A12F2B1D4100",
        )
        self.assertEqual(
            second.public_key.fingerprint_hex,
            "B922794B7A739D9AD10DC58F31374FEF0CB4854D",
        )

    def test_cert_path_from_spec_resolves_project_relative_selector(self) -> None:
        self.assertEqual(
            gpg.cert_path_from_spec(f"{CERT_REL}:s2", project_root=ROOT),
            CERT,
        )

    def test_gcloud_key_path_parses_version(self) -> None:
        config = config_from_env(
            {
                "LUDOS_GPG_CERT": str(CERT),
                "LUDOS_GPG_KEY": KEY_URI,
            }
        )

        self.assertEqual(config.gcloud_key.project, "example")
        self.assertEqual(config.gcloud_key.location, "global")
        self.assertEqual(config.gcloud_key.keyring, "test")
        self.assertEqual(config.gcloud_key.key, "openpgp")
        self.assertEqual(config.gcloud_key.version, "7")

    def test_attached_signature_packet_shape(self) -> None:
        config = _fake_config()

        with patch("ludos.upload.gpg.time.time", return_value=1783185600):
            signed = sign_attached_data(b"hello", config)

        packets = list(gpg._iter_packets(signed))
        self.assertEqual([tag for tag, _body in packets], [4, 11, 2])
        self.assertEqual(packets[0][1][0], 3)
        self.assertEqual(packets[0][1][1], 0)
        self.assertEqual(packets[0][1][2], gpg.SHA256_ALGORITHM)
        self.assertEqual(packets[0][1][3], gpg.RSA_ALGORITHM)
        self.assertEqual(packets[0][1][4:12], config.public_key.key_id)
        self.assertEqual(packets[1][1][-5:], b"hello")
        self.assertEqual(packets[2][1][0], 4)
        self.assertEqual(packets[2][1][1], 0)
        self.assertEqual(packets[2][1][2], gpg.RSA_ALGORITHM)
        self.assertEqual(packets[2][1][3], gpg.SHA256_ALGORITHM)

    def test_detached_signature_writes_requested_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "anatase.iso"
            output_path = root / "anatase.iso.sig"
            input_path.write_bytes(b"iso")

            sign_detached_file(input_path, output_path, _fake_config())

            packets = list(gpg._iter_packets(output_path.read_bytes()))
            self.assertEqual([tag for tag, _body in packets], [2])

    def test_sign_detached_command_writes_next_to_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "anatase.iso"
            input_path.write_bytes(b"iso")

            with patch("ludos.upload.gpg.config_from_env", return_value=_fake_config()):
                self.assertEqual(gpg.sign_detached(input_path), 0)

            self.assertTrue((root / "anatase.iso.sig").exists())

    def test_verify_attached_uses_cache_gpg_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input"
            output_path = root / "output"
            input_path.write_bytes(b"payload")

            with (
                patch("ludos.upload.gpg.config_from_env", return_value=_fake_config()),
                patch("ludos.upload.gpg._run_streamed_command") as run,
            ):
                self.assertEqual(
                    sign_file(input_path, output_path, verify=True, project_root=root),
                    0,
                )

            self.assertEqual(
                run.call_args_list,
                [
                    call(
                        [
                            "gpg",
                            "--homedir",
                            str(root / "cache" / "gpg"),
                            "--batch",
                            "--import",
                            str(CERT),
                        ],
                    ),
                    call(
                        [
                            "gpg",
                            "--homedir",
                            str(root / "cache" / "gpg"),
                            "--batch",
                            "--verify",
                            str(output_path),
                        ],
                    ),
                ],
            )

    def test_verify_detached_uses_cache_gpg_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_path = root / "input"
            input_path.write_bytes(b"payload")

            with (
                patch("ludos.upload.gpg.config_from_env", return_value=_fake_config()),
                patch("ludos.upload.gpg._run_streamed_command") as run,
            ):
                self.assertEqual(
                    gpg.sign_detached(input_path, verify=True, project_root=root),
                    0,
                )

            output_path = root / "input.sig"
            self.assertEqual(
                run.call_args_list,
                [
                    call(
                        [
                            "gpg",
                            "--homedir",
                            str(root / "cache" / "gpg"),
                            "--batch",
                            "--import",
                            str(CERT),
                        ],
                    ),
                    call(
                        [
                            "gpg",
                            "--homedir",
                            str(root / "cache" / "gpg"),
                            "--batch",
                            "--verify",
                            str(output_path),
                            str(input_path),
                        ],
                    ),
                ],
            )

    def test_gcloud_signs_digest_info_without_digest_algorithm(self) -> None:
        config = config_from_env({"LUDOS_GPG_CERT": str(CERT), "LUDOS_GPG_KEY": KEY_URI})

        def run(command: list[str]) -> None:
            self.assertNotIn("--digest-algorithm=sha256", command)
            self.assertIn("--version=7", command)
            signature_file = Path(
                next(item.removeprefix("--signature-file=") for item in command if item.startswith("--signature-file="))
            )
            signature_file.write_bytes(b"\x81" + b"\x00" * 511)

        with patch("ludos.upload.gpg._run_streamed_command", side_effect=run):
            self.assertEqual(
                gpg._gcloud_sign_digest_info(b"digest-info", config),
                b"\x81" + b"\x00" * 511,
            )

    def test_streamed_command_sends_output_to_ludos_stream(self) -> None:
        process = _FakeProcess("line one\nline two\n")

        with (
            patch("ludos.upload.gpg.subprocess.Popen", return_value=process),
            patch("ludos.upload.gpg.stream") as stream,
        ):
            gpg._run_streamed_command(["gpg", "--version"])

        stream.assert_has_calls([call("line one\n"), call("line two\n")])


def _fake_config() -> GpgSigningConfig:
    config = config_from_env({"LUDOS_GPG_CERT": f"{CERT}:s2", "LUDOS_GPG_KEY": KEY_URI})
    return replace(config, signer=lambda _digest_info, _config: b"\x81" + b"\x00" * 511)


class _FakeProcess:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.stdout = io.StringIO(output)
        self._returncode = returncode
        self._waited = False

    def wait(self) -> int:
        self._waited = True
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if self._waited else None

    def terminate(self) -> None:
        self._returncode = -15


if __name__ == "__main__":
    unittest.main()

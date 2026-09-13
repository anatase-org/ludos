from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.__main__ import build_parser
from ludos.disk import (
    DISK_ARCHITECTURES,
    ESP_START_SECTOR,
    ESP_SIZE,
    GIB,
    ROOT_GROW_ATTRIBUTE,
    ROOT_HEADROOM,
    ROOT_START_SECTOR,
    DiskContext,
    bootc_disk,
    _disk_architecture,
    _disk_builder_command,
    _disk_builder_script,
    _flatpak_script,
    _local_oci_path,
    _ostree_target_ref,
    _parse_flatpak_uris,
    _parse_size,
    _publish_output,
    _pull_source_image,
    _prepare_output_target,
    _probe_execution,
    _require_rootless_podman,
)
from ludos.model import ConfigError, InstallerConfig, InstallerFlatpaksConfig, Manifest


def _manifest(installer: InstallerConfig = InstallerConfig()) -> Manifest:
    return Manifest(
        version=1,
        env={"arch": "x86_64"},
        releasever="44",
        distro="f44-x86_64",
        orchestrator="quay.io/fedora/fedora:44",
        bootstrap="cards/bootstrap.yml",
        repos=tuple(),
        cards=("cards/base/kernel",),
        name="Anatase",
        installer=installer,
    )


def _context(tmp: Path, installer: InstallerConfig = InstallerConfig()) -> DiskContext:
    return DiskContext(
        manifest=_manifest(installer),
        manifest_path=tmp / "anatase.yml",
        source_ref="ostree-unverified-image:oci:/ludos/source:latest",
        target_ref="registry:i.anatase.org/anatase:stable",
        output_dir=tmp / "cache/disk/anatase-f44-x86_64",
        work_dir=tmp / "cache/disk/.anatase.tmp",
        source_mount=tmp / "cache/disk/.anatase.tmp/source-oci",
        target_arch="x86_64",
        flatpak_uris=tuple(),
        requested_size=None,
        podman="podman",
        tooling_image="sha256:" + "a" * 64,
        tooling_arch="x86_64",
        root_uuid="11111111-2222-4333-8444-555555555555",
    )


class DiskParserTests(unittest.TestCase):
    def test_parser_accepts_disk_options(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "disk",
                "anatase.yml",
                "cache/oci/anatase-f44-aarch64",
                "--target-ref",
                "docker://i.anatase.org/anatase:stable",
                "--output",
                "cache/disk/arm",
                "--cache-dir",
                "cache",
                "--arch",
                "arm64",
                "--orchestrator",
                "i.anatase.org/anatase:stable",
                "--size",
                "16G",
                "--flatpak-uri",
                "anatase=oci+https://flatpaks.example/arm",
                "--force",
            ]
        )

        self.assertEqual(args.command, "bootc")
        self.assertEqual(args.bootc_action, "disk")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertEqual(args.target_ref, "docker://i.anatase.org/anatase:stable")
        self.assertEqual(args.output, Path("cache/disk/arm"))
        self.assertEqual(args.arch, "arm64")
        self.assertEqual(args.orchestrator, "i.anatase.org/anatase:stable")
        self.assertEqual(args.size, "16G")
        self.assertTrue(args.force)

    def test_disk_dispatch(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "disk",
                "anatase.yml",
                "docker://example.test/os:stable",
                "--size",
                "8G",
            ]
        )
        with (
            patch("ludos.__main__.show_logo") as show_logo,
            patch("ludos.__main__.bootc_disk", return_value=0) as disk,
        ):
            self.assertEqual(args.func(args), 0)

        show_logo.assert_called_once_with(args)
        disk.assert_called_once_with(
            Path("anatase.yml"),
            "docker://example.test/os:stable",
            target_ref=None,
            output=None,
            cache_dir=None,
            arch=None,
            orchestrator=None,
            size="8G",
            flatpak_uris=tuple(),
            force=False,
        )


class DiskConfigurationTests(unittest.TestCase):
    def test_architectures_use_discoverable_root_guids(self) -> None:
        self.assertEqual(
            DISK_ARCHITECTURES["aarch64"].root_type_guid,
            "b921b045-1df0-41c3-af44-4c6f280d3fae",
        )
        self.assertEqual(
            DISK_ARCHITECTURES["x86_64"].root_type_guid,
            "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",
        )
        self.assertEqual(_disk_architecture("arm64").boot_filename, "BOOTAA64.EFI")
        self.assertEqual(_disk_architecture("amd64").boot_filename, "BOOTX64.EFI")

    def test_rejects_unknown_architecture(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported disk architecture"):
            _disk_architecture("riscv64")

    def test_size_parser(self) -> None:
        self.assertEqual(_parse_size("16G"), 16 * GIB)
        self.assertEqual(_parse_size("16GiB"), 16 * GIB)
        self.assertEqual(_parse_size(str(4 * GIB)), 4 * GIB)

    def test_size_parser_rejects_invalid_and_small_sizes(self) -> None:
        for value in ("", "0", "3.5G", "1G"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                _parse_size(value)

    def test_registry_target_ref_is_normalized(self) -> None:
        self.assertEqual(
            _ostree_target_ref("docker://i.example.test/os:stable"),
            "ostree-unverified-registry:i.example.test/os:stable",
        )
        self.assertEqual(
            _ostree_target_ref("registry:i.example.test/os:stable"),
            "ostree-unverified-registry:i.example.test/os:stable",
        )

    def test_local_target_ref_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "persistent registry"):
            _ostree_target_ref("oci:/build/os:latest")

    def test_local_oci_layout_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(_local_oci_path(str(root)), root.resolve())
            self.assertEqual(_local_oci_path(f"oci:{root}:latest"), root.resolve())
        self.assertIsNone(_local_oci_path("quay.io/example/os:stable"))

    def test_local_oci_layout_requires_persistent_target_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "anatase.yml"
            manifest_path.touch()
            source = root / "source-oci"
            source.mkdir()
            with (
                patch("ludos.disk.Manifest.from_file", return_value=_manifest()),
                self.assertRaisesRegex(ConfigError, "--target-ref is required"),
            ):
                bootc_disk(manifest_path, str(source), arch="x86_64")

    def test_output_requires_force_and_preserves_it_until_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "disk"
            output.mkdir()
            (output / "disk.raw").touch()
            with self.assertRaisesRegex(ConfigError, "use --force"):
                _prepare_output_target(output, force=False)
            _prepare_output_target(output, force=True)
            self.assertTrue((output / "disk.raw").exists())

    def test_force_publishes_completed_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            ctx.output_dir.mkdir(parents=True)
            (ctx.output_dir / "old").touch()
            ctx.source_mount.mkdir(parents=True)
            ctx.disk.touch()

            _publish_output(ctx, force=True)

            self.assertTrue((ctx.output_dir / "disk.raw").is_file())
            self.assertFalse((ctx.output_dir / "old").exists())
            self.assertFalse(ctx.source_mount.exists())

    def test_rootless_guard(self) -> None:
        rootless = subprocess.CompletedProcess([], 0, stdout="true\n", stderr="")
        with patch("ludos.disk._run", return_value=rootless) as run:
            _require_rootless_podman("podman")
        run.assert_called_once()

        rootful = subprocess.CompletedProcess([], 0, stdout="false\n", stderr="")
        with patch("ludos.disk._run", return_value=rootful):
            with self.assertRaisesRegex(ConfigError, "requires rootless Podman"):
                _require_rootless_podman("podman")

    def test_pull_normalizes_bare_podman_image_id(self) -> None:
        pulled = subprocess.CompletedProcess([], 0, stdout="image-ref\n", stderr="")
        inspected = subprocess.CompletedProcess([], 0, stdout="a" * 64 + "\n", stderr="")
        with patch("ludos.disk._run", side_effect=(pulled, inspected)):
            self.assertEqual(
                _pull_source_image("podman", "example.test/os:stable", "x86_64"),
                "sha256:" + "a" * 64,
            )


class DiskFlatpakTests(unittest.TestCase):
    def setUp(self) -> None:
        self.groups = (
            InstallerFlatpaksConfig(
                repo="anatase",
                nodeps=True,
                preinstall=("org.anatase.TextEditor", "org.anatase.ImageViewer"),
                installer=("org.fedoraproject.AnacondaInstaller",),
            ),
        )

    def test_preinstall_only_and_no_deps(self) -> None:
        script = _flatpak_script(
            self.groups,
            (("anatase", "oci+https://flatpaks.example/arm"),),
            "aarch64",
        )
        self.assertIn("org.anatase.TextEditor", script)
        self.assertIn("org.anatase.ImageViewer", script)
        self.assertNotIn("AnacondaInstaller", script)
        self.assertIn("--arch=aarch64", script)
        self.assertIn("--no-deps", script)
        self.assertIn("remote-modify", script)

    def test_uri_override_validation(self) -> None:
        self.assertEqual(
            _parse_flatpak_uris(("anatase=https://example.test",), self.groups),
            (("anatase", "https://example.test"),),
        )
        with self.assertRaisesRegex(ConfigError, "remote without preinstalls"):
            _parse_flatpak_uris(("flathub=https://example.test",), self.groups)
        with self.assertRaisesRegex(ConfigError, "duplicate Flatpak URI override"):
            _parse_flatpak_uris(
                (
                    "anatase=https://one.example.test",
                    "anatase=https://two.example.test",
                ),
                self.groups,
            )

    def test_installer_only_remote_cannot_be_overridden(self) -> None:
        groups = (
            InstallerFlatpaksConfig(
                repo="installer-only",
                installer=("org.example.Installer",),
            ),
        )
        with self.assertRaisesRegex(ConfigError, "remote without preinstalls"):
            _parse_flatpak_uris(
                ("installer-only=https://example.test",),
                groups,
            )


class DiskBuilderTests(unittest.TestCase):
    def test_command_is_rootless_and_has_no_devices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = _disk_builder_command(_context(Path(tmp)))
        joined = " ".join(command)
        self.assertNotIn("--privileged", command)
        self.assertNotIn("--device", command)
        self.assertNotIn("losetup", joined)
        self.assertNotIn("qemu-system", joined)
        self.assertIn("label=disable", command)
        self.assertIn("seccomp=unconfined", command)
        self.assertIn("linux/amd64", command)

    def test_separate_orchestrator_runs_native_without_cross_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            object.__setattr__(ctx, "target_arch", "aarch64")
            object.__setattr__(ctx, "tooling_image", "sha256:" + "b" * 64)
            object.__setattr__(ctx, "tooling_arch", "x86_64")
            command = _disk_builder_command(ctx)

        self.assertEqual(command[command.index("--platform") + 1], "linux/amd64")
        self.assertIn("sha256:" + "b" * 64, command)
        self.assertNotIn("type=image", " ".join(command))

    def test_execution_probe_uses_orchestrator_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            object.__setattr__(ctx, "target_arch", "aarch64")
            object.__setattr__(ctx, "tooling_image", "sha256:" + "b" * 64)
            object.__setattr__(ctx, "tooling_arch", "x86_64")
            completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with patch("ludos.disk._run", return_value=completed) as run:
                _probe_execution(ctx)

        command = run.call_args.args[0]
        self.assertIn("linux/amd64", command)
        self.assertIn("sha256:" + "b" * 64, command)
        self.assertNotIn("linux/arm64", command)

    def test_script_builds_regular_file_gpt_fat_and_btrfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = _disk_builder_script(_context(Path(tmp)))
        self.assertIn("fakeroot -s", script)
        self.assertIn("mkfs.btrfs", script)
        self.assertIn("--rootdir", script)
        self.assertIn("mkfs.vfat", script)
        self.assertIn("mcopy -s", script)
        self.assertIn("sfdisk --quiet", script)
        self.assertIn(f'GUID:{ROOT_GROW_ATTRIBUTE}', script)
        self.assertIn(f"truncate -s {ESP_SIZE}", script)
        self.assertIn(f"start={ESP_START_SECTOR}", script)
        self.assertIn(f"start={ROOT_START_SECTOR}", script)
        self.assertIn(f"FILESYSTEM_BYTES + {ROOT_HEADROOM}", script)
        self.assertIn("DISK_BYTES=$REQUESTED_SIZE", script)
        self.assertIn('test "$DISK_BYTES" -lt "$MIN_DISK"', script)
        self.assertIn("GrowFileSystem=yes", script)
        self.assertIn("BOOTX64.EFI", script)
        self.assertIn('$DEPLOY/usr/lib/efi', script)
        self.assertIn('$DEPLOY/usr/lib/bootupd', script)
        self.assertIn('disk_status "Validating disk image"', script)
        self.assertNotIn("cryptsetup", script)
        self.assertNotIn("mount ", script)
        self.assertNotIn("losetup", script)

    def test_script_contains_only_preinstall_flatpaks(self) -> None:
        installer = InstallerConfig(
            flatpaks=(
                InstallerFlatpaksConfig(
                    repo="anatase",
                    preinstall=("org.anatase.TextEditor",),
                    installer=("org.fedoraproject.AnacondaInstaller",),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            script = _disk_builder_script(_context(Path(tmp), installer))
        self.assertIn("org.anatase.TextEditor", script)
        self.assertNotIn("org.fedoraproject.AnacondaInstaller", script)


class DiskRootlessIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("LUDOS_DISK_INTEGRATION_IMAGE"),
        "set LUDOS_DISK_INTEGRATION_IMAGE to run rootless filesystem integration",
    )
    def test_raw_image_preserves_fakeroot_selinux_xattr(self) -> None:
        _require_rootless_podman("podman")
        image = os.environ["LUDOS_DISK_INTEGRATION_IMAGE"]
        cache = Path.cwd() / "cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="disk-integration-", dir=cache) as tmp:
            command = [
                "podman",
                "run",
                "--rm",
                "--security-opt",
                "label=disable",
                "--mount",
                f"type=bind,source={Path(tmp).resolve()},target=/work",
                image,
                "/bin/sh",
                "-ceu",
                """
mkdir -p /work/root /work/esp/EFI/BOOT
printf loader > /work/esp/EFI/BOOT/BOOTX64.EFI
fakeroot -- /bin/sh -ceux '
    touch /work/root/labeled
    setfattr -n security.selinux -v system_u:object_r:usr_t:s0 /work/root/labeled
    truncate -s 256M /work/root.btrfs
    mkfs.btrfs --force --rootdir /work/root /work/root.btrfs >/dev/null
    truncate -s 64M /work/esp.vfat
    mkfs.vfat -F 32 /work/esp.vfat >/dev/null
    mcopy -s -i /work/esp.vfat /work/esp/EFI ::/
    truncate -s 384M /work/disk.raw
    sfdisk --quiet /work/disk.raw <<EOF
label: gpt
unit: sectors

start=2048, size=131072, type=c12a7328-f81f-11d2-ba4b-00a0c93ec93b
start=133120, size=653278, type=4f68bce3-e8cd-4db1-96e7-fbcaf984b709
EOF
    dd if=/work/esp.vfat of=/work/disk.raw bs=512 seek=2048 conv=notrunc,sparse status=none
    dd if=/work/root.btrfs of=/work/disk.raw bs=512 seek=133120 conv=notrunc,sparse status=none
    sfdisk --verify /work/disk.raw
    blkid -p -O 1048576 -S 67108864 /work/disk.raw | grep -q vfat
    blkid -p -O 68157440 -S 334478336 /work/disk.raw | grep -q btrfs
    mdir -i /work/esp.vfat ::/EFI/BOOT/BOOTX64.EFI >/dev/null
    dd if=/work/disk.raw of=/work/extracted.btrfs bs=512 skip=133120 count=524288 conv=sparse status=none
    btrfs inspect-internal dump-tree /work/extracted.btrfs | grep -q security.selinux
'
""",
            ]
            result = subprocess.run(command, check=False, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

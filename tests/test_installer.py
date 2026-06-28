from __future__ import annotations

import subprocess
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.__main__ import build_parser
from ludos.installer import (
    BIOS_ELTORITO_IMAGE,
    CONTAINER_WORKDIR,
    EFI_BOOT_IMAGE,
    LUDOS_EFI_ASSET_DIR,
    LUDOS_EFI_BOOT_ASSETS,
    InstallerContext,
    LIVE_ROOT_IMAGE,
    bootc_installer,
    _build_installer_image,
    _container_ludos_efi_asset_dir,
    _copy_installer_files,
    _copy_live_iso_payload,
    _copy_ludos_efi_payload,
    _efi_asset_script,
    _efi_image_size_kib,
    _grub_config,
    _grub_mkimage_command,
    _installer_containerfile,
    _installer_image_ref,
    _installer_latest_image_ref,
    _installer_build_script,
    _pull_source_image,
    _fat_label_for_manifest,
    _label_base,
    _container_name,
    _erofs_profile,
    _erofs_worker_count,
    _kernel_and_initramfs,
    _kernel_asset_script,
    _mcopy_tree_script,
    _mkfs_erofs_tar_command,
    _resolve_output_dir,
    _run_host,
    _safe_ref_name,
    _source_image_ref,
    _stream_root_erofs,
    _tool_command,
    _tool_path,
    _xorriso_command,
)
from ludos.model import ConfigError, InstallerConfig, Manifest


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


def _context(
    tmp: Path,
    *,
    installer: InstallerConfig = InstallerConfig(),
    orchestrator: str = "orchestrator",
    scratch: bool = False,
) -> InstallerContext:
    return InstallerContext(
        manifest=_manifest(installer),
        manifest_path=tmp / "anatase.yml",
        ref=str(tmp / "cache/oci/anatase-f44-x86_64"),
        output_dir=tmp / "cache/iso/anatase-installer",
        orchestrator=orchestrator,
        scratch=scratch,
        podman="podman",
    )


class InstallerParserTests(unittest.TestCase):
    def test_parser_accepts_installer(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "installer",
                "anatase.yml",
                "cache/oci/anatase-f44-x86_64",
                "--output",
                "cache/iso/anatase-installer",
                "--cache-dir",
                "cache",
                "--orchestrator",
                "localhost/tools:latest",
                "--scratch",
            ]
        )

        self.assertEqual(args.command, "bootc")
        self.assertEqual(args.bootc_action, "installer")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertEqual(args.ref, "cache/oci/anatase-f44-x86_64")
        self.assertEqual(args.output, Path("cache/iso/anatase-installer"))
        self.assertEqual(args.cache_dir, Path("cache"))
        self.assertEqual(args.orchestrator, "localhost/tools:latest")
        self.assertTrue(args.scratch)

    def test_parser_requires_installer_ref(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["bootc", "installer", "anatase.yml"])

    def test_parser_defaults_installer_orchestrator(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "installer",
                "anatase.yml",
                "cache/oci/anatase-f44-x86_64",
            ]
        )

        self.assertIsNone(args.orchestrator)
        self.assertFalse(args.scratch)


class InstallerManifestTests(unittest.TestCase):
    def test_manifest_installer_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "anatase.yml"
            manifest.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Test OS",
                        "releasever: '44'",
                        "distro: f44-$arch",
                        "orchestrator: quay.io/fedora/fedora:44",
                        "bootstrap: cards/bootstrap.yml",
                        "repos: []",
                        "cards:",
                        "  - cards/base/kernel",
                    ]
                ),
                encoding="utf-8",
            )

            parsed = Manifest.from_file(manifest)

        self.assertEqual(parsed.installer, InstallerConfig())
        self.assertEqual(parsed.name, "Test OS")

    def test_manifest_installer_parses_files_and_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "anatase.yml"
            manifest.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Test OS",
                        "releasever: '44'",
                        "distro: f44-$arch",
                        "orchestrator: quay.io/fedora/fedora:44",
                        "bootstrap: cards/bootstrap.yml",
                        "repos: []",
                        "cards:",
                        "  - cards/base/kernel",
                        "installer:",
                        "  ostree: true",
                        "  files:",
                        "    - installer.ks",
                        "  build: |",
                        "    echo installer",
                    ]
                ),
                encoding="utf-8",
            )

            parsed = Manifest.from_file(manifest)

        self.assertEqual(parsed.installer.files, ("installer.ks",))
        self.assertEqual(parsed.installer.build, "echo installer")
        self.assertTrue(parsed.installer.ostree)
        self.assertEqual(parsed.name, "Test OS")

    def test_manifest_installer_rejects_non_boolean_ostree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "anatase.yml"
            manifest.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Test OS",
                        "releasever: '44'",
                        "distro: f44-$arch",
                        "orchestrator: quay.io/fedora/fedora:44",
                        "bootstrap: cards/bootstrap.yml",
                        "repos: []",
                        "cards:",
                        "  - cards/base/kernel",
                        "installer:",
                        "  ostree: yes please",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "installer.ostree.*boolean"):
                Manifest.from_file(manifest)


class InstallerHelperTests(unittest.TestCase):
    def test_output_override_bypasses_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                _resolve_output_dir(
                    root / "anatase.yml",
                    "cache/oci/anatase-f44-x86_64",
                    root / "custom-output",
                    root / "cache",
                ),
                (root / "custom-output").resolve(),
            )

    def test_default_output_uses_manifest_artifact_name_under_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "anatase.yml"
            manifest.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "env:",
                        "  arch: x86_64",
                        "  releasever: 44",
                        "releasever: $releasever",
                        "distro: f$releasever-$arch",
                        "orchestrator: quay.io/fedora/fedora:$releasever",
                        "bootstrap: cards/bootstrap.yml",
                        "repos: []",
                        "cards:",
                        "  - cards/base/kernel",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                _resolve_output_dir(
                    manifest,
                    "cache/oci/anatase-f44-x86_64",
                    None,
                    None,
                ),
                root / "cache/iso/anatase-f44-x86_64",
            )

    def test_source_image_ref_accepts_oci_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            oci_dir = Path(tmp) / "cache/oci/anatase-f44-x86_64"
            oci_dir.mkdir(parents=True)

            self.assertEqual(
                _source_image_ref(str(oci_dir)),
                f"oci:{oci_dir.resolve()}:latest",
            )

    def test_run_host_streams_uncaptured_output(self) -> None:
        class Process:
            def __init__(self, *_args, **_kwargs) -> None:
                self.stdin = None
                self.stdout = io.StringIO("line 1\nline 2\n")

            def wait(self) -> int:
                return 0

            def poll(self) -> int:
                return 0

        with (
            patch("ludos.installer.subprocess.Popen", side_effect=Process),
            patch("ludos.installer.stream") as stream,
        ):
            result = _run_host(["podman", "build"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "line 1\nline 2\n")
        stream.assert_any_call("line 1\n")
        stream.assert_any_call("line 2\n")

    def test_bootc_installer_defaults_orchestrator_to_installer_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "anatase.yml"
            manifest.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Anatase",
                        "releasever: '44'",
                        "distro: f44-$arch",
                        "orchestrator: quay.io/fedora/fedora:44",
                        "bootstrap: cards/bootstrap.yml",
                        "repos: []",
                        "cards:",
                        "  - cards/base/kernel",
                    ]
                ),
                encoding="utf-8",
            )
            oci_dir = root / "cache/oci/anatase-f44-x86_64"
            oci_dir.mkdir(parents=True)
            seen: list[tuple[str, bool]] = []

            def record(ctx: InstallerContext, *_args) -> None:
                seen.append((ctx.orchestrator, ctx.scratch))

            with (
                patch("ludos.installer.shutil.which", return_value="podman"),
                patch("ludos.installer._prepare_installer_build_context"),
                patch(
                    "ludos.installer._pull_source_image",
                    return_value="sha256:" + "d" * 64,
                ),
                patch(
                    "ludos.installer._build_installer_image",
                    return_value="localhost/installer:test",
                ) as build_image,
                patch("ludos.installer._create_root_erofs", side_effect=record),
                patch("ludos.installer._create_efi_image"),
                patch("ludos.installer._create_iso"),
            ):
                bootc_installer(manifest, str(oci_dir), output=root / "out", scratch=True)

        self.assertEqual(seen, [("localhost/installer:test", True)])
        self.assertEqual(build_image.call_args.args[1], "sha256:" + "d" * 64)

    def test_installer_image_ref_uses_installer_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))

            self.assertEqual(
                _installer_image_ref(ctx),
                f"localhost/installer:{_safe_ref_name(ctx.ref)}",
            )
            self.assertEqual(_installer_latest_image_ref(), "localhost/installer:latest")

    def test_installer_containerfile_runs_build_steps_in_image(self) -> None:
        containerfile = _installer_containerfile(
            "sha256:abc",
            has_files=True,
            build_script="echo installer\n",
        )

        self.assertIn("FROM sha256:abc", containerfile)
        self.assertIn("COPY files/ /files/", containerfile)
        self.assertIn("RUN /bin/sh -ex <<'LUDOS_INSTALLER_BUILD'", containerfile)
        self.assertIn("echo installer", containerfile)
        self.assertIn("rm -rf /files", containerfile)
        self.assertNotIn("LUDOS_INSTALLER_OSTREE", containerfile)

    def test_installer_containerfile_adds_ostree_step_before_build(self) -> None:
        containerfile = _installer_containerfile(
            "sha256:abc",
            has_files=True,
            build_script="echo installer\n",
            ostree=True,
        )

        self.assertIn("ARG LUDOS_INSTALLER_SOURCE_IMAGE", containerfile)
        self.assertIn("bootc internals ostree-ext container image pull", containerfile)
        self.assertIn("--ostree-digestfile", containerfile)
        self.assertIn('ostree --repo="${repo}" refs --create=os "${commit}"', containerfile)
        self.assertIn("ostree-unverified-image:containers-storage:", containerfile)
        self.assertNotIn("container unencapsulate", containerfile)
        self.assertNotIn("--write-ref os", containerfile)
        self.assertLess(
            containerfile.index("LUDOS_INSTALLER_OSTREE"),
            containerfile.index("LUDOS_INSTALLER_BUILD"),
        )

    def test_pull_source_image_imports_to_storage_and_returns_image_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))

            def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
                if command[:3] == ["podman", "pull", "--quiet"]:
                    return subprocess.CompletedProcess(command, 0, stdout="sha256:" + "a" * 64 + "\n")
                if command[:3] == ["podman", "image", "inspect"]:
                    return subprocess.CompletedProcess(command, 0, stdout="sha256:" + "b" * 64 + "\n")
                raise AssertionError(command)

            with patch("ludos.installer._run_host", side_effect=run) as run_mock:
                image_id = _pull_source_image(ctx, "oci:/cache/image:latest")

        self.assertEqual(image_id, "sha256:" + "b" * 64)
        self.assertEqual(run_mock.call_args_list[0].args[0], ["podman", "pull", "--quiet", "oci:/cache/image:latest"])

    def test_build_installer_image_builds_containerfile_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))
            ctx.build_context.mkdir(parents=True)

            with patch("ludos.installer._run_host") as run:
                image = _build_installer_image(ctx, "sha256:base")

            containerfile = ctx.build_context / "Containerfile"
            self.assertTrue(containerfile.is_file())

        self.assertEqual(image, f"localhost/installer:{_safe_ref_name(ctx.ref)}")
        run.assert_called_once_with(
            [
                "podman",
                "build",
                "--tag",
                image,
                "--tag",
                "localhost/installer:latest",
                "--file",
                str(containerfile),
                str(ctx.build_context),
            ]
        )

    def test_build_installer_image_ostree_mounts_storage_and_passes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), installer=InstallerConfig(ostree=True))
            ctx.build_context.mkdir(parents=True)
            graph_root = Path(tmp) / "graph"
            run_root = Path(tmp) / "run"

            def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
                if command[:3] == ["podman", "info", "--format"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=f'{{"graphRoot": "{graph_root}", "runRoot": "{run_root}"}}',
                    )
                return subprocess.CompletedProcess(command, 0, stdout="")

            with patch("ludos.installer._run_host", side_effect=run) as run_mock:
                image = _build_installer_image(ctx, "sha256:" + "c" * 64)

            containerfile_text = (ctx.build_context / "Containerfile").read_text(
                encoding="utf-8"
            )
            build_command = run_mock.call_args_list[-1].args[0]

        self.assertEqual(image, f"localhost/installer:{_safe_ref_name(ctx.ref)}")
        self.assertIn("FROM sha256:" + "c" * 64, containerfile_text)
        self.assertIn("--cap-add", build_command)
        self.assertIn("SYS_ADMIN", build_command)
        self.assertIn("--build-arg", build_command)
        self.assertIn("LUDOS_INSTALLER_SOURCE_IMAGE=" + "c" * 64, build_command)
        self.assertNotIn("LUDOS_INSTALLER_SOURCE_IMAGE=sha256:" + "c" * 64, build_command)
        self.assertIn(f"{graph_root}:/var/lib/containers/storage", build_command)
        self.assertIn(f"{run_root}:/run/containers/storage", build_command)

    def test_container_name_is_deterministic_for_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))

            self.assertEqual(
                _container_name(ctx),
                f"installer-{_safe_ref_name(ctx.ref)}",
            )

    def test_kernel_asset_script_reports_only_kernel_and_initramfs(self) -> None:
        script = _kernel_asset_script()

        self.assertIn("find /usr/lib/modules", script)
        self.assertIn("-name vmlinuz", script)
        self.assertIn('initramfs="${kernel%/vmlinuz}/initramfs.img"', script)
        self.assertIn('printf "%s\\n%s\\n" "$kernel" "$initramfs"', script)

    def test_efi_asset_script_requires_shim_and_searches_ostree_boot(self) -> None:
        script = _efi_asset_script()

        self.assertIn("/usr/lib/efi", script)
        self.assertIn("/usr/lib/ostree-boot", script)
        self.assertIn('shimx64*.efi', script)
        self.assertIn("install shim-x64", script)
        self.assertIn('mmx64*.efi', script)
        self.assertIn("MokManager EFI file", script)
        self.assertIn('grubx64.efi', script)
        self.assertIn('printf "%s\\n%s\\n%s\\n" "$shim" "$mok_manager" "$grub"', script)

    def test_container_ludos_efi_asset_dir_checks_standard_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))

            with patch(
                "ludos.installer._run_host",
                return_value=subprocess.CompletedProcess(["podman"], 0),
            ) as run:
                self.assertTrue(_container_ludos_efi_asset_dir(ctx, "installer:image"))

        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["podman", "run", "--rm", "--entrypoint", "/bin/sh"])
        self.assertIn("installer:image", command)
        self.assertIn(f"test -d {LUDOS_EFI_ASSET_DIR}", command)
        self.assertFalse(run.call_args.kwargs["check"])

    def test_kernel_and_initramfs_use_copied_boot_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            boot_assets = Path(tmp)
            (boot_assets / "vmlinuz").write_text("kernel", encoding="utf-8")
            (boot_assets / "initramfs.img").write_text("initramfs", encoding="utf-8")

            self.assertEqual(
                _kernel_and_initramfs(boot_assets),
                (boot_assets / "vmlinuz", boot_assets / "initramfs.img"),
            )

    def test_copy_ludos_efi_payload_merges_payload_into_efi_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            boot_assets = root / "boot-assets"
            payload = boot_assets / LUDOS_EFI_BOOT_ASSETS
            payload.mkdir(parents=True)
            (payload / "anatase.der").write_text("cert", encoding="utf-8")
            (payload / "EFI/anatase").mkdir(parents=True)
            (payload / "EFI/anatase/tool.efi").write_text("tool", encoding="utf-8")
            efi_root = root / "efi-tree"
            (efi_root / "EFI/BOOT").mkdir(parents=True)
            (efi_root / "EFI/BOOT/BOOTX64.EFI").write_text("shim", encoding="utf-8")

            _copy_ludos_efi_payload(boot_assets, efi_root)

            self.assertEqual((efi_root / "anatase.der").read_text(encoding="utf-8"), "cert")
            self.assertEqual(
                (efi_root / "EFI/anatase/tool.efi").read_text(encoding="utf-8"),
                "tool",
            )
            self.assertEqual(
                (efi_root / "EFI/BOOT/BOOTX64.EFI").read_text(encoding="utf-8"),
                "shim",
            )

    def test_tool_command_uses_orchestrator_not_payload_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), orchestrator="localhost/orchestrator:test")

            command = _tool_command(ctx, ["mkfs.erofs", "--help"])

        self.assertIn("localhost/orchestrator:test", command)
        self.assertNotIn("cache/oci/anatase-f44-x86_64", command)
        self.assertEqual(command[-2:], ["mkfs.erofs", "--help"])

    def test_tool_command_can_keep_stdin_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), orchestrator="localhost/orchestrator:test")

            command = _tool_command(ctx, ["mkfs.erofs"], stdin=True)

        self.assertIn("--interactive", command)
        self.assertLess(command.index("--interactive"), command.index("localhost/orchestrator:test"))

    def test_tool_path_maps_output_paths_for_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))

            self.assertEqual(
                _tool_path(ctx, ctx.output_dir / "root.erofs"),
                Path(CONTAINER_WORKDIR) / "root.erofs",
            )

    def test_grub_config_is_silent_and_generic(self) -> None:
        config = _grub_config("ANATASE_ISO")

        self.assertIn("set timeout=0", config)
        self.assertIn("set timeout_style=hidden", config)
        self.assertIn('menuentry "Installer"', config)
        self.assertIn("root=live:CDLABEL=ANATASE_ISO", config)
        self.assertIn("selinux=0", config)
        self.assertIn('if [ "$grub_platform" = "efi" ]; then', config)
        self.assertIn("linuxefi /vmlinuz", config)
        self.assertIn("linux /vmlinuz", config)
        self.assertNotIn("terminal_output gfxterm", config)

    def test_context_labels_are_derived_from_manifest_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp))

            self.assertEqual(ctx.iso_label, "ANATASE_ISO")
            self.assertEqual(ctx.rootfs_label, "ANATASE_ROOT")
            self.assertEqual(ctx.efi_label, "ANATASE_KEY")
            self.assertEqual(ctx.menuentry, "Anatase Installer")

    def test_grub_config_accepts_named_menuentry(self) -> None:
        config = _grub_config("ANATASE_ISO", 'Anatase "Installer"')

        self.assertIn('menuentry "Anatase \\"Installer\\""', config)

    def test_grub_config_supports_platform_specific_boot_commands(self) -> None:
        efi_config = _grub_config("ANATASE_ISO", platform="efi")
        bios_config = _grub_config("ANATASE_ISO", platform="bios")

        self.assertIn("linuxefi /vmlinuz", efi_config)
        self.assertNotIn("linux /vmlinuz", efi_config)
        self.assertIn("linux /vmlinuz", bios_config)
        self.assertNotIn("linuxefi /vmlinuz", bios_config)

    def test_label_base_sanitizes_manifest_name(self) -> None:
        self.assertEqual(_label_base("My OS"), "MY_OS")
        self.assertEqual(_label_base(" My++OS "), "MY_OS")

    def test_fat_label_for_manifest_fits_vfat_limit(self) -> None:
        manifest = _manifest()
        long_manifest = Manifest(
            version=manifest.version,
            env=manifest.env,
            releasever=manifest.releasever,
            distro=manifest.distro,
            orchestrator=manifest.orchestrator,
            bootstrap=manifest.bootstrap,
            repos=manifest.repos,
            cards=manifest.cards,
            name="Anatase Workstation",
            installer=manifest.installer,
        )

        self.assertEqual(_fat_label_for_manifest(manifest, "KEY"), "ANATASE_KEY")
        self.assertEqual(_fat_label_for_manifest(long_manifest, "KEY"), "ANATASE_KEY")

    def test_mkfs_erofs_tar_command_reads_tar_from_stdin(self) -> None:
        self.assertEqual(
            _mkfs_erofs_tar_command("ANATASE_ROOT", Path("root.erofs"), workers=8),
            [
                "mkfs.erofs",
                "-L",
                "ANATASE_ROOT",
                "-z",
                "zstd,3",
                "-E",
                "ztailpacking,fragments",
                "--workers=8",
                "--tar=f",
                "root.erofs",
                "/proc/self/fd/0",
            ],
        )

    def test_mkfs_erofs_tar_command_supports_scratch_profile(self) -> None:
        self.assertEqual(
            _mkfs_erofs_tar_command(
                "ANATASE_ROOT",
                Path("root.erofs"),
                profile=_erofs_profile(scratch=True),
                workers=8,
            ),
            [
                "mkfs.erofs",
                "-L",
                "ANATASE_ROOT",
                "-z",
                "lz4",
                "--workers=8",
                "--tar=f",
                "root.erofs",
                "/proc/self/fd/0",
            ],
        )

    def test_erofs_worker_count_falls_back_to_cpu_count(self) -> None:
        with (
            patch("ludos.installer.os.sched_getaffinity", side_effect=AttributeError),
            patch("ludos.installer.os.cpu_count", return_value=0),
        ):
            self.assertEqual(_erofs_worker_count(), 1)

    def test_mcopy_tree_script_expands_source_glob_in_shell(self) -> None:
        self.assertEqual(
            _mcopy_tree_script(Path("/work/efi.img"), Path("/work/efi-tree")),
            "mcopy -s -i /work/efi.img /work/efi-tree/* ::/",
        )

    def test_efi_image_size_uses_minimum_for_small_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            efi_tree = Path(tmp)
            (efi_tree / "BOOTX64.EFI").write_bytes(b"x")

            self.assertEqual(_efi_image_size_kib(efi_tree), 65536)

    def test_efi_image_size_uses_ten_percent_or_32m_overhead(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            efi_tree = Path(tmp)
            (efi_tree / "payload").write_bytes(b"\0" * (200 * 1024 * 1024))

            self.assertEqual(_efi_image_size_kib(efi_tree), 237568)

    def test_stream_root_erofs_reports_erofs_failure_before_export_sigpipe(self) -> None:
        class ExportProcess:
            stdout = io.BytesIO(b"tar")
            stderr = io.BytesIO(b"")

            def wait(self) -> int:
                return -13

        class ErofsProcess:
            returncode = 1

            def communicate(self) -> tuple[bytes, bytes]:
                return b"", b"mkfs failed"

        processes = [ExportProcess(), ErofsProcess()]

        with self.assertRaisesRegex(ConfigError, "mkfs failed"):
            with patch("ludos.installer.subprocess.Popen", side_effect=processes):
                _stream_root_erofs(_context(Path("/tmp")), "container")

    def test_copy_live_iso_payload_places_erofs_and_efi_image_in_standard_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _context(root)
            ctx.output_dir.mkdir(parents=True)
            ctx.root_erofs.write_text("erofs", encoding="utf-8")
            ctx.efi_img.write_text("efi", encoding="utf-8")
            ctx.boot_assets.mkdir()
            (ctx.boot_assets / "shimx64.efi").write_text("shim", encoding="utf-8")
            (ctx.boot_assets / "mmx64.efi").write_text("mok", encoding="utf-8")
            (ctx.boot_assets / "grubx64.efi").write_text("grub", encoding="utf-8")
            (ctx.boot_assets / LUDOS_EFI_BOOT_ASSETS).mkdir()
            (ctx.boot_assets / LUDOS_EFI_BOOT_ASSETS / "anatase.der").write_text(
                "cert",
                encoding="utf-8",
            )
            (ctx.boot_assets / LUDOS_EFI_BOOT_ASSETS / "EFI/BOOT").mkdir(parents=True)
            (ctx.boot_assets / LUDOS_EFI_BOOT_ASSETS / "EFI/BOOT/grubx64.efi").write_text(
                "custom",
                encoding="utf-8",
            )
            iso_tree = ctx.output_dir / "iso-tree"
            iso_tree.mkdir()

            _copy_live_iso_payload(ctx, iso_tree)

            self.assertEqual((iso_tree / LIVE_ROOT_IMAGE).read_text(encoding="utf-8"), "erofs")
            self.assertEqual((iso_tree / EFI_BOOT_IMAGE).read_text(encoding="utf-8"), "efi")
            self.assertEqual((iso_tree / "EFI/BOOT/BOOTX64.EFI").read_text(encoding="utf-8"), "shim")
            self.assertEqual((iso_tree / "EFI/BOOT/mmx64.efi").read_text(encoding="utf-8"), "mok")
            self.assertEqual((iso_tree / "EFI/BOOT/grubx64.efi").read_text(encoding="utf-8"), "grub")
            self.assertEqual((iso_tree / "anatase.der").read_text(encoding="utf-8"), "cert")
            self.assertIn(
                "root=live:CDLABEL=ANATASE_ISO",
                (iso_tree / "EFI/BOOT/grub.cfg").read_text(encoding="utf-8"),
            )

    def test_xorriso_command_uses_standard_live_iso_boot_entries(self) -> None:
        command = _xorriso_command(
            Path("installer.iso"),
            Path("."),
            bios_mbr=Path("boot_hybrid.img"),
        )

        self.assertEqual(command[:3], ["xorriso", "-as", "mkisofs"])
        self.assertEqual(command[command.index("-V") + 1], "ANATASE_ISO")
        self.assertEqual(command[command.index("-o") + 1], "installer.iso")
        self.assertEqual(command[-1], ".")
        self.assertIn("-r", command)
        self.assertIn("-J", command)
        self.assertIn("-joliet-long", command)
        self.assertEqual(command[command.index("-iso-level") + 1], "3")
        self.assertIn("--grub2-mbr", command)
        self.assertIn("boot_hybrid.img", command)
        self.assertIn("-b", command)
        self.assertEqual(command[command.index("-b") + 1], str(BIOS_ELTORITO_IMAGE))
        self.assertIn("-boot-info-table", command)
        self.assertIn("--grub2-boot-info", command)
        self.assertIn("-eltorito-alt-boot", command)
        self.assertIn("-e", command)
        self.assertEqual(command[command.index("-e") + 1], str(EFI_BOOT_IMAGE))
        self.assertIn("-isohybrid-gpt-basdat", command)
        self.assertNotIn("-append_partition", command)
        self.assertNotIn("-boot_image", command)

    def test_grub_mkimage_command_builds_i386_pc_core(self) -> None:
        command = _grub_mkimage_command(Path("core.img"))

        self.assertEqual(command[:5], ["grub2-mkimage", "-O", "i386-pc", "-o", "core.img"])
        self.assertIn("-p", command)
        self.assertIn("/boot/grub", command)
        self.assertIn("biosdisk", command)
        self.assertIn("linux", command)

    def test_installer_build_script_removes_files(self) -> None:
        self.assertEqual(
            _installer_build_script("echo hi\n"),
            "echo hi\nrm -rf /files\n",
        )

    def test_copy_installer_files_places_files_under_rootfs_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source.txt").write_text("hello", encoding="utf-8")
            rootfs = root / "rootfs"
            rootfs.mkdir()

            _copy_installer_files(
                InstallerConfig(files=("target/source.txt::source.txt",)),
                root,
                rootfs,
            )

            self.assertEqual(
                (rootfs / "files/target/source.txt").read_text(encoding="utf-8"),
                "hello",
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import hashlib
import os
import re
import signal
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .bootc import DEFAULT_CACHE_DIR, _manifest_artifact_path, _safe_oci_name
from .build import (
    HASH_LENGTH,
    FileRef,
    _cache_name,
    _image_exists,
    _local_image,
    _parse_file_ref,
    _substitute_variables,
    _tag_image,
    _validate_relative_file_path,
)
from .logging import log, stream
from .model import ConfigError, InstallerConfig, InstallerFlatpaksConfig, Manifest


DEFAULT_LABEL_BASE = "LUDOS"
FAT_LABEL_MAX = 11
CONTAINER_WORKDIR = "/ludos/installer"
# From reddit:
# zstd -b1 -e22 enwik8
#  1#enwik8            : 100000000 ->  40667563 (x2.459),  363.0 MB/s, 1312.6 MB/s
#  2#enwik8            : 100000000 ->  37332782 (x2.679),  274.6 MB/s, 1191.7 MB/s
#  3#enwik8            : 100000000 ->  35461800 (x2.820),  220.2 MB/s, 1095.3 MB/s # sane default for scatch
#  4#enwik8            : 100000000 ->  34754903 (x2.877),  187.3 MB/s  1058.0 MB/s
#  5#enwik8            : 100000000 ->  33663781 (x2.971),  100.1 MB/s, 1063.0 MB/s
#  6#enwik8            : 100000000 ->  32571332 (x3.070),   76.0 MB/s, 1151.3 MB/s
#  7#enwik8            : 100000000 ->  31933763 (x3.131),   69.5 MB/s, 1057.9 MB/s
#  8#enwik8            : 100000000 ->  31542878 (x3.170),   55.5 MB/s, 1100.0 MB/s
#  9#enwik8            : 100000000 ->  31034682 (x3.222),   51.0 MB/s, 1152.9 MB/s 
# 10#enwik8            : 100000000 ->  30619017 (x3.266),   37.6 MB/s, 1113.6 MB/s
# 11#enwik8            : 100000000 ->  30416549 (x3.288),   22.3 MB/s, 1107.4 MB/s
# 12#enwik8            : 100000000 ->  30338917 (x3.296),   18.7 MB/s,  839.1 MB/s
# 13#enwik8            : 100000000 ->  29972260 (x3.336),   7.06 MB/s, 1128.1 MB/s
# 14#enwik8            : 100000000 ->  29795318 (x3.356),   5.36 MB/s, 1108.0 MB/s
# 15#enwik8            : 100000000 ->  29436415 (x3.397),   4.02 MB/s, 1160.5 MB/s
# 16#enwik8            : 100000000 ->  28437242 (x3.517),   3.90 MB/s, 1149.6 MB/s
# 17#enwik8            : 100000000 ->  27710189 (x3.609),   3.07 MB/s, 1150.2 MB/s
# 18#enwik8            : 100000000 ->  27320373 (x3.660),   2.62 MB/s, 1151.6 MB/s
# 19#enwik8            : 100000000 ->  26952099 (x3.710),   2.21 MB/s,  766.3 MB/s
# 20#enwik8            : 100000000 ->  25983520 (x3.849),   1.79 MB/s,  975.8 MB/s
# 21#enwik8            : 100000000 ->  25535719 (x3.916),   1.62 MB/s,  883.5 MB/s
# 22#enwik8            : 100000000 ->  25333641 (x3.947),   1.46 MB/s,  893.1 MB/s
# Level 9 saves around 100mb:
# - 3) 4.8G 1m19s (browser reports 5.14 GB)
# - 9) 4.7G 2m33s (browser reports 5.02 GB)
EROFS_COMPRESSION = "zstd,9"
EROFS_COMPRESSION_SCRATCH = "zstd,3"
EROFS_FEATURES = "ztailpacking,fragments"
BIOS_GRUB_DIR = Path("boot/grub/i386-pc")
BIOS_ELTORITO_IMAGE = BIOS_GRUB_DIR / "eltorito.img"
EFI_BOOT_IMAGE = Path("images/efiboot.img")
LIVE_ROOT_IMAGE = Path("LiveOS/squashfs.img")
LUDOS_EFI_ASSET_DIR = Path("/usr/lib/ludos/efi")
LUDOS_EFI_BOOT_ASSETS = Path("ludos-efi")
BIOS_GRUB_MODULES = (
    "biosdisk",
    "iso9660",
    "part_msdos",
    "part_gpt",
    "normal",
    "configfile",
    "linux",
    "test",
    "search",
    "search_label",
)


@dataclass(frozen=True)
class ErofsProfile:
    name: str
    compression: str
    pcluster_size: str | None = None
    features: str | None = None


EROFS_DEFAULT_PROFILE = ErofsProfile(
    name="default",
    compression=EROFS_COMPRESSION,
    features=EROFS_FEATURES,
)
EROFS_SCRATCH_PROFILE = ErofsProfile(
    name="default",
    compression=EROFS_COMPRESSION_SCRATCH,
    features=EROFS_FEATURES,
)


@dataclass(frozen=True)
class InstallerContext:
    manifest: Manifest
    manifest_path: Path
    ref: str
    output_dir: Path
    orchestrator: str
    scratch: bool = False
    force: bool = False
    podman: str = "podman"

    @property
    def boot_assets(self) -> Path:
        return self.output_dir / "boot-assets"

    @property
    def build_context(self) -> Path:
        return self.output_dir / "container"

    @property
    def efi_img(self) -> Path:
        return self.output_dir / "efi.img"

    @property
    def root_erofs(self) -> Path:
        return self.output_dir / "root.erofs"

    @property
    def installer_iso(self) -> Path:
        return self.output_dir / "installer.iso"

    @property
    def iso_label(self) -> str:
        return _label_for_manifest(self.manifest, "ISO")

    @property
    def rootfs_label(self) -> str:
        return _label_for_manifest(self.manifest, "ROOT")

    @property
    def efi_label(self) -> str:
        return _fat_label_for_manifest(self.manifest, "KEY")

    @property
    def menuentry(self) -> str:
        name = self.manifest.name.strip()
        return f"{name} Installer" if name else "Installer"


def bootc_installer(
    manifest_path: Path,
    ref: str,
    *,
    output: Path | None = None,
    cache_dir: Path | None = None,
    orchestrator: str | None = None,
    scratch: bool = False,
    force: bool = False,
) -> int:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = Manifest.from_file(manifest_path)
    output_dir = _resolve_output_dir(
        manifest_path,
        ref,
        output,
        cache_dir,
        manifest=manifest,
    )
    podman = shutil.which("podman")
    if podman is None:
        raise ConfigError("podman must be installed to create an installer ISO")

    prepare_ctx = InstallerContext(
        manifest=manifest,
        manifest_path=manifest_path,
        ref=ref,
        output_dir=output_dir,
        orchestrator=orchestrator or ref,
        scratch=scratch,
        force=force,
        podman=podman,
    )

    log(f"Preparing installer output directory: {prepare_ctx.output_dir}")
    _prepare_output_dir(prepare_ctx.output_dir)
    source_ref = _source_image_ref(ref)
    log(f"Importing installer source image into local storage: {source_ref}")
    source_image = _pull_source_image(prepare_ctx, source_ref)
    log("Preparing installer image build context")
    _prepare_installer_build_context(prepare_ctx)
    log(f"Building installer image from {source_image}")
    installer_image = _build_installer_image(prepare_ctx, source_image)
    ctx = InstallerContext(
        manifest=manifest,
        manifest_path=manifest_path,
        ref=ref,
        output_dir=output_dir,
        orchestrator=orchestrator or installer_image,
        scratch=scratch,
        force=force,
        podman=podman,
    )
    log(f"Using installer tooling image: {ctx.orchestrator}")
    log("Creating EROFS root image")
    _create_root_erofs(ctx, source_ref, installer_image)
    log("Creating UEFI boot image")
    _create_efi_image(ctx)
    log("Creating hybrid installer ISO")
    _create_iso(ctx)
    log(f"Created installer ISO: {ctx.installer_iso}")
    return 0


def _resolve_output_dir(
    manifest_path: Path,
    ref: str,
    output: Path | None,
    cache_dir: Path | None,
    *,
    manifest: Manifest | None = None,
) -> Path:
    if output is not None:
        return output.expanduser().resolve()
    cache_root = (
        cache_dir.expanduser().resolve()
        if cache_dir is not None
        else (manifest_path.resolve().parent / DEFAULT_CACHE_DIR).resolve()
    )
    return _manifest_artifact_path(manifest_path, cache_root / "iso", manifest=manifest)


def _safe_ref_name(ref: str) -> str:
    value = ref
    if value.startswith("oci:"):
        value = value.removeprefix("oci:")
        value = value.removesuffix(":latest")
    return _safe_oci_name(value)


def _prepare_output_dir(output_dir: Path) -> None:
    if output_dir.is_symlink() or output_dir.is_file():
        output_dir.unlink()
    elif output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)


def _create_root_erofs(ctx: InstallerContext, source_ref: str, run_ref: str) -> None:
    container = _container_name(ctx)

    log(f"Preparing installer rootfs container from {source_ref}")
    _run_host([ctx.podman, "rm", "-f", container], check=False)
    result = _run_host(
        [ctx.podman, "create", "--name", container, run_ref, "/bin/true"],
        capture=True,
    )
    container_id = result.stdout.strip() or container
    try:
        _copy_boot_assets(ctx, container_id, run_ref)
        _stream_root_erofs(ctx, container_id)
    finally:
        _run_host([ctx.podman, "rm", "-f", container_id], check=False)


def _source_image_ref(ref: str) -> str:
    path = Path(ref).expanduser()
    if path.exists():
        if not path.is_dir():
            raise ConfigError(f"installer source image path is not a directory: {path}")
        return f"oci:{path.resolve()}:latest"
    return ref


def _pull_source_image(ctx: InstallerContext, source_ref: str) -> str:
    result = _run_host(
        [ctx.podman, "pull", "--quiet", source_ref],
        capture=True,
    )
    image_ref = _pulled_image_ref(result.stdout, source_ref)
    inspect = _run_host(
        [ctx.podman, "image", "inspect", image_ref, "--format", "{{.Id}}"],
        capture=True,
    )
    return _normalize_image_id(inspect.stdout.strip())


def _pulled_image_ref(output: str, fallback: str) -> str:
    line = _last_output_line(output)
    if not line:
        return fallback
    if line.startswith("Loaded image: "):
        return line.removeprefix("Loaded image: ").strip()
    return line


def _normalize_image_id(value: str) -> str:
    image_id = value.strip()
    if image_id.startswith("sha256:"):
        image_id = image_id.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", image_id):
        raise ConfigError(f"podman image inspect returned an invalid image ID: {value}")
    return f"sha256:{image_id.lower()}"


def _prepare_installer_build_context(ctx: InstallerContext) -> None:
    if ctx.build_context.exists():
        shutil.rmtree(ctx.build_context)
    ctx.build_context.mkdir(parents=True)
    _copy_installer_files(ctx.manifest.installer, ctx.manifest_path.parent, ctx.build_context)


def _build_installer_image(ctx: InstallerContext, base_ref: str) -> str:
    image = _installer_image_ref(ctx, base_ref)
    latest_image = _installer_latest_image_ref(ctx)
    if not ctx.force and _image_exists(ctx.podman, image):
        log(f"Reusing installer image: {image}")
        _tag_image(ctx.podman, image, latest_image)
        return image

    containerfile = ctx.build_context / "Containerfile"
    has_files = (ctx.build_context / "files").is_dir()
    containerfile.write_text(
        _installer_containerfile(
            base_ref,
            has_files,
            ctx.manifest.installer.build,
            ostree=ctx.manifest.installer.ostree,
            flatpak_groups=ctx.manifest.installer.flatpaks,
        ),
        encoding="utf-8",
    )
    log(f"Committing installer image: {image}")
    build_options = []
    if ctx.manifest.installer.ostree:
        build_options.extend(
            [
                "--cap-add",
                "SYS_ADMIN",
                "--build-arg",
                f"LUDOS_INSTALLER_SOURCE_IMAGE={_containers_storage_image_id(base_ref)}",
                *_podman_storage_volume_options(ctx),
            ]
        )
    _run_host(
        [
            ctx.podman,
            "build",
            *build_options,
            "--tag",
            image,
            "--tag",
            latest_image,
            "--file",
            str(containerfile),
            str(ctx.build_context),
        ]
    )
    return image


def _installer_image_ref(ctx: InstallerContext, source_image: str = "") -> str:
    image, distro, local_prefix = _installer_manifest_identity(ctx)
    installer_hash = _installer_hash(ctx, source_image)
    return _local_image(local_prefix, "installers", f"{distro}-{image}-{installer_hash}")


def _installer_latest_image_ref(ctx: InstallerContext | None = None) -> str:
    if ctx is None:
        return "installers:latest"
    image, _distro, local_prefix = _installer_manifest_identity(ctx)
    return _local_image(local_prefix, "installers", image)


def _installer_manifest_identity(ctx: InstallerContext) -> tuple[str, str, str]:
    manifest_env = {key: str(value) for key, value in ctx.manifest.env.items()}
    manifest_env["releasever"] = _cache_name(
        _substitute_variables(ctx.manifest.releasever, manifest_env),
        "releasever",
    )
    arch = _cache_name(
        _substitute_variables(str(manifest_env.get("arch", "")), manifest_env),
        "arch",
    )
    manifest_env["arch"] = arch
    manifest_env = {
        key: _substitute_variables(value, manifest_env)
        for key, value in manifest_env.items()
    }
    distro = _cache_name(
        _substitute_variables(ctx.manifest.distro, manifest_env),
        "distro",
    )
    image = _cache_name(ctx.manifest_path.resolve().stem, "image")
    local_prefix = ctx.manifest.local_prefix
    if "/" in local_prefix or ":" in local_prefix:
        raise ConfigError(f"invalid local_prefix '{local_prefix}'")
    return image, distro, local_prefix


def _installer_hash(ctx: InstallerContext, source_image: str) -> str:
    payload = {
        "source_image": source_image,
        "scratch": ctx.scratch,
        "installer": {
            "ostree": ctx.manifest.installer.ostree,
            "build": ctx.manifest.installer.build,
            "files": _installer_file_hash_inputs(ctx),
            "flatpaks": tuple(
                (
                    "remote",
                    group.repo,
                    group.nodeps,
                    group.preinstall,
                    group.installer,
                )
                for group in ctx.manifest.installer.flatpaks
            ),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def _installer_file_hash_inputs(ctx: InstallerContext) -> tuple[tuple[str, str, str], ...]:
    entries = []
    manifest_root = ctx.manifest_path.parent
    for value in ctx.manifest.installer.files:
        file_ref = _parse_file_ref(value)
        if _is_remote_file_ref(file_ref.source):
            entries.append((file_ref.target, file_ref.source, file_ref.source))
            continue
        target_relpath = _validate_relative_file_path(
            file_ref.target,
            ctx.manifest_path,
            "installer files destination",
        )
        source_relpath = _validate_relative_file_path(
            file_ref.source,
            ctx.manifest_path,
            "installer files source",
        )
        source_path = (manifest_root / source_relpath).resolve()
        try:
            source_path.relative_to(manifest_root.resolve())
        except ValueError as exc:
            raise ConfigError(
                f"{manifest_root}: installer files entry '{file_ref.original}' escapes the manifest directory"
            ) from exc
        if not source_path.is_file():
            raise ConfigError(
                f"{manifest_root}: installer files entry '{file_ref.original}' is missing"
            )
        entries.append((target_relpath.as_posix(), file_ref.source, _hash_path(source_path)))
    return tuple(entries)


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _installer_containerfile(
    base_ref: str,
    has_files: bool,
    build_script: str,
    *,
    ostree: bool = False,
    flatpak_groups: tuple[InstallerFlatpaksConfig, ...] = tuple(),
) -> str:
    if "\n" in base_ref:
        raise ConfigError("installer base image ref must not contain newlines")
    lines = [f"FROM {base_ref}"]
    if ostree:
        lines.extend(
            [
                "",
                "ARG LUDOS_INSTALLER_SOURCE_IMAGE",
                "RUN /bin/sh -ex <<'LUDOS_INSTALLER_OSTREE'",
                _installer_ostree_script().rstrip(),
                "LUDOS_INSTALLER_OSTREE",
            ]
        )
    if any(group.all for group in flatpak_groups):
        lines.append("")
        lines.extend(_installer_flatpak_lines(flatpak_groups))
    if has_files:
        lines.append("COPY files/ /files/")
    lines.extend(
        [
            "",
            "RUN /bin/sh -ex <<'LUDOS_INSTALLER_BUILD'",
            _installer_build_script(build_script).rstrip(),
            "LUDOS_INSTALLER_BUILD",
            "",
        ]
    )
    return "\n".join(lines)


def _installer_flatpak_lines(
    flatpak_groups: tuple[InstallerFlatpaksConfig, ...],
) -> list[str]:
    return [
        "RUN /bin/sh -ex <<'LUDOS_INSTALL_FLATPAKS'",
        _installer_flatpak_script(flatpak_groups).rstrip(),
        "LUDOS_INSTALL_FLATPAKS",
    ]


def _installer_flatpak_script(
    flatpak_groups: tuple[InstallerFlatpaksConfig, ...],
) -> str:
    lines = [
        "mkdir -p /var/lib/flatpak",
        "flatpak_arch=$(uname -m)",
        *_flatpak_phase_install_lines(flatpak_groups, "preinstall"),
        "flatpak update --system --appstream -y --noninteractive",
        "rm -rf /var/lib/flatpak-installer",
        "cp -alT /var/lib/flatpak /var/lib/flatpak-installer",
        *_flatpak_phase_install_lines(flatpak_groups, "installer"),
        "flatpak update --system --appstream -y --noninteractive",
    ]
    return "\n".join(lines) + "\n"


def _flatpak_phase_install_lines(
    flatpak_groups: tuple[InstallerFlatpaksConfig, ...],
    phase: str,
) -> list[str]:
    grouped: dict[tuple[str, bool], list[str]] = {}
    for group in flatpak_groups:
        key = (group.repo, group.nodeps)
        grouped.setdefault(key, []).extend(getattr(group, phase))
    return [
        line
        for (repo, nodeps), flatpaks in grouped.items()
        for line in _flatpak_install_lines(repo, tuple(dict.fromkeys(flatpaks)), nodeps=nodeps)
    ]


def _flatpak_install_lines(
    repo: str,
    flatpaks: tuple[str, ...],
    *,
    nodeps: bool = False,
) -> list[str]:
    if not flatpaks:
        return []
    options = ["--system", "-y", "--noninteractive"]
    if nodeps:
        options.append("--no-deps")
    lines = [
        f"flatpak install {' '.join(options)} {shlex.quote(repo)} \\",
    ]
    for index, ref in enumerate(flatpaks):
        suffix = " \\" if index < len(flatpaks) - 1 else ""
        lines.append(f'    "{_shell_double_quoted(ref)}"{suffix}')
    return lines


def _shell_double_quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")


def _installer_ostree_script() -> str:
    return "\n".join(
        [
            'repo="/ostree/repo"',
            'if [ ! -d "${repo}/objects" ]; then',
            '  echo "installer OSTree repo is missing objects: ${repo}" >&2',
            "  exit 1",
            "fi",
            'if [ -z "${LUDOS_INSTALLER_SOURCE_IMAGE:-}" ]; then',
            '  echo "LUDOS_INSTALLER_SOURCE_IMAGE is not set" >&2',
            "  exit 1",
            "fi",
            'digestfile="/run/ludos-installer-os-commit"',
            'rm -f "${digestfile}"',
            "bootc internals ostree-ext container image pull \\",
            '  --ostree-digestfile "${digestfile}" \\',
            "  --quiet \\",
            '  "${repo}" \\',
            '  "ostree-unverified-image:containers-storage:${LUDOS_INSTALLER_SOURCE_IMAGE}"',
            'commit="$(cat "${digestfile}")"',
            'if [ -z "${commit}" ]; then',
            '  echo "ostree-ext did not write an installer os commit digest" >&2',
            "  exit 1",
            "fi",
            'if ostree --repo="${repo}" refs | grep -qx os; then',
            '  ostree --repo="${repo}" refs --delete os',
            "fi",
            'ostree --repo="${repo}" refs --create=os "${commit}"',
            'ostree --repo="${repo}" summary --update',
        ]
    )


def _containers_storage_image_id(image_id: str) -> str:
    value = image_id.strip()
    if value.startswith("sha256:"):
        value = value.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ConfigError(f"installer source image ID is invalid: {image_id}")
    return value


def _podman_storage_volume_options(ctx: InstallerContext) -> list[str]:
    result = _run_host(
        [ctx.podman, "info", "--format", "{{json .Store}}"],
        capture=True,
    )
    try:
        store = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConfigError("failed to parse podman storage information") from exc
    graph_root = str(store.get("graphRoot") or "").strip()
    if not graph_root:
        raise ConfigError("podman storage graphRoot is unavailable")
    run_root = str(store.get("runRoot") or "").strip()
    volumes = [
        "--volume",
        f"{graph_root}:/var/lib/containers/storage",
    ]
    if run_root:
        volumes.extend(["--volume", f"{run_root}:/run/containers/storage"])
    return volumes


def _last_output_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _container_name(ctx: InstallerContext) -> str:
    return f"installer-{_safe_ref_name(ctx.ref)}"


def _require_image(podman: str, image: str, description: str) -> None:
    result = subprocess.run([podman, "image", "exists", image], check=False)
    if result.returncode != 0:
        raise ConfigError(f"{description} is not available locally: {image}")


def _copy_boot_assets(ctx: InstallerContext, container_id: str, run_ref: str) -> None:
    if ctx.boot_assets.exists():
        shutil.rmtree(ctx.boot_assets)
    ctx.boot_assets.mkdir(parents=True)
    log("Copying installer boot assets from derived image")
    kernel, initramfs = _container_kernel_assets(ctx, run_ref)
    _run_host([ctx.podman, "cp", f"{container_id}:{kernel}", str(ctx.boot_assets / "vmlinuz")])
    _run_host(
        [
            ctx.podman,
            "cp",
            f"{container_id}:{initramfs}",
            str(ctx.boot_assets / "initramfs.img"),
        ]
    )
    shim, mok_manager, grub = _container_efi_assets(ctx, run_ref)
    _run_host([ctx.podman, "cp", f"{container_id}:{shim}", str(ctx.boot_assets / "shimx64.efi")])
    _run_host(
        [
            ctx.podman,
            "cp",
            f"{container_id}:{mok_manager}",
            str(ctx.boot_assets / "mmx64.efi"),
        ]
    )
    _run_host([ctx.podman, "cp", f"{container_id}:{grub}", str(ctx.boot_assets / "grubx64.efi")])
    _copy_container_ludos_efi_assets(ctx, container_id, run_ref)


def _copy_container_ludos_efi_assets(
    ctx: InstallerContext,
    container_id: str,
    run_ref: str,
) -> None:
    payload = ctx.boot_assets / LUDOS_EFI_BOOT_ASSETS
    if payload.exists():
        shutil.rmtree(payload)
    if not _container_ludos_efi_asset_dir(ctx, run_ref):
        return
    log(f"Copying optional EFI payload from {LUDOS_EFI_ASSET_DIR}")
    payload.mkdir(parents=True)
    _run_host(
        [
            ctx.podman,
            "cp",
            f"{container_id}:{LUDOS_EFI_ASSET_DIR}/.",
            str(payload),
        ]
    )


def _container_ludos_efi_asset_dir(ctx: InstallerContext, run_ref: str) -> bool:
    result = _run_host(
        [
            ctx.podman,
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            run_ref,
            "-ceu",
            f"test -d {shlex.quote(str(LUDOS_EFI_ASSET_DIR))}",
        ],
        check=False,
    )
    return result.returncode == 0


def _container_kernel_assets(ctx: InstallerContext, run_ref: str) -> tuple[str, str]:
    result = _run_host(
        [
            ctx.podman,
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            run_ref,
            "-ceu",
            _kernel_asset_script(),
        ],
        capture=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        raise ConfigError("installer image did not report kernel and initramfs paths")
    return lines[0], lines[1]


def _kernel_asset_script() -> str:
    return "\n".join(
        [
            'kernel=$(find /usr/lib/modules -mindepth 2 -maxdepth 2 -type f -name vmlinuz | sort | tail -n 1)',
            'if [ -z "$kernel" ]; then',
            '  echo "installer image has no kernel under /usr/lib/modules" >&2',
            "  exit 1",
            "fi",
            'initramfs="${kernel%/vmlinuz}/initramfs.img"',
            'if [ ! -f "$initramfs" ]; then',
            '  echo "installer image is missing initramfs: $initramfs" >&2',
            "  exit 1",
            "fi",
            'printf "%s\\n%s\\n" "$kernel" "$initramfs"',
        ]
    )


def _container_efi_assets(ctx: InstallerContext, run_ref: str) -> tuple[str, str, str]:
    result = _run_host(
        [
            ctx.podman,
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            run_ref,
            "-ceu",
            _efi_asset_script(),
        ],
        capture=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 3:
        raise ConfigError("installer image did not report shim, MokManager, and GRUB EFI paths")
    return lines[0], lines[1], lines[2]


def _efi_asset_script() -> str:
    return "\n".join(
        [
            "search_roots=",
            'for root in /usr/lib/efi /usr/lib/ostree-boot /boot /usr/share /usr/lib; do',
            '  if [ -d "$root" ]; then search_roots="$search_roots $root"; fi',
            "done",
            'shim=$(find $search_roots -type f \\( -iname "shimx64*.efi" -o -iname "shim.efi" \\) 2>/dev/null | sort | tail -n 1)',
            'if [ -z "$shim" ]; then',
            '  echo "installer image is missing shim EFI file; install shim-x64" >&2',
            "  exit 1",
            "fi",
            'mok_manager=$(find $search_roots -type f -iname "mmx64*.efi" 2>/dev/null | sort | tail -n 1)',
            'if [ -z "$mok_manager" ]; then',
            '  echo "installer image is missing MokManager EFI file; install shim-x64" >&2',
            "  exit 1",
            "fi",
            'grub=$(find $search_roots -type f -iname "grubx64.efi" 2>/dev/null | sort | tail -n 1)',
            'if [ -z "$grub" ]; then',
            '  echo "installer image is missing grubx64.efi" >&2',
            "  exit 1",
            "fi",
            'printf "%s\\n%s\\n%s\\n" "$shim" "$mok_manager" "$grub"',
        ]
    )


def _stream_root_erofs(ctx: InstallerContext, container_id: str) -> None:
    log(f"Streaming installer rootfs into EROFS image: {ctx.root_erofs}")
    workers = _erofs_worker_count()
    profile = _erofs_profile(ctx.scratch)
    log(
        "Using EROFS compression profile: "
        f"{profile.name}, compression={profile.compression}, "
        f"pcluster={profile.pcluster_size or 'default'}, "
        f"features={profile.features or 'none'}, workers={workers}"
    )
    export_command = [ctx.podman, "export", container_id]
    erofs_command = _tool_command(
        ctx,
        _mkfs_erofs_tar_command(
            ctx.rootfs_label,
            _tool_path(ctx, ctx.root_erofs),
            profile=profile,
            workers=workers,
        ),
        stdin=True,
    )
    export_process = subprocess.Popen(
        export_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert export_process.stdout is not None
    erofs_process = subprocess.Popen(
        erofs_command,
        stdin=export_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    export_process.stdout.close()
    erofs_stdout, erofs_stderr = erofs_process.communicate()
    export_stderr = export_process.stderr.read() if export_process.stderr else b""
    export_status = export_process.wait()

    if erofs_process.returncode != 0:
        _raise_command_error(
            erofs_command,
            erofs_process.returncode,
            erofs_stderr,
            erofs_stdout,
        )
    if export_status == -signal.SIGPIPE:
        return
    if export_status != 0:
        _raise_command_error(export_command, export_status, export_stderr, b"")


def _copy_installer_files(
    installer: InstallerConfig,
    manifest_root: Path,
    rootfs: Path,
) -> None:
    files_dir = rootfs / "files"
    if files_dir.exists():
        shutil.rmtree(files_dir)
    if not installer.files:
        log("No installer files configured")
        return
    log(f"Copying {len(installer.files)} installer file(s)")
    files_dir.mkdir(parents=True)
    for value in installer.files:
        file_ref = _parse_file_ref(value)
        _copy_installer_file(file_ref, manifest_root, files_dir)


def _copy_installer_file(file_ref: FileRef, manifest_root: Path, files_dir: Path) -> None:
    if _is_remote_file_ref(file_ref.source):
        raise ConfigError(
            f"installer files entry '{file_ref.original}' uses an unsupported remote source"
        )
    target_relpath = _validate_relative_file_path(
        file_ref.target,
        manifest_root,
        "installer files destination",
    )
    source_relpath = _validate_relative_file_path(
        file_ref.source,
        manifest_root,
        "installer files source",
    )
    source_path = (manifest_root / source_relpath).resolve()
    try:
        source_path.relative_to(manifest_root.resolve())
    except ValueError as exc:
        raise ConfigError(
            f"{manifest_root}: installer files entry '{file_ref.original}' escapes the manifest directory"
        ) from exc
    if not source_path.is_file():
        raise ConfigError(
            f"{manifest_root}: installer files entry '{file_ref.original}' is missing"
        )
    target_path = files_dir / target_relpath
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)


def _is_remote_file_ref(source: str) -> bool:
    return source.startswith(
        ("https://", "http://", "git+https://", "git+http://", "git+ssh://", "git+file://")
    )


def _installer_build_script(build_script: str) -> str:
    body = build_script.rstrip()
    if body:
        return f"{body}\nrm -rf /files\n"
    return "rm -rf /files\n"


def _create_efi_image(ctx: InstallerContext) -> None:
    efi_tree = ctx.output_dir / "efi-tree"
    if efi_tree.exists():
        shutil.rmtree(efi_tree)
    (efi_tree / "EFI/BOOT").mkdir(parents=True)

    kernel, initramfs = _kernel_and_initramfs(ctx.boot_assets)
    log(f"Adding UEFI kernel payload: {kernel.parent.name}")
    _copy_ludos_efi_payload(ctx.boot_assets, efi_tree)
    shutil.copy2(kernel, efi_tree / "vmlinuz")
    shutil.copy2(initramfs, efi_tree / "initramfs.img")
    shutil.copy2(ctx.boot_assets / "shimx64.efi", efi_tree / "EFI/BOOT/BOOTX64.EFI")
    shutil.copy2(ctx.boot_assets / "mmx64.efi", efi_tree / "EFI/BOOT/mmx64.efi")
    shutil.copy2(ctx.boot_assets / "grubx64.efi", efi_tree / "EFI/BOOT/grubx64.efi")
    (efi_tree / "EFI/BOOT/grub.cfg").write_text(
        _grub_config(ctx.iso_label, ctx.menuentry, platform="efi"),
        encoding="utf-8",
    )

    size_kib = _efi_image_size_kib(efi_tree)
    log(f"Formatting EFI system partition image: {ctx.efi_img}")
    _run(
        ctx,
        [
            "mkfs.vfat",
            "-n",
            ctx.efi_label,
            "-C",
            str(_tool_path(ctx, ctx.efi_img)),
            str(size_kib),
        ],
    )
    _run(ctx, ["mmd", "-i", str(_tool_path(ctx, ctx.efi_img)), "::/EFI", "::/EFI/BOOT"])
    _run(
        ctx,
        [
            "/bin/sh",
            "-ceu",
            _mcopy_tree_script(_tool_path(ctx, ctx.efi_img), _tool_path(ctx, efi_tree)),
        ],
    )


def _kernel_and_initramfs(boot_assets: Path) -> tuple[Path, Path]:
    kernel = boot_assets / "vmlinuz"
    if not kernel.is_file():
        raise ConfigError("installer boot assets are missing vmlinuz")
    initramfs = boot_assets / "initramfs.img"
    if not initramfs.is_file():
        raise ConfigError("installer boot assets are missing initramfs.img")
    return kernel, initramfs


def _efi_image_size_kib(efi_tree: Path) -> int:
    total = 0
    for path in efi_tree.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    overhead = max(total // 10, 32 * 1024 * 1024)
    return max(65536, (total + overhead + 1023) // 1024)


def _mcopy_tree_script(image: Path, source_tree: Path) -> str:
    return (
        "mcopy -s -i "
        f"{shlex.quote(str(image))} "
        f"{shlex.quote(str(source_tree))}/* "
        "::/"
    )


def _grub_config(
    iso_label: str,
    menuentry: str = "Installer",
    *,
    platform: str = "auto",
) -> str:
    kernel_args = (
        f"root=live:CDLABEL={iso_label} "
        "rd.live.image rd.live.overlay.overlayfs=1 selinux=0 quiet rhgb"
    )
    if platform == "efi":
        boot_lines = [
            f"    linuxefi /vmlinuz {kernel_args}",
            "    initrdefi /initramfs.img",
        ]
    elif platform == "bios":
        boot_lines = [
            f"    linux /vmlinuz {kernel_args}",
            "    initrd /initramfs.img",
        ]
    elif platform == "auto":
        boot_lines = [
            '    if [ "$grub_platform" = "efi" ]; then',
            f"        linuxefi /vmlinuz {kernel_args}",
            "        initrdefi /initramfs.img",
            "    else",
            f"        linux /vmlinuz {kernel_args}",
            "        initrd /initramfs.img",
            "    fi",
        ]
    else:
        raise ConfigError(f"unsupported GRUB platform: {platform}")
    return "\n".join(
        [
            "set timeout=0",
            "set timeout_style=hidden",
            "set pager=0",
            "",
            f'menuentry "{_grub_quote(menuentry)}" {{',
            *boot_lines,
            "}",
            "",
        ]
    )


def _grub_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _label_for_manifest(manifest: Manifest, suffix: str) -> str:
    return f"{_label_base(manifest.name)}_{suffix}"


def _fat_label_for_manifest(manifest: Manifest, suffix: str) -> str:
    suffix = _label_base(suffix)
    separator = "_" if suffix else ""
    base_len = FAT_LABEL_MAX - len(separator) - len(suffix)
    if base_len < 1:
        raise ConfigError(f"FAT label suffix is too long: {suffix}")
    base = _label_base(manifest.name)[:base_len].rstrip("_") or DEFAULT_LABEL_BASE[:base_len]
    return f"{base}{separator}{suffix}"


def _label_base(name: str) -> str:
    value = name.strip() or DEFAULT_LABEL_BASE
    value = value.upper()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^A-Z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or DEFAULT_LABEL_BASE


def _mkfs_erofs_tar_command(
    label: str,
    output: Path,
    *,
    profile: ErofsProfile = EROFS_DEFAULT_PROFILE,
    workers: int | None = None,
) -> list[str]:
    worker_count = workers if workers is not None else _erofs_worker_count()
    command = [
        "mkfs.erofs",
        "-L",
        label,
        "-z",
        profile.compression,
    ]
    if profile.pcluster_size:
        command.extend(["-C", profile.pcluster_size])
    if profile.features:
        command.extend(["-E", profile.features])
    command.extend([f"--workers={worker_count}", "--tar=f", str(output), "/proc/self/fd/0"])
    return command


def _erofs_profile(scratch: bool) -> ErofsProfile:
    return EROFS_SCRATCH_PROFILE if scratch else EROFS_DEFAULT_PROFILE


def _erofs_worker_count() -> int:
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    return max(1, cpus)


def _create_iso(ctx: InstallerContext) -> None:
    iso_tree = ctx.output_dir / "iso-tree"
    if iso_tree.exists():
        shutil.rmtree(iso_tree)
    iso_tree.mkdir()
    log("Adding live root image to ISO tree")
    _copy_live_iso_payload(ctx, iso_tree)
    log("Creating BIOS El Torito boot image")
    bios_mbr = _create_bios_boot_image(ctx, iso_tree)
    log(f"Running xorriso for hybrid ISO: {ctx.installer_iso}")
    _run(
        ctx,
        _xorriso_command(
            _tool_path(ctx, ctx.installer_iso),
            _tool_path(ctx, iso_tree),
            iso_label=ctx.iso_label,
            bios_mbr=_tool_path(ctx, bios_mbr),
        ),
    )


def _copy_live_iso_payload(ctx: InstallerContext, iso_tree: Path) -> None:
    _copy_ludos_efi_payload(ctx.boot_assets, iso_tree)

    live_root = iso_tree / LIVE_ROOT_IMAGE
    live_root.parent.mkdir(parents=True)
    shutil.copy2(ctx.root_erofs, live_root)

    efi_boot = iso_tree / EFI_BOOT_IMAGE
    efi_boot.parent.mkdir(parents=True)
    shutil.copy2(ctx.efi_img, efi_boot)

    visible_efi = iso_tree / "EFI/BOOT"
    visible_efi.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ctx.boot_assets / "shimx64.efi", visible_efi / "BOOTX64.EFI")
    shutil.copy2(ctx.boot_assets / "mmx64.efi", visible_efi / "mmx64.efi")
    shutil.copy2(ctx.boot_assets / "grubx64.efi", visible_efi / "grubx64.efi")
    (visible_efi / "grub.cfg").write_text(
        _grub_config(ctx.iso_label, ctx.menuentry, platform="efi"),
        encoding="utf-8",
    )


def _copy_ludos_efi_payload(boot_assets: Path, efi_root: Path) -> None:
    payload = boot_assets / LUDOS_EFI_BOOT_ASSETS
    if payload.is_dir():
        shutil.copytree(payload, efi_root, dirs_exist_ok=True)


def _create_bios_boot_image(ctx: InstallerContext, iso_tree: Path) -> Path:
    grub_dir = iso_tree / BIOS_GRUB_DIR
    grub_dir.mkdir(parents=True)
    kernel, initramfs = _kernel_and_initramfs(ctx.boot_assets)
    shutil.copy2(kernel, iso_tree / "vmlinuz")
    shutil.copy2(initramfs, iso_tree / "initramfs.img")
    (iso_tree / "boot/grub/grub.cfg").write_text(
        _grub_config(ctx.iso_label, ctx.menuentry, platform="bios"),
        encoding="utf-8",
    )

    cdboot = grub_dir / "cdboot.img"
    core = grub_dir / "core.img"
    eltorito = grub_dir / BIOS_ELTORITO_IMAGE.name
    mbr = ctx.output_dir / "boot_hybrid.img"

    _run(ctx, ["cp", "/usr/lib/grub/i386-pc/cdboot.img", str(_tool_path(ctx, cdboot))])
    _run(ctx, ["cp", "/usr/lib/grub/i386-pc/boot_hybrid.img", str(_tool_path(ctx, mbr))])
    _run(ctx, _grub_mkimage_command(_tool_path(ctx, core)))
    _run(
        ctx,
        [
            "/bin/sh",
            "-ceu",
            (
                "cat "
                f"{shlex.quote(str(_tool_path(ctx, cdboot)))} "
                f"{shlex.quote(str(_tool_path(ctx, core)))} "
                f"> {shlex.quote(str(_tool_path(ctx, eltorito)))}"
            ),
        ],
    )
    return mbr


def _grub_mkimage_command(output: Path) -> list[str]:
    return [
        "grub2-mkimage",
        "-O",
        "i386-pc",
        "-o",
        str(output),
        "-p",
        "/boot/grub",
        *BIOS_GRUB_MODULES,
    ]


def _xorriso_command(
    iso: Path,
    iso_tree: Path = Path("."),
    *,
    iso_label: str = "ANATASE_ISO",
    bios_mbr: Path | None = None,
    bios_boot_image: Path = BIOS_ELTORITO_IMAGE,
    efi_boot_image: Path = EFI_BOOT_IMAGE,
) -> list[str]:
    command = [
        "xorriso",
        "-as",
        "mkisofs",
        "-V",
        iso_label,
        "-r",
        "-J",
        "-joliet-long",
        "-iso-level",
        "3",
        "-o",
        str(iso),
    ]
    if bios_mbr is not None:
        command.extend(
            [
                "--grub2-mbr",
                str(bios_mbr),
            ]
        )
    command.extend(
        [
            "-b",
            str(bios_boot_image),
            "-no-emul-boot",
            "-boot-load-size",
            "4",
            "-boot-info-table",
            "--grub2-boot-info",
            "-eltorito-alt-boot",
            "-e",
            str(efi_boot_image),
            "-no-emul-boot",
            "-isohybrid-gpt-basdat",
            str(iso_tree),
        ]
    )
    return command


def _run(
    ctx: InstallerContext,
    command: list[str],
    *,
    input_text: str | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return _run_host(
        _tool_command(ctx, command),
        input_text=input_text,
        capture=capture,
    )


def _run_host(
    command: list[str],
    *,
    input_text: str | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if not capture:
        return _run_host_streamed(
            command,
            input_text=input_text,
            check=check,
        )
    result = subprocess.run(
        command,
        input=input_text,
        text=True,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        _raise_command_error(command, result.returncode, result.stderr, result.stdout)
    return result


def _run_host_streamed(
    command: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_chunks: list[str] = []
    try:
        if input_text is not None:
            assert process.stdin is not None
            process.stdin.write(input_text)
            process.stdin.close()

        assert process.stdout is not None
        for line in process.stdout:
            output_chunks.append(line)
            stream(line)
        returncode = process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()

    output = "".join(output_chunks)
    if check and returncode != 0:
        _raise_command_error(command, returncode, output, None)
    return subprocess.CompletedProcess(command, returncode, stdout=output)


def _raise_command_error(
    command: list[str],
    returncode: int | None,
    stderr: str | bytes | None,
    stdout: str | bytes | None,
) -> None:
    command_line = " ".join(shlex.quote(part) for part in command)
    details = "\n".join(
        part.strip()
        for part in (_decode_output(stderr), _decode_output(stdout))
        if part and part.strip()
    )
    status = returncode if returncode is not None else "unknown"
    if details:
        raise ConfigError(
            f"installer command failed with exit status {status}: "
            f"{command_line}\n{details}"
        )
    raise ConfigError(f"installer command failed with exit status {status}: {command_line}")


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _tool_command(
    ctx: InstallerContext,
    command: list[str],
    *,
    stdin: bool = False,
) -> list[str]:
    tool = [
        ctx.podman,
        "run",
        "--rm",
    ]
    if stdin:
        tool.append("--interactive")
    tool.extend(
        [
            "--privileged",
            "--volume",
            f"{ctx.output_dir}:{CONTAINER_WORKDIR}",
            "--workdir",
            CONTAINER_WORKDIR,
            ctx.orchestrator,
            *command,
        ]
    )
    return tool


def _tool_path(ctx: InstallerContext, path: Path) -> Path:
    path = path.resolve()
    try:
        relative = path.relative_to(ctx.output_dir.resolve())
    except ValueError as exc:
        raise ConfigError(f"installer path is outside the mounted output directory: {path}") from exc
    return Path(CONTAINER_WORKDIR) / relative

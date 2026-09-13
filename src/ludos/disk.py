from __future__ import annotations

from base64 import b64encode
import os
import platform
import re
import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .bootc import DEFAULT_CACHE_DIR, _manifest_artifact_path
from .build import _load_dotenv
from .common import _normalize_arch, _oci_platform
from .logging import log
from .model import ConfigError, InstallerFlatpaksConfig, Manifest


SECTOR_SIZE = 512
MIB = 1024**2
GIB = 1024**3
ESP_START_SECTOR = MIB // SECTOR_SIZE
ESP_SIZE = 512 * MIB
ESP_SECTORS = ESP_SIZE // SECTOR_SIZE
BOOT_START_SECTOR = ESP_START_SECTOR + ESP_SECTORS
BOOT_SIZE = 1500 * MIB
BOOT_SECTORS = BOOT_SIZE // SECTOR_SIZE
ROOT_START_SECTOR = BOOT_START_SECTOR + BOOT_SECTORS
GPT_TRAILING_SECTORS = 34
ROOT_HEADROOM = 2 * GIB
ROOT_GROW_ATTRIBUTE = 59
BOOT_HIDDEN_ATTRIBUTE = 62
BOOT_NO_AUTO_ATTRIBUTE = 63
ROOT_NO_AUTO_ATTRIBUTE = 63
ESP_TYPE_GUID = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
BOOT_TYPE_GUID = "bc13c2ff-59e6-4262-a352-b275fd6f7172"
BTRFS_COMPRESSION = "zstd"
CONTAINER_WORKDIR = Path("/ludos/disk")
CONTAINER_SOURCE = Path("/ludos/source")


@dataclass(frozen=True)
class DiskArchitecture:
    suffix: str
    boot_filename: str
    root_type_guid: str

    @property
    def grub_filename(self) -> str:
        return f"grub{self.suffix}.efi"

    @property
    def mok_filename(self) -> str:
        return f"mm{self.suffix}.efi"

    @property
    def fallback_filename(self) -> str:
        return f"fb{self.suffix}.efi"

    @property
    def boot_csv_filename(self) -> str:
        return f"BOOT{self.suffix.upper()}.CSV"


DISK_ARCHITECTURES = {
    "aarch64": DiskArchitecture(
        "aa64",
        "BOOTAA64.EFI",
        "b921b045-1df0-41c3-af44-4c6f280d3fae",
    ),
    "x86_64": DiskArchitecture(
        "x64",
        "BOOTX64.EFI",
        "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",
    ),
}


@dataclass(frozen=True)
class DiskContext:
    manifest: Manifest
    manifest_path: Path
    source_ref: str
    target_ref: str
    output_dir: Path
    work_dir: Path
    source_mount: Path
    target_arch: str
    flatpak_uris: tuple[tuple[str, str], ...]
    requested_size: int | None
    compress: bool
    podman: str
    tooling_image: str
    tooling_arch: str
    esp_uuid: str
    boot_uuid: str
    root_uuid: str

    @property
    def architecture(self) -> DiskArchitecture:
        return _disk_architecture(self.target_arch)

    @property
    def stateroot(self) -> str:
        value = re.sub(r"[^a-z0-9_.-]+", "-", self.manifest.name.lower()).strip("-.")
        return value or "ludos"

    @property
    def root_label(self) -> str:
        return self._auxiliary_label("disk", limit=255).upper()

    @property
    def esp_label(self) -> str:
        return self._auxiliary_label("efi", limit=11).upper()

    @property
    def esp_filesystem_label(self) -> str:
        return self.esp_label.upper()

    @property
    def boot_label(self) -> str:
        return self._auxiliary_label("boot", limit=16).upper()

    def _auxiliary_label(self, suffix: str, *, limit: int) -> str:
        value = re.sub(r"[^a-z0-9]+", "_", self.manifest.name.lower()).strip("_")
        prefix = (value or "ludos")[: limit - len(suffix) - 1]
        return f"{prefix}_{suffix}"

    @property
    def disk(self) -> Path:
        return self.work_dir / "disk.raw"

    @property
    def artifact_name(self) -> str:
        return "disk.img.gz" if self.compress else "disk.raw"

    @property
    def artifact(self) -> Path:
        return self.work_dir / self.artifact_name


def bootc_disk(
    manifest_path: Path,
    ref: str,
    *,
    target_ref: str | None = None,
    output: Path | None = None,
    cache_dir: Path | None = None,
    arch: str | None = None,
    orchestrator: str | None = None,
    size: str | None = None,
    compress: bool = False,
    flatpak_uris: tuple[str, ...] = tuple(),
    force: bool = False,
) -> int:
    manifest_path = manifest_path.expanduser().resolve()
    if not manifest_path.is_file():
        raise ConfigError(f"manifest is missing: {manifest_path}")
    if not ref.strip():
        raise ConfigError("disk source image ref must not be empty")

    selected_arch = arch
    if selected_arch is None:
        selected_arch = _load_dotenv(manifest_path.parent / ".env").get("arch")
    selected_arch = _normalize_arch(selected_arch or platform.machine())
    _disk_architecture(selected_arch)
    manifest = Manifest.from_file(manifest_path, arch=selected_arch)

    local_source = _local_oci_path(ref)
    if local_source is not None and target_ref is None:
        raise ConfigError(
            "--target-ref is required when the disk source is a local OCI layout"
        )
    update_ref = _ostree_target_ref(target_ref or ref)
    parsed_size = _parse_size(size) if size is not None else None
    parsed_flatpak_uris = _parse_flatpak_uris(
        flatpak_uris,
        manifest.installer.flatpaks,
    )
    output_dir = _resolve_output_dir(
        manifest_path,
        output,
        cache_dir,
        manifest=manifest,
        arch=selected_arch,
    )

    podman = shutil.which("podman")
    if podman is None:
        raise ConfigError("podman must be installed to create a disk image")
    log("Checking that Podman is running rootless")
    _require_rootless_podman(podman)
    log(f"Preparing disk output: {output_dir}")
    _prepare_output_target(output_dir, force=force)
    work_dir = output_dir.with_name(
        f".{output_dir.name}.tmp-{uuid.uuid4().hex[:12]}"
    )
    work_dir.mkdir(parents=True)

    try:
        source_ref = _podman_source_ref(ref, local_source)
        log(f"Pulling {selected_arch} disk payload image: {source_ref}")
        image = _pull_source_image(podman, source_ref, selected_arch)
        _require_image_architecture(
            podman,
            image,
            selected_arch,
            description="disk payload",
        )
        source_mount = local_source
        if source_mount is None:
            source_mount = work_dir / "source-oci"
            log("Exporting disk payload to a rootless OCI layout")
            _export_source_image(podman, image, source_mount)

        tooling_arch = selected_arch
        tooling_image = image
        if orchestrator is not None:
            tooling_arch = _normalize_arch(platform.machine())
            log(
                f"Pulling native disk tooling image for {tooling_arch}: "
                f"{orchestrator}"
            )
            tooling_image = _pull_tooling_image(
                podman,
                orchestrator,
                tooling_arch,
            )
            _require_image_architecture(
                podman,
                tooling_image,
                tooling_arch,
                description="disk tooling",
            )
            log(f"Using separate native disk tooling image: {tooling_image}")
        else:
            log("Using the disk payload image for disk tooling")
        esp_volume_id = uuid.uuid4().hex[:8].upper()
        ctx = DiskContext(
            manifest=manifest,
            manifest_path=manifest_path,
            source_ref=f"ostree-unverified-image:oci:{CONTAINER_SOURCE}:latest",
            target_ref=update_ref,
            output_dir=output_dir,
            work_dir=work_dir,
            source_mount=source_mount,
            target_arch=selected_arch,
            flatpak_uris=parsed_flatpak_uris,
            requested_size=parsed_size,
            compress=compress,
            podman=podman,
            tooling_image=tooling_image,
            tooling_arch=tooling_arch,
            esp_uuid=f"{esp_volume_id[:4]}-{esp_volume_id[4:]}",
            boot_uuid=str(uuid.uuid4()),
            root_uuid=str(uuid.uuid4()),
        )
        log(f"Probing {tooling_arch} disk tooling execution")
        _probe_execution(ctx)
        log(f"Creating {selected_arch} UEFI disk image in rootless Podman")
        _run_disk_builder(ctx)
        _publish_output(ctx, force=force)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    log(f"Created raw disk image: {output_dir / 'disk.raw'}")
    if compress:
        log(f"Created compressed disk image: {output_dir / 'disk.img.gz'}")
    return 0


def _disk_architecture(arch: str) -> DiskArchitecture:
    normalized = _normalize_arch(arch)
    try:
        return DISK_ARCHITECTURES[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(DISK_ARCHITECTURES))
        raise ConfigError(
            f"unsupported disk architecture: {arch}; expected one of {supported}"
        ) from exc


def _parse_size(value: str) -> int:
    match = re.fullmatch(
        r"\s*([1-9][0-9]*)\s*([KMGTPE]?)(?:i?B?)?\s*",
        value,
        re.IGNORECASE,
    )
    if match is None:
        raise ConfigError(f"invalid disk size: {value}")
    units = {
        "": 1,
        "K": 1024,
        "M": MIB,
        "G": GIB,
        "T": 1024**4,
        "P": 1024**5,
        "E": 1024**6,
    }
    result = int(match.group(1)) * units[match.group(2).upper()]
    if result % SECTOR_SIZE:
        raise ConfigError("disk size must be a multiple of 512 bytes")
    minimum = (
        ROOT_START_SECTOR * SECTOR_SIZE
        + GPT_TRAILING_SECTORS * SECTOR_SIZE
        + 256 * MIB
    )
    if result < minimum:
        raise ConfigError(f"disk size must be at least {minimum} bytes")
    return result


def _resolve_output_dir(
    manifest_path: Path,
    output: Path | None,
    cache_dir: Path | None,
    *,
    manifest: Manifest,
    arch: str,
) -> Path:
    if output is not None:
        return output.expanduser().resolve()
    cache_root = (
        cache_dir.expanduser().resolve()
        if cache_dir is not None
        else (manifest_path.parent / DEFAULT_CACHE_DIR).resolve()
    )
    return _manifest_artifact_path(
        manifest_path,
        cache_root / "disk",
        manifest=manifest,
        arch=arch,
    )


def _prepare_output_target(output_dir: Path, *, force: bool) -> None:
    if not output_dir.exists() and not output_dir.is_symlink():
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        return
    if not force:
        raise ConfigError(
            f"disk output already exists: {output_dir}; use --force to replace it"
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)


def _local_oci_path(ref: str) -> Path | None:
    value = ref.strip()
    if value.startswith("oci:"):
        value = value.removeprefix("oci:")
        if value.endswith(":latest"):
            value = value.removesuffix(":latest")
        path = Path(value).expanduser()
        if not path.is_dir():
            raise ConfigError(f"disk source OCI layout is missing: {path}")
        return path.resolve()
    path = Path(value).expanduser()
    if path.exists():
        if not path.is_dir():
            raise ConfigError(f"disk source image path is not a directory: {path}")
        return path.resolve()
    return None


def _podman_source_ref(ref: str, local_source: Path | None) -> str:
    if local_source is not None:
        return f"oci:{local_source}:latest"
    return ref.strip()


def _ostree_target_ref(ref: str) -> str:
    value = ref.strip()
    if not value:
        raise ConfigError("disk target image ref must not be empty")
    if value.startswith("docker://"):
        value = value.removeprefix("docker://")
    if value.startswith("registry:"):
        return f"ostree-unverified-registry:{value.removeprefix('registry:')}"
    if value.startswith(
        (
            "ostree-image-signed:",
            "ostree-unverified-registry:",
            "ostree-remote-image:",
        )
    ):
        return value
    if value.startswith(("oci:", "dir:", "containers-storage:")):
        raise ConfigError("disk target image ref must name a persistent registry image")
    return f"ostree-unverified-registry:{value}"


def _parse_flatpak_uris(
    values: tuple[str, ...],
    groups: tuple[InstallerFlatpaksConfig, ...],
) -> tuple[tuple[str, str], ...]:
    configured = {group.repo for group in groups if group.preinstall}
    parsed: dict[str, str] = {}
    for value in values:
        remote, separator, uri = value.partition("=")
        remote = remote.strip()
        uri = uri.strip()
        if not separator or not remote or not uri:
            raise ConfigError(
                f"invalid Flatpak URI override: {value}; expected REMOTE=URI"
            )
        if remote not in configured:
            raise ConfigError(
                f"Flatpak URI override names a remote without preinstalls: {remote}"
            )
        if remote in parsed:
            raise ConfigError(f"duplicate Flatpak URI override: {remote}")
        parsed[remote] = uri
    return tuple(sorted(parsed.items()))


def _require_rootless_podman(podman: str) -> None:
    result = _run(
        [podman, "info", "--format", "{{json .Host.Security.Rootless}}"],
        capture=True,
        label="inspect Podman mode",
    )
    if result.stdout.strip().lower() != "true":
        raise ConfigError("ludos bootc disk requires rootless Podman")


def _pull_source_image(podman: str, ref: str, arch: str) -> str:
    return _pull_image(podman, ref, arch, description="disk source")


def _pull_tooling_image(podman: str, ref: str, arch: str) -> str:
    return _pull_image(podman, ref, arch, description="disk tooling")


def _pull_image(
    podman: str,
    ref: str,
    arch: str,
    *,
    description: str,
) -> str:
    result = _run(
        [podman, "pull", "--quiet", "--platform", _oci_platform(arch), ref],
        capture=True,
        label=f"pull {description} image",
    )
    candidate = next(
        (line.strip() for line in reversed(result.stdout.splitlines()) if line.strip()),
        ref,
    )
    if candidate.startswith("Loaded image: "):
        candidate = candidate.removeprefix("Loaded image: ").strip()
    inspect = _run(
        [podman, "image", "inspect", candidate, "--format", "{{.Id}}"],
        capture=True,
        label=f"inspect {description} image",
    )
    image_id = inspect.stdout.strip().lower()
    image_id = image_id.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", image_id):
        raise ConfigError(
            f"podman returned an invalid {description} image ID: {image_id}"
        )
    return f"sha256:{image_id}"


def _require_image_architecture(
    podman: str,
    image: str,
    arch: str,
    *,
    description: str = "disk source",
) -> None:
    result = _run(
        [podman, "image", "inspect", image, "--format", "{{.Architecture}}"],
        capture=True,
        label=f"inspect {description} architecture",
    )
    actual = _normalize_arch(result.stdout.strip())
    if actual != arch:
        raise ConfigError(
            f"{description} image architecture is {actual}, expected {arch}"
        )


def _export_source_image(podman: str, image: str, destination: Path) -> None:
    _run(
        [
            podman,
            "push",
            "--quiet",
            image,
            f"oci:{destination}:latest",
        ],
        label="export disk source OCI layout",
    )


def _probe_execution(ctx: DiskContext) -> None:
    foreign = _normalize_arch(platform.machine()) != ctx.tooling_arch
    if foreign:
        log(f"Probing rootless QEMU user-mode support for {ctx.tooling_arch}")
    try:
        _run(
            [
                ctx.podman,
                "run",
                "--rm",
                "--platform",
                _oci_platform(ctx.tooling_arch),
                ctx.tooling_image,
                "/usr/bin/true",
            ],
            capture=True,
            label="execute disk tooling image",
        )
    except ConfigError as exc:
        if foreign:
            raise ConfigError(
                f"cannot execute {ctx.tooling_arch} tooling with rootless Podman; "
                "configure QEMU user-mode binfmt support"
            ) from exc
        raise


def _run_disk_builder(ctx: DiskContext) -> None:
    command = _disk_builder_command(ctx)
    _run(command, label="create raw disk image")


def _disk_builder_command(ctx: DiskContext) -> list[str]:
    return [
        ctx.podman,
        "run",
        "--rm",
        "--platform",
        _oci_platform(ctx.tooling_arch),
        "--security-opt",
        "label=disable",
        "--security-opt",
        "seccomp=unconfined",
        "--mount",
        f"type=bind,source={ctx.work_dir},target={CONTAINER_WORKDIR}",
        "--mount",
        f"type=bind,source={ctx.source_mount},target={CONTAINER_SOURCE},ro=true",
        "--workdir",
        str(CONTAINER_WORKDIR),
        ctx.tooling_image,
        "/bin/sh",
        "-ceu",
        _disk_builder_script(ctx),
    ]


def _disk_builder_script(ctx: DiskContext) -> str:
    arch = ctx.architecture
    boot_csv = b64encode(_shim_boot_csv(ctx)).decode("ascii")
    flatpaks = _flatpak_script(
        ctx.manifest.installer.flatpaks,
        ctx.flatpak_uris,
        ctx.target_arch,
    )
    variables = {
        "SOURCE_REF": ctx.source_ref,
        "TARGET_REF": ctx.target_ref,
        "STATEROOT": ctx.stateroot,
        "ESP_UUID": ctx.esp_uuid,
        "ESP_VOLUME_ID": ctx.esp_uuid.replace("-", ""),
        "ESP_LABEL": ctx.esp_label,
        "ESP_FILESYSTEM_LABEL": ctx.esp_filesystem_label,
        "BOOT_UUID": ctx.boot_uuid,
        "BOOT_LABEL": ctx.boot_label,
        "ROOT_UUID": ctx.root_uuid,
        "ROOT_LABEL": ctx.root_label,
        "ROOT_TYPE_GUID": arch.root_type_guid,
        "BTRFS_COMPRESSION": BTRFS_COMPRESSION,
        "BOOT_FILENAME": arch.boot_filename,
        "GRUB_FILENAME": arch.grub_filename,
        "MOK_FILENAME": arch.mok_filename,
        "FALLBACK_FILENAME": arch.fallback_filename,
        "BOOT_CSV_FILENAME": arch.boot_csv_filename,
        "BOOT_CSV_BASE64": boot_csv,
        "TARGET_ARCH": ctx.target_arch,
        "REQUESTED_SIZE": str(ctx.requested_size or ""),
    }
    assignments = "\n".join(
        f"export {key}={shlex.quote(value)}" for key, value in variables.items()
    )
    compression = ""
    if ctx.compress:
        compression = """
disk_status "Compressing disk image with gzip level 6"
gzip -6 --stdout "$DISK_IMAGE" > "$COMPRESSED_IMAGE"
"""
    inner = f"""
umask 022
disk_status() {{ printf '==> %s\n' "$1"; }}
SYSROOT={CONTAINER_WORKDIR}/root-tree
ESP_TREE={CONTAINER_WORKDIR}/esp-tree
ESP_IMAGE={CONTAINER_WORKDIR}/esp.img
BOOT_IMAGE={CONTAINER_WORKDIR}/boot.ext4
ROOT_IMAGE={CONTAINER_WORKDIR}/root.btrfs
DISK_IMAGE={CONTAINER_WORKDIR}/disk.raw
COMPRESSED_IMAGE={CONTAINER_WORKDIR}/disk.img.gz

rm -rf "$SYSROOT" "$ESP_TREE"
rm -f "$ESP_IMAGE" "$BOOT_IMAGE" "$ROOT_IMAGE" "$DISK_IMAGE" "$COMPRESSED_IMAGE"
mkdir -p "$SYSROOT" "$ESP_TREE/EFI/BOOT"

disk_status "Initializing OSTree sysroot"
ostree admin init-fs --modern "$SYSROOT"
ostree admin os-init --sysroot "$SYSROOT" "$STATEROOT"
disk_status "Importing bootc payload into OSTree"
ostree container image pull \
    --ostree-digestfile "{CONTAINER_WORKDIR}/commit" \
    "$SYSROOT/ostree/repo" \
    "$SOURCE_REF"
COMMIT=$(cat "{CONTAINER_WORKDIR}/commit")
cat > "{CONTAINER_WORKDIR}/origin" <<EOF_ORIGIN
[origin]
container-image-reference=$TARGET_REF
EOF_ORIGIN
disk_status "Deploying bootc payload"
ostree admin deploy \
    --sysroot "$SYSROOT" \
    --os "$STATEROOT" \
    --no-merge \
    --origin-file "{CONTAINER_WORKDIR}/origin" \
    --karg "root=UUID=$ROOT_UUID" \
    --karg "rootflags=compress=$BTRFS_COMPRESSION" \
    --karg rw \
    "$COMMIT"
rm -f "{CONTAINER_WORKDIR}/commit" "{CONTAINER_WORKDIR}/origin"

DEPLOY=$(find "$SYSROOT/ostree/deploy/$STATEROOT/deploy" -mindepth 1 -maxdepth 1 -type d -name '*.0' -print -quit)
test -n "$DEPLOY"
test -f "$DEPLOY.origin"
grep -Fq "container-image-reference=$TARGET_REF" "$DEPLOY.origin"
find "$SYSROOT/boot/loader/entries" -type f -name '*.conf' -print -quit | grep -q .

mkdir -p "$DEPLOY/etc/repart.d"
cat > "$DEPLOY/etc/repart.d/50-ludos-root.conf" <<'EOF_REPART'
[Partition]
Type=root
GrowFileSystem=yes
EOF_REPART
mkdir -p "$SYSROOT/boot/efi"
printf '\nUUID=%s /boot ext4 defaults 0 2\nUUID=%s /boot/efi vfat umask=0077,shortname=winnt 0 2\n' \
    "$BOOT_UUID" "$ESP_UUID" >> "$DEPLOY/etc/fstab"

disk_status "Preinstalling Flatpaks"
{flatpaks}

disk_status "Preparing UEFI boot assets"
mkdir -p "$SYSROOT/boot/grub2"
cat "$DEPLOY/usr/lib/bootupd/grub2-static/grub-static-pre.cfg" \
    "$DEPLOY"/usr/lib/bootupd/grub2-static/configs.d/*.cfg \
    > "$SYSROOT/boot/grub2/grub.cfg"
grub2-editenv "$SYSROOT/boot/grub2/grubenv" create

GRUB_SOURCE=$(find "$DEPLOY/usr/lib/efi/grub2" -type f -name "$GRUB_FILENAME" -print | sort -V | tail -n 1)
SHIM_SOURCE=$(find "$DEPLOY/usr/lib/efi/shim" -type f -name "$BOOT_FILENAME" -print | sort -V | tail -n 1)
MOK_SOURCE=$(find "$DEPLOY/usr/lib/efi/shim" -type f -name "$MOK_FILENAME" -print | sort -V | tail -n 1)
FALLBACK_SOURCE=$(find "$DEPLOY/usr/lib/efi/shim" -type f -name "$FALLBACK_FILENAME" -print | sort -V | tail -n 1)
test -n "$GRUB_SOURCE"
test -n "$SHIM_SOURCE"
test -n "$MOK_SOURCE"
VENDOR=$(basename "$(dirname "$GRUB_SOURCE")")
mkdir -p "$ESP_TREE/EFI/$VENDOR"
cp "$SHIM_SOURCE" "$ESP_TREE/EFI/BOOT/$BOOT_FILENAME"
cp "$GRUB_SOURCE" "$ESP_TREE/EFI/BOOT/$GRUB_FILENAME"
cp "$MOK_SOURCE" "$ESP_TREE/EFI/BOOT/$MOK_FILENAME"
test -z "$FALLBACK_SOURCE" || cp "$FALLBACK_SOURCE" "$ESP_TREE/EFI/BOOT/$FALLBACK_FILENAME"
cp "$GRUB_SOURCE" "$ESP_TREE/EFI/$VENDOR/$GRUB_FILENAME"
cp "$MOK_SOURCE" "$ESP_TREE/EFI/$VENDOR/$MOK_FILENAME"
cp "$SHIM_SOURCE" "$ESP_TREE/EFI/$VENDOR/shim{arch.suffix}.efi"
test -z "$FALLBACK_SOURCE" || cp "$FALLBACK_SOURCE" "$ESP_TREE/EFI/$VENDOR/$FALLBACK_FILENAME"
printf '%s' "$BOOT_CSV_BASE64" | base64 --decode > "$ESP_TREE/EFI/$VENDOR/$BOOT_CSV_FILENAME"

for directory in "$ESP_TREE/EFI/BOOT" "$ESP_TREE/EFI/$VENDOR"; do
    cp "$DEPLOY/usr/lib/bootupd/grub2-static/grub-static-efi.cfg" "$directory/grub.cfg"
    printf 'set BOOT_UUID=%s\n' "$BOOT_UUID" > "$directory/bootuuid.cfg"
done

disk_status "Applying target SELinux policy"
POLICY="$DEPLOY/etc/selinux/targeted/contexts/files/file_contexts"
test -f "$POLICY"
setfiles -F -q -r "$SYSROOT" "$POLICY" \
    "$SYSROOT/boot" \
    "$SYSROOT/ostree/deploy/$STATEROOT/var" \
    "$DEPLOY/etc/fstab" \
    "$DEPLOY/etc/repart.d"

disk_status "Creating ext4 /boot filesystem"
truncate -s {BOOT_SIZE} "$BOOT_IMAGE"
mkfs.ext4 -q -F -m 0 -L "$BOOT_LABEL" -U "$BOOT_UUID" -d "$SYSROOT/boot" "$BOOT_IMAGE"
grub2-fstest "$BOOT_IMAGE" ls /loader/entries | grep -q .
rm -rf "$SYSROOT/boot"
mkdir -p "$SYSROOT/boot"
setfiles -F -q -r "$SYSROOT" "$POLICY" "$SYSROOT/boot"

disk_status "Creating Btrfs root filesystem"
USED=$(du -sx -B1 "$SYSROOT" | cut -f1)
SEED_ROOT=$((USED + USED / 4 + {GIB}))
SEED_ROOT=$(((SEED_ROOT + {GIB} - 1) / {GIB} * {GIB}))
truncate -s "$SEED_ROOT" "$ROOT_IMAGE"
mkfs.btrfs --force --shrink --compress "$BTRFS_COMPRESSION" \
    --label "$ROOT_LABEL" --uuid "$ROOT_UUID" \
    --rootdir "$SYSROOT" "$ROOT_IMAGE"
FILESYSTEM_BYTES=$(stat -c %s "$ROOT_IMAGE")
MIN_ROOT=$((FILESYSTEM_BYTES + {ROOT_HEADROOM}))
MIN_ROOT=$(((MIN_ROOT + {GIB} - 1) / {GIB} * {GIB}))
MIN_DISK=$(({ROOT_START_SECTOR} * {SECTOR_SIZE} + MIN_ROOT + {GPT_TRAILING_SECTORS} * {SECTOR_SIZE}))
MIN_DISK=$(((MIN_DISK + {GIB} - 1) / {GIB} * {GIB}))
if test -n "$REQUESTED_SIZE"; then
    DISK_BYTES=$REQUESTED_SIZE
    if test "$DISK_BYTES" -lt "$MIN_DISK"; then
        echo "requested disk size $DISK_BYTES is smaller than required size $MIN_DISK" >&2
        exit 65
    fi
else
    DISK_BYTES=$MIN_DISK
fi
TOTAL_SECTORS=$((DISK_BYTES / {SECTOR_SIZE}))
ROOT_SECTORS=$((TOTAL_SECTORS - {ROOT_START_SECTOR} - {GPT_TRAILING_SECTORS}))
ROOT_BYTES=$((ROOT_SECTORS * {SECTOR_SIZE}))
if test "$FILESYSTEM_BYTES" -gt "$ROOT_BYTES"; then
    echo "Btrfs filesystem is larger than the root partition" >&2
    exit 65
fi
truncate -s "$ROOT_BYTES" "$ROOT_IMAGE"

disk_status "Creating FAT32 EFI system partition"
truncate -s {ESP_SIZE} "$ESP_IMAGE"
mkfs.vfat -F 32 -n "$ESP_FILESYSTEM_LABEL" -i "$ESP_VOLUME_ID" "$ESP_IMAGE"
mcopy -s -i "$ESP_IMAGE" "$ESP_TREE/EFI" ::/

disk_status "Assembling sparse GPT disk image"
truncate -s "$DISK_BYTES" "$DISK_IMAGE"
sfdisk --quiet "$DISK_IMAGE" <<EOF_SFDISK
label: gpt
unit: sectors

start={ESP_START_SECTOR}, size={ESP_SECTORS}, type={ESP_TYPE_GUID}, name="$ESP_LABEL"
start={BOOT_START_SECTOR}, size={BOOT_SECTORS}, type={BOOT_TYPE_GUID}, name="$BOOT_LABEL"
start={ROOT_START_SECTOR}, size=$ROOT_SECTORS, type=$ROOT_TYPE_GUID, name="$ROOT_LABEL"
EOF_SFDISK
sfdisk --part-attrs "$DISK_IMAGE" 2 "GUID:{BOOT_HIDDEN_ATTRIBUTE},GUID:{BOOT_NO_AUTO_ATTRIBUTE}"
sfdisk --part-attrs "$DISK_IMAGE" 3 "GUID:{ROOT_GROW_ATTRIBUTE},GUID:{ROOT_NO_AUTO_ATTRIBUTE}"
dd if="$ESP_IMAGE" of="$DISK_IMAGE" bs={SECTOR_SIZE} seek={ESP_START_SECTOR} conv=notrunc,sparse status=none
dd if="$BOOT_IMAGE" of="$DISK_IMAGE" bs={SECTOR_SIZE} seek={BOOT_START_SECTOR} conv=notrunc,sparse status=none
dd if="$ROOT_IMAGE" of="$DISK_IMAGE" bs={SECTOR_SIZE} seek={ROOT_START_SECTOR} conv=notrunc,sparse status=none
sync "$DISK_IMAGE"

disk_status "Validating disk image"
sfdisk --verify "$DISK_IMAGE"
blkid -p -O $(({ESP_START_SECTOR} * {SECTOR_SIZE})) -S {ESP_SIZE} "$DISK_IMAGE" | grep -q 'TYPE="vfat"'
blkid -p -O $(({BOOT_START_SECTOR} * {SECTOR_SIZE})) -S {BOOT_SIZE} "$DISK_IMAGE" | grep -q 'TYPE="ext4"'
blkid -p -O $(({ROOT_START_SECTOR} * {SECTOR_SIZE})) -S "$ROOT_BYTES" "$DISK_IMAGE" | grep -q 'TYPE="btrfs"'
mdir -i "$ESP_IMAGE" "::/EFI/BOOT/$BOOT_FILENAME" >/dev/null
mdir -i "$ESP_IMAGE" "::/EFI/$VENDOR/$BOOT_CSV_FILENAME" >/dev/null
e2fsck -fn "$BOOT_IMAGE"
btrfs inspect-internal dump-super "$ROOT_IMAGE" >/dev/null

rm -rf "$SYSROOT" "$ESP_TREE"
rm -f "$ESP_IMAGE" "$BOOT_IMAGE" "$ROOT_IMAGE"

{compression}
"""
    required_tools = (
        "fakeroot",
        "ostree",
        "flatpak",
        "setfiles",
        "grub2-editenv",
        "grub2-fstest",
        "mkfs.btrfs",
        "mkfs.ext4",
        "mkfs.vfat",
        "mcopy",
        "mdir",
        "sfdisk",
        "blkid",
        "btrfs",
        "e2fsck",
        "base64",
    )
    if ctx.compress:
        required_tools = (*required_tools, "gzip")
    required = " ".join(required_tools)
    return f"""{assignments}
printf '==> Validating disk tooling\n'
for tool in {required}; do
    command -v "$tool" >/dev/null || {{ echo "required disk tool is missing: $tool" >&2; exit 69; }}
done
fakeroot -s {CONTAINER_WORKDIR}/fakeroot.db -- /bin/sh -ceu {shlex.quote(inner)}
rm -f {CONTAINER_WORKDIR}/fakeroot.db
"""


def _shim_boot_csv(ctx: DiskContext) -> bytes:
    """Return shim fallback metadata in its required UTF-16LE representation."""
    label = re.sub(r"[,\r\n]+", " ", ctx.manifest.name).strip() or ctx.stateroot
    record = (
        f"shim{ctx.architecture.suffix}.efi,{label},,"
        f"This is the boot entry for {label}\n"
    )
    return b"\xff\xfe" + record.encode("utf-16-le")


def _flatpak_script(
    groups: tuple[InstallerFlatpaksConfig, ...],
    overrides: tuple[tuple[str, str], ...],
    arch: str,
) -> str:
    override_map = dict(overrides)
    lines = [
        'export FLATPAK_SYSTEM_DIR="$SYSROOT/ostree/deploy/$STATEROOT/var/lib/flatpak"',
        'export FLATPAK_CONFIG_DIR="$DEPLOY/etc/flatpak"',
        'export FLATPAK_DATA_DIR="$DEPLOY/usr/share/flatpak"',
        'mkdir -p "$FLATPAK_SYSTEM_DIR" "$FLATPAK_CONFIG_DIR"',
    ]
    for group in groups:
        if not group.preinstall:
            continue
        remote = shlex.quote(group.repo)
        if group.repo in override_map:
            uri = shlex.quote(override_map[group.repo])
            lines.append(
                f"flatpak --system remote-modify --url={uri} {remote}"
            )
        options = [
            "--system",
            "install",
            "--assumeyes",
            "--noninteractive",
            f"--arch={arch}",
        ]
        if group.nodeps:
            options.append("--no-deps")
        command = ["flatpak", *options, group.repo, *group.preinstall]
        lines.append(" ".join(shlex.quote(part) for part in command))
    return "\n".join(lines)


def _publish_output(ctx: DiskContext, *, force: bool = False) -> None:
    if not ctx.artifact.is_file():
        raise ConfigError(f"disk builder did not create {ctx.artifact_name}")
    try:
        ctx.source_mount.relative_to(ctx.work_dir)
    except ValueError:
        pass
    else:
        shutil.rmtree(ctx.source_mount)
    if ctx.output_dir.is_dir() and not ctx.output_dir.is_symlink():
        if not force:
            raise ConfigError(f"disk output already exists: {ctx.output_dir}")
        shutil.rmtree(ctx.output_dir)
    elif ctx.output_dir.exists() or ctx.output_dir.is_symlink():
        if not force:
            raise ConfigError(f"disk output already exists: {ctx.output_dir}")
        ctx.output_dir.unlink()
    os.replace(ctx.work_dir, ctx.output_dir)


def _run(
    command: list[str],
    *,
    capture: bool = False,
    label: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode == 0:
        return result
    display_command = list(command)
    if len(display_command) >= 2 and display_command[-2] == "-ceu":
        display_command[-1] = "<disk-builder-script>"
    command_line = " ".join(shlex.quote(part) for part in display_command)
    details = "\n".join(
        value.strip()
        for value in (result.stderr, result.stdout)
        if value and value.strip()
    )
    message = f"failed to {label} (exit {result.returncode}): {command_line}"
    if details:
        message = f"{message}\n{details}"
    raise ConfigError(message)

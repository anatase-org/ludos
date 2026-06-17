from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .build import _run_streamed_command
from .logging import log
from .model import ConfigError


DEFAULT_CACHE_DIR = Path("cache")
DEFAULT_ORCHESTRATOR = "orchestrator"
DEFAULT_OSTREE_REF = "master"
SOURCE_MOUNT = "/ludos/source"
OSTREE_MOUNT = "/ludos/ostree"


def ostree_import(
    ref: str,
    *,
    cache_dir: Path | None = None,
    orchestrator: str = DEFAULT_ORCHESTRATOR,
    ostree_ref: str = DEFAULT_OSTREE_REF,
) -> int:
    if not ref.strip():
        raise ConfigError("container ref must not be empty")
    if not ostree_ref.strip():
        raise ConfigError("ostree ref must not be empty")

    podman = shutil.which("podman")
    if podman is None:
        raise ConfigError("podman must be installed to import a bootc image")

    cache_root = (cache_dir or DEFAULT_CACHE_DIR).expanduser().resolve()
    ostree_dir = cache_root / "ostree"
    ostree_dir.mkdir(parents=True, exist_ok=True)

    _require_image(podman, ref, "source image")
    _require_image(podman, orchestrator, "orchestrator image")
    orchestrator_display_ref = _image_display_ref(podman, orchestrator)

    log(f"Importing {ref} into OSTree repo: {ostree_dir}")
    log(f"Using orchestrator image: {orchestrator} ({orchestrator_display_ref})")

    script = "\n".join(
        [
            'repo="/ludos/ostree"',
            'source="/ludos/source"',
            'if [ ! -d "$repo/objects" ]; then',
            '  ostree --repo="$repo" init --mode=bare-user',
            '  ostree --repo="$repo" config set core.fsync false',
            "fi",
            'ostree --repo="$repo" commit \\',
            '  -b "$LUDOS_OSTREE_REF" \\',
            '  --tree=dir="$source" \\',
            "  --bootable \\",
            '  --selinux-policy="$source" \\',
            "  --selinux-labeling-epoch=1",
            'commit=$(ostree --repo="$repo" rev-parse "$LUDOS_OSTREE_REF")',
            'printf "Imported OSTree commit: %s\\n" "$commit"',
        ]
    )

    command = [
        podman,
        "run",
        "--rm",
        "--mount",
        f"type=image,source={ref},target={SOURCE_MOUNT}",
        "--mount",
        f"type=bind,source={ostree_dir},target={OSTREE_MOUNT}",
        "--env",
        f"LUDOS_OSTREE_REF={ostree_ref}",
        "--workdir",
        "/ludos",
        orchestrator,
        "/bin/sh",
        "-ceu",
        script,
    ]
    returncode, _output = _run_streamed_command(command)
    if returncode != 0:
        raise ConfigError(f"bootc ostree import failed with exit status {returncode}")

    log(f"Imported {ref} as {ostree_ref} in {ostree_dir}")
    return 0


def _require_image(podman: str, image: str, description: str) -> None:
    result = subprocess.run([podman, "image", "exists", image], check=False)
    if result.returncode != 0:
        raise ConfigError(f"{description} is not available locally: {image}")


def _image_display_ref(podman: str, image: str) -> str:
    result = subprocess.run(
        [podman, "image", "inspect", image, "--format", "{{json .}}"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    data = json.loads(result.stdout)
    repo_tags = data.get("RepoTags") or ()
    if repo_tags:
        return str(repo_tags[0])
    repo_digests = data.get("RepoDigests") or ()
    if repo_digests:
        return _short_digest(str(repo_digests[0]))
    image_id = str(data.get("Id") or "").strip()
    if image_id:
        return _short_digest(image_id)
    return image


def _short_digest(value: str) -> str:
    digest = value.rsplit("@", 1)[-1]
    if digest.startswith("sha256:"):
        return f"sha256:{digest.removeprefix('sha256:')[:12]}"
    if len(digest) >= 12 and all(c in "0123456789abcdef" for c in digest[:12].lower()):
        return digest[:12]
    return digest

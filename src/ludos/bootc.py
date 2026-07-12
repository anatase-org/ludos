from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import build as build_module
from .build import (
    _cache_name,
    _cleanup_dnf_workspaces,
    _load_dotenv,
    _local_prefix,
    _substitute_variables,
    _terminate_process_group,
    build_build_images,
    build_final_manifest_images,
    build_package_card_images,
    resolve_build_manifests,
)
from .common import _default_cache_version
from .logging import log, piter, pstream, warning
from .model import ConfigError, Manifest
from .rechunk.alg import main as rechunk_main


DEFAULT_CACHE_DIR = Path("cache")
DEFAULT_OSTREE_REF = "os"
DEFAULT_OCI_WRITERS = 4
OCI_VERSION_LABEL = "org.opencontainers.image.version"
SOURCE_MOUNT = "/ludos/source"
OSTREE_MOUNT = "/ludos/ostree"
POSTPROCESS_MOUNT = "/ludos/postprocess.py"
PROGRESS_TOTAL_PREFIX = "__LUDOS_OSTREE_APPROX_TOTAL__ "
COMMIT_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_OCI_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
OCI_EXPORT_PROGRESS_RE = re.compile(r"^Exported OCI layer \d+/(?P<total>\d+): .+$")


def bootc_create(
    manifests: tuple[Path, ...],
    *,
    chunks: Path | None = None,
    previous_manifest: str | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ci: bool = False,
    ccache: bool = True,
    writers: int = DEFAULT_OCI_WRITERS,
    force: bool = False,
) -> int:
    if not manifests:
        raise ConfigError("at least one manifest is required")
    if writers < 1:
        raise ConfigError("writers must be at least 1")

    cache_root = _resolve_cache_root(manifests, cache_dir)
    chunks_path = _resolve_chunks_path(manifests, chunks)

    metadata = tuple()
    try:
        metadata = resolve_build_manifests(
            manifests,
            cache_dir=cache_root,
            cache_version=cache_version,
            cache_only=cache_only,
            ccache=ccache,
        )
        mode = "combined" if ci else "separated"
        final_metadata = (
            build_module._resolve_final_manifest_metadata(metadata, mode=mode)
            if all(hasattr(item, "releasever") for item in metadata)
            else metadata
        )
        can_reuse_final = all(
            hasattr(item, "podman") and hasattr(item, "output_image")
            for item in final_metadata
        )
        if not force and can_reuse_final and all(
            build_module._ensure_image(
                item.podman,
                item.output_image,
                getattr(item, "ci_registry", ""),
            )
            for item in final_metadata
        ):
            for item in final_metadata:
                build_module._tag_image(item.podman, item.output_image, item.latest_image)
            metadata = final_metadata
            results = tuple(
                build_module._metadata_build_result(item)
                for item in metadata
            )
        else:
            metadata = final_metadata
            build_package_card_images(metadata, cache_only=cache_only)
            build_outputs = build_build_images(metadata, cache_only=cache_only)
            results = build_final_manifest_images(
                metadata,
                build_outputs=build_outputs,
                mode=mode,
                cache_only=cache_only,
                force=force,
            )
    finally:
        _cleanup_dnf_workspaces(metadata)

    ostree_dir = cache_root / "ostree"
    oci_dir = cache_root / "oci"
    work_root = cache_root / "rechunk"
    oci_dir.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    previous_manifest_path = None
    if previous_manifest:
        previous_manifest_path = work_root / "previous-manifest.json"
        _fetch_previous_manifest(previous_manifest, previous_manifest_path)

    for manifest, result in zip(metadata, results):
        image = result.output_image
        safe_name = _bootc_artifact_name(manifest.image, manifest.distro)
        work_dir = work_root / safe_name
        git_dir = Path(manifest.root_dir)
        revision = _git_revision(git_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        contentmeta = work_dir / "contentmeta.json"
        result_fn = work_dir / "results.txt"

        log(f"Creating bootc OSTree import for {image}")
        import_kwargs = {
            "cache_dir": cache_root,
            "orchestrator": image,
            "ostree_ref": DEFAULT_OSTREE_REF,
        }
        ostree_version = dict(manifest.manifest_labels).get(OCI_VERSION_LABEL)
        if ostree_version:
            import_kwargs["ostree_version"] = ostree_version
        ostree_import(image, **import_kwargs)

        log(f"Rechunking {image} using {chunks_path}")
        rechunk_kwargs = dict(
            repo=str(ostree_dir),
            ref=DEFAULT_OSTREE_REF,
            contentmeta_fn=str(contentmeta),
            chunks_fn=str(chunks_path),
            result_fn=str(result_fn),
            labels=_manifest_labels(manifest.manifest_labels),
            revision=revision,
            git_dir=str(git_dir),
            ostree_image=image,
            podman=result.podman,
        )
        if previous_manifest_path:
            rechunk_kwargs["previous_manifest"] = str(previous_manifest_path)
        rechunk_main(**rechunk_kwargs)

        _export_rechunked_oci(
            podman=result.podman,
            image=image,
            ostree_dir=ostree_dir,
            oci_dir=oci_dir,
            work_dir=work_dir,
            safe_name=safe_name,
            writers=writers,
        )

    return 0


def _fetch_previous_manifest(ref: str, output: Path) -> Path:
    skopeo = shutil.which("skopeo")
    if skopeo is None:
        raise ConfigError("skopeo must be installed to inspect a previous manifest")

    transport_ref = ref if "://" in ref else f"docker://{ref}"
    log(f"Inspecting previous bootc manifest: {ref}")
    result = subprocess.run(
        [skopeo, "inspect", "--no-tags", transport_ref],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ConfigError(f"failed to inspect previous manifest: {ref}")
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"skopeo returned an invalid previous manifest: {ref}") from exc
    if not isinstance(manifest, dict):
        raise ConfigError(f"skopeo returned an invalid previous manifest: {ref}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest), encoding="utf-8")
    return output


def ostree_import(
    ref: str,
    *,
    cache_dir: Path | None = None,
    orchestrator: str | None = None,
    ostree_ref: str = DEFAULT_OSTREE_REF,
    process: bool = True,
    ostree_version: str | None = None,
) -> int:
    if not ref.strip():
        raise ConfigError("container ref must not be empty")
    if not ostree_ref.strip():
        raise ConfigError("ostree ref must not be empty")
    orchestrator = orchestrator or ref

    podman = shutil.which("podman")
    if podman is None:
        raise ConfigError("podman must be installed to import a bootc image")

    cache_root = (cache_dir or DEFAULT_CACHE_DIR).expanduser().resolve()
    ostree_dir = cache_root / "ostree"
    ostree_dir.mkdir(parents=True, exist_ok=True)

    _require_image(podman, ref, "source image")
    if orchestrator != ref:
        _require_image(podman, orchestrator, "orchestrator image")
    source_info = None
    if not ostree_version or orchestrator == ref:
        source_info = _image_inspect(podman, ref)
    if not ostree_version:
        assert source_info is not None
        ostree_version = _image_label(source_info, OCI_VERSION_LABEL)
    if orchestrator == ref:
        orchestrator_info = source_info
        assert orchestrator_info is not None
    else:
        orchestrator_info = _image_inspect(podman, orchestrator)
    orchestrator_display_ref = _image_display_ref(orchestrator_info)

    log(f"Importing {ref} into OSTree repo: {ostree_dir}")
    log(f"Using orchestrator image: {orchestrator} ({orchestrator_display_ref})")
    if process:
        log("Postprocessing image root before OSTree import")
    else:
        log("Importing image root without postprocessing")

    command = [
        podman,
        "run",
        "--rm",
        "--mount",
        f"type=image,source={ref},target={SOURCE_MOUNT},rw=true",
        "--mount",
        f"type=bind,source={ostree_dir},target={OSTREE_MOUNT}",
    ]
    if process:
        command.extend(
            [
                "--mount",
                (
                    "type=bind,"
                    f"source={Path(__file__).with_name('postprocess.py').resolve()},"
                    f"target={POSTPROCESS_MOUNT},ro"
                ),
            ]
        )
    command.extend(
        [
            "--env",
            f"LUDOS_OSTREE_REF={ostree_ref}",
            *(
                ["--env", f"LUDOS_OSTREE_VERSION={ostree_version}"]
                if ostree_version
                else []
            ),
            "--workdir",
            "/ludos",
            orchestrator,
        ]
    )
    if process:
        command.extend(
            [
                "python3",
                POSTPROCESS_MOUNT,
                "--progress-total-prefix",
                PROGRESS_TOTAL_PREFIX,
                *(
                    ["--ostree-version", ostree_version]
                    if ostree_version
                    else []
                ),
                SOURCE_MOUNT,
                OSTREE_MOUNT,
                ostree_ref,
            ]
        )
    else:
        command.extend(["/bin/sh", "-ceu", _unprocessed_ostree_import_script()])

    returncode, output = _run_ostree_import_command(command, ostree_dir)
    if returncode != 0:
        raise ConfigError(f"bootc ostree import failed with exit status {returncode}")
    commit = _parse_commit(output)

    log(f"Imported {ref} as {ostree_ref} ({commit}) in {ostree_dir}")
    return 0


def _resolve_cache_root(manifests: tuple[Path, ...], cache_dir: Path | None) -> Path:
    if cache_dir is not None:
        return cache_dir.expanduser().resolve()
    return (manifests[0].resolve().parent / DEFAULT_CACHE_DIR).resolve()


def _resolve_chunks_path(manifests: tuple[Path, ...], chunks: Path | None) -> Path:
    chunks_path = chunks or (manifests[0].resolve().parent / "chunks.yml")
    chunks_path = chunks_path.expanduser().resolve()
    if not chunks_path.is_file():
        raise ConfigError(f"chunks file is missing: {chunks_path}")
    return chunks_path


def _manifest_artifact_path(
    manifest_path: Path,
    parent: Path,
    *,
    manifest: Manifest | None = None,
    cache_version: str | None = None,
) -> Path:
    return parent / _manifest_artifact_name(
        manifest_path,
        manifest=manifest,
        cache_version=cache_version,
    )


def _manifest_artifact_name(
    manifest_path: Path,
    *,
    manifest: Manifest | None = None,
    cache_version: str | None = None,
) -> str:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = manifest or Manifest.from_file(manifest_path)
    env = _manifest_artifact_env(manifest_path, manifest, cache_version)
    image = _cache_name(manifest_path.stem, "image")
    distro = _cache_name(_substitute_variables(manifest.distro, env), "distro")
    return _bootc_artifact_name(image, distro)


def _manifest_artifact_env(
    manifest_path: Path,
    manifest: Manifest,
    cache_version: str | None,
) -> dict[str, str]:
    root_dir = manifest_path.resolve().parent
    env = {key: str(value) for key, value in manifest.env.items()}
    local_values = _load_dotenv(root_dir / ".env")
    local_prefix = local_values.pop("local_prefix", manifest.local_prefix)
    _local_prefix(local_prefix)
    env.update(local_values)
    if cache_version is None:
        cache_version = _default_cache_version()
    else:
        cache_version = _cache_name(cache_version, "version")
    env["version"] = cache_version
    releasever = _cache_name(
        _substitute_variables(manifest.releasever, env),
        "releasever",
    )
    env["releasever"] = releasever
    arch = _cache_name(
        _substitute_variables(str(env.get("arch", "")), env),
        "arch",
    )
    env["arch"] = arch
    env = {key: _substitute_variables(value, env) for key, value in env.items()}
    env["distro"] = _cache_name(
        _substitute_variables(manifest.distro, env),
        "distro",
    )
    return env


def _bootc_artifact_name(image: str, distro: str) -> str:
    image = _cache_name(image, "image")
    distro = _cache_name(distro, "distro")
    return _safe_oci_name(f"{image}-{distro}")


def _manifest_labels(labels: tuple[tuple[str, str], ...]) -> list[str]:
    return [f"{key}={value}" for key, value in labels]


def _git_revision(git_dir: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(git_dir), "rev-parse", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        return revision
    return None


def _safe_oci_name(image: str) -> str:
    value = image
    value = value.replace(":", "-")
    value = SAFE_OCI_NAME_RE.sub("-", value).strip("-._")
    return value or "image"


def _export_rechunked_oci(
    *,
    podman: str,
    image: str,
    ostree_dir: Path,
    oci_dir: Path,
    work_dir: Path,
    safe_name: str,
    writers: int = DEFAULT_OCI_WRITERS,
) -> None:
    if writers < 1:
        raise ConfigError("writers must be at least 1")
    target_dir = oci_dir / safe_name
    if target_dir.is_symlink() or target_dir.is_file():
        target_dir.unlink()
    elif target_dir.exists():
        shutil.rmtree(target_dir)
    target = f"oci:/ludos/oci/{safe_name}:latest"
    log(f"Exporting rechunked OCI image: {target_dir}")
    encapsulate_args = [
        "bootc",
        "internals",
        "ostree-ext",
        "container",
        "encapsulate",
        "--repo",
        "/ludos/ostree",
        "--contentmeta",
        "/ludos/rechunk/contentmeta.json",
    ]
    if _bootc_encapsulate_supports_jobs(podman, image):
        encapsulate_args.extend(["--jobs", str(writers)])
    returncode, _output = _run_oci_export_command(
        [
            podman,
            "run",
            "--rm",
            "--volume",
            f"{ostree_dir}:/ludos/ostree:ro",
            "--volume",
            f"{work_dir}:/ludos/rechunk:ro",
            "--volume",
            f"{oci_dir}:/ludos/oci",
            image,
            *encapsulate_args,
            DEFAULT_OSTREE_REF,
            target,
        ]
    )
    if returncode != 0:
        raise ConfigError(f"bootc OCI export failed with exit status {returncode}")


def _bootc_encapsulate_supports_jobs(podman: str, image: str) -> bool:
    command = [
        podman,
        "run",
        "--rm",
        image,
        "bootc",
        "internals",
        "ostree-ext",
        "container",
        "encapsulate",
        "--help",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        warning("Could not verify bootc encapsulate --jobs support; exporting without --jobs")
        return False
    if result.returncode != 0:
        warning(
            "Could not verify bootc encapsulate --jobs support "
            f"(help exited {result.returncode}); exporting without --jobs"
        )
        return False
    if "--jobs" not in result.stdout:
        warning("bootc encapsulate does not support --jobs; exporting without --jobs")
        return False
    return True


def _oci_export_line_rewriter(line: str) -> str:
    stripped = line.strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", stripped):
        return f"Exported OCI digest: {stripped}\n"
    return line


def _run_oci_export_command(command: list[str]) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    stdout_chunks: list[str] = []

    with piter(
        total=None,
        desc="Exporting OCI layers",
        unit="layers",
        leave=False,
    ) as progress:
        stdout_thread = threading.Thread(
            target=_read_oci_export_stdout,
            args=(process, stdout_chunks),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_oci_export_stderr,
            args=(process, progress),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = process.wait()
        finally:
            if process.poll() is None:
                _terminate_process_group(process)
            stdout_thread.join()
            stderr_thread.join()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    return returncode, "".join(stdout_chunks)


def _read_oci_export_stdout(
    process: subprocess.Popen[str],
    output: list[str],
) -> None:
    assert process.stdout is not None
    for raw_line in process.stdout:
        output.append(raw_line)
        line = _oci_export_line_rewriter(raw_line).rstrip("\n")
        if line:
            pstream(line)


def _read_oci_export_stderr(process: subprocess.Popen[str], progress: object) -> None:
    assert process.stderr is not None
    for raw_line in process.stderr:
        line = raw_line.rstrip("\n")
        match = OCI_EXPORT_PROGRESS_RE.match(line)
        if match is not None:
            progress.total = int(match.group("total"))
            progress.refresh()
            progress.update(1)
        pstream(line)


def _unprocessed_ostree_import_script() -> str:
    return "\n".join(
        [
            'repo="/ludos/ostree"',
            'source="/ludos/source"',
            'if [ ! -d "$repo/objects" ]; then',
            '  ostree --repo="$repo" init --mode=bare-user',
            '  ostree --repo="$repo" config set core.fsync false',
            "fi",
            'set --',
            'if [ -n "${LUDOS_OSTREE_VERSION:-}" ]; then',
            '  set -- "--add-metadata-string=version=$LUDOS_OSTREE_VERSION"',
            "fi",
            'approx_entries=$(find "$source" -mindepth 1 -printf ".\\n" | wc -l)',
            'approx_total=$((approx_entries * 2 + 1))',
            f'printf "{PROGRESS_TOTAL_PREFIX}%s\\n" "$approx_total" >&2',
            'commit=$(env -u G_MESSAGES_DEBUG ostree --repo="$repo" commit -v \\',
            '  -b "$LUDOS_OSTREE_REF" \\',
            '  --tree=dir="$source" \\',
            '  "$@" \\',
            "  --bootable \\",
            '  --selinux-policy="$source" \\',
            "  --selinux-labeling-epoch=1)",
            'printf "%s\\n" "$commit"',
        ]
    )


def _run_ostree_import_command(command: list[str], ostree_dir: Path) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    stdout_chunks: list[str] = []
    baseline = _count_repo_objects(ostree_dir)
    stop_counter = threading.Event()

    with piter(
        total=None,
        desc="Importing OSTree",
        unit="objects",
        leave=False,
    ) as progress:
        stderr_thread = threading.Thread(
            target=_read_ostree_stderr,
            args=(process, progress),
            daemon=True,
        )
        stdout_thread = threading.Thread(
            target=_read_stdout,
            args=(process, stdout_chunks),
            daemon=True,
        )
        counter_thread = threading.Thread(
            target=_count_repo_objects_until_done,
            args=(ostree_dir, baseline, progress, stop_counter),
            daemon=True,
        )
        stderr_thread.start()
        stdout_thread.start()
        counter_thread.start()
        try:
            returncode = process.wait()
        finally:
            stop_counter.set()
            if process.poll() is None:
                _terminate_process_group(process)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            stdout_thread.join()
            stderr_thread.join()
            counter_thread.join()

    return returncode, "".join(stdout_chunks)


def _read_stdout(process: subprocess.Popen[str], output: list[str]) -> None:
    assert process.stdout is not None
    for chunk in process.stdout:
        output.append(chunk)


def _read_ostree_stderr(process: subprocess.Popen[str], progress: object) -> None:
    assert process.stderr is not None
    for raw_line in process.stderr:
        line = raw_line.rstrip("\n")
        if line.startswith(PROGRESS_TOTAL_PREFIX):
            total = _parse_progress_total(line)
            if total is not None:
                progress.total = total
                progress.refresh()
            continue
        pstream(line)


def _parse_progress_total(line: str) -> int | None:
    value = line.removeprefix(PROGRESS_TOTAL_PREFIX).strip()
    try:
        total = int(value)
    except ValueError:
        return None
    return max(total, 0)


def _count_repo_objects_until_done(
    ostree_dir: Path,
    baseline: int,
    progress: object,
    stop: threading.Event,
) -> None:
    while not stop.wait(0.25):
        _update_object_progress(ostree_dir, baseline, progress)
    _update_object_progress(ostree_dir, baseline, progress)


def _update_object_progress(ostree_dir: Path, baseline: int, progress: object) -> None:
    imported = max(0, _count_repo_objects(ostree_dir) - baseline)
    if imported > progress.n:
        progress.update(imported - progress.n)


def _count_repo_objects(ostree_dir: Path) -> int:
    objects = ostree_dir / "objects"
    if not objects.is_dir():
        return 0
    count = 0
    for root, _dirs, files in os.walk(objects):
        if root.endswith("/tmp"):
            continue
        count += sum(
            1
            for file in files
            if file.endswith((".file", ".dirtree", ".dirmeta", ".commit"))
        )
    return count


def _parse_commit(output: str) -> str:
    commits = [
        line.strip() for line in output.splitlines() if COMMIT_RE.match(line.strip())
    ]
    if not commits:
        raise ConfigError("bootc ostree import did not emit an OSTree commit hash")
    return commits[-1]


def _require_image(podman: str, image: str, description: str) -> None:
    result = subprocess.run([podman, "image", "exists", image], check=False)
    if result.returncode != 0:
        raise ConfigError(f"{description} is not available locally: {image}")


def _image_inspect(podman: str, image: str) -> dict:
    result = subprocess.run(
        [podman, "image", "inspect", image, "--format", "{{json .}}"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return json.loads(result.stdout)


def _image_label(data: dict, key: str) -> str | None:
    labels = data.get("Labels") or {}
    value = labels.get(key)
    if value:
        return str(value)
    return None


def _image_display_ref(data: dict) -> str:
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

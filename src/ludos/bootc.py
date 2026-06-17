from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .build import _terminate_process_group
from .logging import log, piter, pstream
from .model import ConfigError


DEFAULT_CACHE_DIR = Path("cache")
DEFAULT_OSTREE_REF = "master"
SOURCE_MOUNT = "/ludos/source"
OSTREE_MOUNT = "/ludos/ostree"
POSTPROCESS_MOUNT = "/ludos/postprocess.py"
PROGRESS_TOTAL_PREFIX = "__LUDOS_OSTREE_APPROX_TOTAL__ "
COMMIT_RE = re.compile(r"^[0-9a-f]{64}$")


def ostree_import(
    ref: str,
    *,
    cache_dir: Path | None = None,
    orchestrator: str | None = None,
    ostree_ref: str = DEFAULT_OSTREE_REF,
    process: bool = True,
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
    orchestrator_display_ref = _image_display_ref(podman, orchestrator)

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
        f"type=image,source={ref},target={SOURCE_MOUNT}",
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


def _unprocessed_ostree_import_script() -> str:
    return "\n".join(
        [
            'repo="/ludos/ostree"',
            'source="/ludos/source"',
            'if [ ! -d "$repo/objects" ]; then',
            '  ostree --repo="$repo" init --mode=bare-user',
            '  ostree --repo="$repo" config set core.fsync false',
            "fi",
            'approx_entries=$(find "$source" -mindepth 1 -printf ".\\n" | wc -l)',
            'approx_total=$((approx_entries * 2 + 1))',
            f'printf "{PROGRESS_TOTAL_PREFIX}%s\\n" "$approx_total" >&2',
            'commit=$(env -u G_MESSAGES_DEBUG ostree --repo="$repo" commit -v \\',
            '  -b "$LUDOS_OSTREE_REF" \\',
            '  --tree=dir="$source" \\',
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

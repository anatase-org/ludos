from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .model import ConfigError, validate_manifest


@dataclass(frozen=True)
class BuildResult:
    image: str
    distro: str
    bootstrap: str
    output_image: str
    requested_packages: tuple[str, ...]
    resolved_packages: tuple[str, ...]
    package_blocks: tuple[tuple[str, tuple[str, ...]], ...]
    package_dir: Path
    package_list: Path
    repo_dir: Path
    podman: str
    cache_version: str
    repo_images: tuple[str, ...]
    package_images: tuple[str, ...]


def build_manifest(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
) -> BuildResult:
    validation = validate_manifest(manifest_path, cards_dir)
    if validation.missing_repos:
        missing = ", ".join(validation.missing_repos)
        raise ConfigError(f"{manifest_path}: missing repository definitions: {missing}")
    if validation.missing_cards:
        missing = ", ".join(validation.missing_cards)
        raise ConfigError(f"{manifest_path}: missing card definitions: {missing}")

    root_dir = manifest_path.resolve().parent
    image = _cache_name(manifest_path.resolve().stem, "image")
    manifest_env = {key: str(value) for key, value in validation.manifest.env.items()}
    local_values = _load_dotenv(root_dir / ".env")
    local_prefix = local_values.pop("local_prefix", validation.manifest.local_prefix)
    manifest_env.update(local_values)
    distro = _cache_name(
        _substitute_variables(validation.manifest.distro, manifest_env),
        "distro",
    )
    bootstrap = _substitute_variables(validation.manifest.bootstrap, manifest_env)
    output_image = f"localhost/ludos/{image}:{distro}"
    if cache_version is None:
        iso_today = _datetime.date.today().isocalendar()
        cache_version = f"{iso_today.year}-{iso_today.week:02d}"
        load_only_version = False
    else:
        cache_version = _cache_name(cache_version, "version")
        load_only_version = True
    local_prefix = _local_prefix(local_prefix)

    if cache_dir is None:
        cache_dir = root_dir / "cache"
    else:
        cache_dir = cache_dir.expanduser().resolve()
    package_dir = cache_dir / "packages" / distro
    dnf_dir = cache_dir / "dnf" / distro
    build_dir = cache_dir / "build" / f"{image}-{distro}"
    repo_dir = dnf_dir / "repos"
    dnf_cache_dir = dnf_dir / "cache"
    dnf_persist_dir = dnf_dir / "persist"
    dnf_log_dir = dnf_dir / "log"
    package_list = dnf_dir / f"{image}-packages.json"
    oci_dir = build_dir / "oci"
    resolve_root_dir = build_dir / "resolve-root"

    package_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)
    dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_persist_dir.mkdir(parents=True, exist_ok=True)
    dnf_log_dir.mkdir(parents=True, exist_ok=True)
    resolve_root_dir.mkdir(parents=True, exist_ok=True)

    podman = shutil.which("podman")
    if not podman:
        raise ConfigError("podman must be installed to build")

    for existing in repo_dir.glob("*.repo"):
        existing.unlink()
    shutil.rmtree(dnf_cache_dir)
    shutil.rmtree(dnf_persist_dir)
    dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_persist_dir.mkdir(parents=True, exist_ok=True)

    repo_images = []
    for repo in validation.repos:
        repo_variables = dict(manifest_env)
        for key, value in repo.ref.vars.items():
            repo_variables[key] = _substitute_variables(value, repo_variables)

        rendered_repo = _substitute_variables(
            repo.source.read_text(encoding="utf-8"),
            repo_variables,
        )
        repo_lines = rendered_repo.rstrip().splitlines()
        repo_lines.append(f"priority={repo.ref.priority}")
        repo_lines.append("metadata_expire=never")
        rendered_repo = "\n".join(repo_lines) + "\n"
        repo_id = _repo_id(rendered_repo, repo.source)
        repo_image = _local_image(local_prefix, "repos", f"{repo.ref.repo}-{cache_version}")
        repo_images.append(repo_image)
        if _image_exists(podman, repo_image):
            _extract_image_paths(
                podman,
                repo_image,
                {
                    "repos": repo_dir,
                    "cache": dnf_cache_dir,
                    "persist": dnf_persist_dir,
                },
            )
            continue
        if load_only_version:
            raise ConfigError(
                f"repository metadata image is missing for requested version: {repo_image}"
            )

        _create_repo_image(
            podman=podman,
            bootstrap=bootstrap,
            root_dir=root_dir,
            build_dir=oci_dir / "repos" / f"{repo.ref.repo}-{cache_version}",
            image=repo_image,
            repo_name=repo.source.name,
            repo_id=repo_id,
            rendered_repo=rendered_repo,
        )
        _extract_image_paths(
            podman,
            repo_image,
            {
                "repos": repo_dir,
                "cache": dnf_cache_dir,
                "persist": dnf_persist_dir,
            },
        )

    card_entries = []
    used_card_names = set()
    for insertion_order, card in enumerate(validation.cards):
        card_name = _card_name(card.source, root_dir) if card.source else "card"
        if card_name in used_card_names:
            index = 2
            while f"{card_name}-{index}" in used_card_names:
                index += 1
            card_name = f"{card_name}-{index}"
        used_card_names.add(card_name)
        card_entries.append((card.priority, insertion_order, card_name, card))

    card_entries.sort(key=lambda entry: (entry[0], entry[1]))
    requested_packages = []
    card_requests = []
    card_names = []
    card_file_sets = []
    postprocess_blocks = []
    for _priority, _insertion_order, card_name, card in card_entries:
        card_names.append(card_name)
        card_packages = []
        for package in card.packages:
            card_packages.append(package)
            requested_packages.append(package)
        card_requests.append(tuple(card_packages))
        card_file_sets.append((card_name, card.source, card.files))
        if card.postprocess.strip():
            postprocess_blocks.append((card_name, card.postprocess.rstrip()))
    requested_packages = tuple(requested_packages)
    if not requested_packages:
        raise ConfigError(f"{manifest_path}: no packages requested by cards")

    bootstrap_dnf_base = [
        podman,
        "run",
        "--rm",
        "--volume",
        f"{root_dir / 'repos'}:/workspace/repos:ro",
        "--volume",
        f"{repo_dir}:/ludos/dnf/repos:ro",
        "--volume",
        f"{dnf_cache_dir}:/ludos/dnf/cache",
        "--volume",
        f"{dnf_persist_dir}:/ludos/dnf/persist",
        "--volume",
        f"{dnf_log_dir}:/ludos/dnf/log",
        "--volume",
        f"{package_dir}:/ludos/packages",
        "--volume",
        f"{resolve_root_dir}:/ludos/resolve-root",
        "--workdir",
        "/workspace/repos",
        bootstrap,
        "dnf5",
    ]

    shutil.rmtree(resolve_root_dir, ignore_errors=True)
    resolve_root_dir.mkdir(parents=True)

    card_resolutions = []
    for card_name, card_packages in zip(card_names, card_requests):
        if not card_packages:
            card_resolutions.append(tuple())
            continue

        transaction_preview = subprocess.run(
            [
                *bootstrap_dnf_base,
                "--assumeno",
                "--setopt=reposdir=/ludos/dnf/repos",
                "--setopt=cachedir=/ludos/dnf/cache",
                "--setopt=persistdir=/ludos/dnf/persist",
                "--setopt=logdir=/ludos/dnf/log",
                "--setopt=install_weak_deps=False",
                "--disable-repo=*",
                "--enable-repo=*",
                "--installroot=/ludos/resolve-root",
                f"--releasever={manifest_env['releasever']}",
                "install",
                *card_packages,
            ],
            check=False,
            text=True,
            capture_output=True,
        )

        card_resolved_package_list = []
        in_install_section = False
        for line in (
            transaction_preview.stdout + "\n" + transaction_preview.stderr
        ).splitlines():
            stripped = line.strip()
            if stripped == "Transaction Summary:":
                break
            if not stripped:
                continue
            if stripped.startswith("Installing"):
                in_install_section = True
                continue
            if stripped.startswith("Package "):
                continue
            if not in_install_section:
                continue

            fields = stripped.split()
            if len(fields) < 4:
                continue
            package, arch, version = fields[:3]
            card_resolved_package_list.append(f"{package}-{version}.{arch}")

        if not card_resolved_package_list:
            output = transaction_preview.stdout + transaction_preview.stderr
            detail = "\n".join(output.splitlines()[-20:])
            raise ConfigError(f"dnf did not resolve packages for {card_name}:\n{detail}")
        card_resolutions.append(tuple(card_resolved_package_list))

    package_counts = {}
    for card_resolution in card_resolutions:
        for package in set(card_resolution):
            package_counts[package] = package_counts.get(package, 0) + 1

    common_package_set = {
        package for package, count in package_counts.items() if count > 1
    }
    common_package_set = _drop_minimal_provider_conflicts(common_package_set)
    seen_common_packages = set()
    package_blocks = []
    common_packages = []
    for card_resolution in card_resolutions:
        for package in card_resolution:
            if package not in common_package_set or package in seen_common_packages:
                continue
            seen_common_packages.add(package)
            common_packages.append(package)
    if common_packages:
        package_blocks.append(("common", tuple(common_packages)))
        common_hash_inputs = []
        for card_request, card_resolution in zip(card_requests, card_resolutions):
            if any(package in common_package_set for package in card_resolution):
                common_hash_inputs.extend(card_request)
        package_block_hash_inputs = [tuple(common_hash_inputs)]
    else:
        package_block_hash_inputs = []

    resolved_package_list = list(common_packages)
    selected_package_set = set(common_packages)
    for card_name, card_request, card_resolution in zip(
        card_names, card_requests, card_resolutions
    ):
        card_packages = []
        for package in card_resolution:
            if package in common_package_set:
                continue
            full_package = _full_provider_package(package)
            if full_package != package and full_package in selected_package_set:
                continue
            card_packages.append(package)
        if not card_packages:
            continue
        card_packages = tuple(card_packages)
        package_blocks.append((card_name, card_packages))
        package_block_hash_inputs.append(card_request)
        resolved_package_list.extend(card_packages)
        selected_package_set.update(card_packages)
    package_blocks = tuple(package_blocks)
    resolved_packages = tuple(resolved_package_list)
    resolved_packages = _drop_minimal_provider_conflicts(resolved_packages)
    if not resolved_packages:
        raise ConfigError("dnf did not resolve any packages")

    package_images = []
    expanded_package_blocks = []
    shutil.rmtree(resolve_root_dir, ignore_errors=True)
    resolve_root_dir.mkdir(parents=True)
    for (block_name, block_packages), hash_inputs in zip(
        package_blocks, package_block_hash_inputs
    ):
        block_hash = _package_hash(hash_inputs)
        package_image = _local_image(
            local_prefix,
            "packages",
            f"{block_name}-{block_hash}-{cache_version}",
        )
        package_images.append(package_image)
        if _image_exists(podman, package_image):
            expanded_package_blocks.append((block_name, block_packages))
            continue

        block_packages = _resolve_local_install_block(
            bootstrap_dnf_base,
            manifest_env,
            block_packages,
        )
        expanded_package_blocks.append((block_name, block_packages))
        rpm_files = _download_block_packages(bootstrap_dnf_base, block_packages)
        _install_resolved_block(bootstrap_dnf_base, manifest_env, rpm_files)
        _create_package_image(
            podman=podman,
            build_dir=oci_dir / "packages" / f"{block_name}-{block_hash}-{cache_version}",
            image=package_image,
            package_dir=package_dir,
            rpm_files=rpm_files,
        )

    package_blocks = tuple(expanded_package_blocks)
    resolved_packages = tuple(
        package
        for _block_name, block_packages in package_blocks
        for package in block_packages
    )
    package_list.write_text(
        json.dumps(
            {
                "packages": list(resolved_packages),
                "blocks": {
                    block_name: list(block_packages)
                    for block_name, block_packages in package_blocks
                },
                "version": cache_version,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    label_lines = "".join(
        f"LABEL {json.dumps(key)}={json.dumps(value)}\n"
        for key, value in validation.manifest.labels.items()
    )
    card_files_dir = build_dir / "files"
    shutil.rmtree(card_files_dir, ignore_errors=True)
    card_file_cards = set()
    for card_name, card_source, card_files in card_file_sets:
        if not card_files:
            continue
        if card_source is None:
            raise ConfigError(f"card '{card_name}' has files but no source path")
        card_source_dir = card_source.parent.resolve()
        card_context_dir = card_files_dir / _identifier(card_name)
        for file_ref in card_files:
            file_path = Path(file_ref)
            if file_path.is_absolute() or ".." in file_path.parts:
                raise ConfigError(
                    f"{card_source}: files entry '{file_ref}' must be relative to the card"
                )
            source_path = (card_source_dir / file_path).resolve()
            try:
                source_path.relative_to(card_source_dir)
            except ValueError as exc:
                raise ConfigError(
                    f"{card_source}: files entry '{file_ref}' escapes the card directory"
                ) from exc
            if not source_path.is_file():
                raise ConfigError(f"{card_source}: files entry '{file_ref}' is missing")
            target_path = card_context_dir / file_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
        card_file_cards.add(card_name)
    package_stage_lines = "".join(
        f"FROM {package_image} AS packages_{_identifier(block_name)}\n"
        for (block_name, _block_packages), package_image in zip(
            package_blocks, package_images
        )
    )
    install_steps = []
    for block_name, block_packages in package_blocks:
        package_lines = f"    /rpms/{block_name}/*.rpm \\\n"
        install_steps.append(
            f"""#
# Install: {block_name}
#

COPY --from=packages_{_identifier(block_name)} / /rpms/{block_name}/
RUN /bin/sh <<'LUDOS_INSTALL_{block_name}'
set -e
dnf5 -y \\
    --installroot=/target \\
    --releasever={manifest_env["releasever"]} \\
    --setopt=reposdir=/ludos/dnf/repos \\
    --setopt=cachedir=/ludos/dnf/cache \\
    --setopt=persistdir=/ludos/dnf/persist \\
    --setopt=logdir=/ludos/dnf/log \\
    --setopt=install_weak_deps=False \\
    --cacheonly \\
    --disable-repo='*' \\
    --enable-repo='*' \\
    --nogpgcheck \\
    install \\
    --allowerasing \\
{package_lines}    && \\
    rm -rf /target/var/cache/dnf /target/var/log/dnf*
LUDOS_INSTALL_{block_name}
"""
        )
    install_step_lines = "\n".join(install_steps)
    postprocess_steps = []
    for block_name, postprocess in postprocess_blocks:
        file_step = ""
        if block_name in card_file_cards:
            file_step = f"COPY files/{_identifier(block_name)}/ /files/\n"
        set_command = "" if _starts_with_set_command(postprocess) else "set -e\n"
        postprocess_steps.append(
            f"""#
# Postprocess: {block_name}
#

{file_step}\
RUN /bin/sh <<'LUDOS_POSTPROCESS_{block_name}'
{set_command}\
{postprocess}
rm -rf /files
LUDOS_POSTPROCESS_{block_name}
"""
        )
    postprocess_step_lines = "\n".join(postprocess_steps)
    containerfile = build_dir / "Containerfile"
    containerfile.write_text(
        f"""{package_stage_lines}FROM {bootstrap} AS install
WORKDIR /workspace/repos
RUN mkdir -p /target

#
# Install packages
#

{install_step_lines}

#
# Switch to real root
#

RUN rm -rf /target/etc/machine-id /target/var/lib/dbus/machine-id

FROM scratch
COPY --from=install /target /

#
# Run postprocessing
#

{postprocess_step_lines}
{label_lines}""",
        encoding="utf-8",
    )

    _run_container_build(
        [
            podman,
            "build",
            "--layers",
            "--pull=missing",
            "--tag",
            output_image,
            "--volume",
            f"{root_dir / 'repos'}:/workspace/repos:ro",
            "--volume",
            f"{repo_dir}:/ludos/dnf/repos:ro",
            "--volume",
            f"{dnf_cache_dir}:/ludos/dnf/cache",
            "--volume",
            f"{dnf_persist_dir}:/ludos/dnf/persist",
            "--volume",
            f"{dnf_log_dir}:/ludos/dnf/log",
            "--file",
            str(containerfile),
            str(build_dir),
        ],
        containerfile,
    )

    return BuildResult(
        image=image,
        distro=distro,
        bootstrap=bootstrap,
        output_image=output_image,
        requested_packages=requested_packages,
        resolved_packages=resolved_packages,
        package_blocks=package_blocks,
        package_dir=package_dir,
        package_list=package_list,
        repo_dir=repo_dir,
        podman=str(podman),
        cache_version=cache_version,
        repo_images=tuple(repo_images),
        package_images=tuple(package_images),
    )


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{path}:{line_number}: invalid environment key '{key}'")
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _local_prefix(value: str) -> str:
    if "/" in value or ":" in value:
        raise ConfigError(f"invalid local_prefix '{value}'")
    return value


def _local_image(local_prefix: str, repository: str, tag: str) -> str:
    return f"localhost/{local_prefix}{repository}:{tag}"


def _image_exists(podman: str, image: str) -> bool:
    return subprocess.run([podman, "image", "exists", image], check=False).returncode == 0


def _repo_id(rendered_repo: str, source: Path) -> str:
    for line in rendered_repo.splitlines():
        match = re.fullmatch(r"\[([^]]+)]", line.strip())
        if match:
            return match.group(1)
    raise ConfigError(f"{source}: repository definition does not contain a repo id")


def _create_repo_image(
    *,
    podman: str,
    bootstrap: str,
    root_dir: Path,
    build_dir: Path,
    image: str,
    repo_name: str,
    repo_id: str,
    rendered_repo: str,
) -> None:
    image_root = build_dir / "root"
    shutil.rmtree(build_dir, ignore_errors=True)
    (image_root / "repos").mkdir(parents=True)
    (image_root / "cache").mkdir()
    (image_root / "persist").mkdir()
    (build_dir / "log").mkdir()
    (image_root / "repos" / repo_name).write_text(rendered_repo, encoding="utf-8")

    subprocess.run(
        [
            podman,
            "run",
            "--rm",
            "--volume",
            f"{root_dir / 'repos'}:/workspace/repos:ro",
            "--volume",
            f"{image_root / 'repos'}:/ludos/dnf/repos:ro",
            "--volume",
            f"{image_root / 'cache'}:/ludos/dnf/cache",
            "--volume",
            f"{image_root / 'persist'}:/ludos/dnf/persist",
            "--volume",
            f"{build_dir / 'log'}:/ludos/dnf/log",
            "--workdir",
            "/workspace/repos",
            bootstrap,
            "dnf5",
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--disable-repo=*",
            f"--enable-repo={repo_id}",
            "makecache",
            "--refresh",
        ],
        check=True,
    )

    containerfile = build_dir / "Containerfile"
    containerfile.write_text("FROM scratch\nCOPY root/ /\n", encoding="utf-8")
    subprocess.run(
        [
            podman,
            "build",
            "--layers",
            "--pull=missing",
            "--tag",
            image,
            "--file",
            str(containerfile),
            str(build_dir),
        ],
        check=True,
    )


def _extract_image_paths(
    podman: str, image: str, paths: dict[str, Path]
) -> None:
    container = subprocess.run(
        [podman, "create", image, "true"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    try:
        for source_name, destination in paths.items():
            destination.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    podman,
                    "cp",
                    f"{container}:/{source_name}/.",
                    str(destination),
                ],
                check=True,
            )
    finally:
        subprocess.run([podman, "rm", container], check=True, stdout=subprocess.DEVNULL)


def _package_rpm_files(
    bootstrap_dnf_base: list[str], block_packages: tuple[str, ...]
) -> tuple[str, ...]:
    query = subprocess.run(
        [
            *bootstrap_dnf_base,
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--disable-repo=*",
            "--enable-repo=*",
            "repoquery",
            "--location",
            *block_packages,
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    rpm_files = []
    seen = set()
    for line in query.stdout.splitlines():
        filename = line.rsplit("/", 1)[-1].strip()
        if not filename.endswith(".rpm") or filename in seen:
            continue
        seen.add(filename)
        rpm_files.append(filename)
    if len(rpm_files) != len(block_packages):
        raise ConfigError(
            f"repoquery returned {len(rpm_files)} RPM locations for {len(block_packages)} packages"
        )
    return tuple(rpm_files)


def _download_block_packages(
    bootstrap_dnf_base: list[str], block_packages: tuple[str, ...]
) -> tuple[str, ...]:
    rpm_files = _package_rpm_files(bootstrap_dnf_base, block_packages)
    subprocess.run(
        [
            *bootstrap_dnf_base,
            "-y",
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--disable-repo=*",
            "--enable-repo=*",
            "download",
            "--destdir=/ludos/packages",
            *block_packages,
        ],
        check=True,
        text=True,
    )
    return rpm_files


def _resolve_local_install_block(
    bootstrap_dnf_base: list[str],
    manifest_env: dict[str, str],
    block_packages: tuple[str, ...],
) -> tuple[str, ...]:
    resolved = list(block_packages)
    seen = set(resolved)
    for _attempt in range(10):
        rpm_files = _download_block_packages(bootstrap_dnf_base, tuple(resolved))
        missing_dependencies = _preview_local_install_dependencies(
            bootstrap_dnf_base,
            manifest_env,
            rpm_files,
        )
        new_dependencies = [
            package for package in missing_dependencies if package not in seen
        ]
        if not new_dependencies:
            return tuple(resolved)
        resolved.extend(new_dependencies)
        seen.update(new_dependencies)

    raise ConfigError("local RPM install dependency resolution did not converge")


def _preview_local_install_dependencies(
    bootstrap_dnf_base: list[str],
    manifest_env: dict[str, str],
    rpm_files: tuple[str, ...],
) -> tuple[str, ...]:
    transaction_preview = subprocess.run(
        [
            *bootstrap_dnf_base,
            "--assumeno",
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--setopt=install_weak_deps=False",
            "--disable-repo=*",
            "--enable-repo=*",
            "--installroot=/ludos/resolve-root",
            f"--releasever={manifest_env['releasever']}",
            "install",
            *_rpm_paths(rpm_files),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    missing_dependencies = []
    in_install_section = False
    saw_transaction_summary = False
    for line in (
        transaction_preview.stdout + "\n" + transaction_preview.stderr
    ).splitlines():
        stripped = line.strip()
        if stripped == "Transaction Summary:":
            saw_transaction_summary = True
            break
        if not stripped:
            continue
        if stripped.startswith("Installing"):
            in_install_section = True
            continue
        if stripped.startswith("Package "):
            continue
        if not in_install_section:
            continue

        fields = stripped.split()
        if len(fields) < 4:
            continue
        package, arch, version, repository = fields[:4]
        if repository == "@commandline":
            continue
        missing_dependencies.append(f"{package}-{version}.{arch}")

    if transaction_preview.returncode != 0 and not saw_transaction_summary:
        output = transaction_preview.stdout + transaction_preview.stderr
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not preview local RPM install:\n{detail}")

    return tuple(missing_dependencies)


def _install_resolved_block(
    bootstrap_dnf_base: list[str],
    manifest_env: dict[str, str],
    rpm_files: tuple[str, ...],
) -> None:
    subprocess.run(
        [
            *bootstrap_dnf_base,
            "-y",
            "--installroot=/ludos/resolve-root",
            f"--releasever={manifest_env['releasever']}",
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--setopt=install_weak_deps=False",
            "--setopt=tsflags=justdb",
            "--disable-repo=*",
            "--enable-repo=*",
            "--nogpgcheck",
            "install",
            *_rpm_paths(rpm_files),
        ],
        check=True,
        text=True,
    )


def _rpm_paths(rpm_files: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"/ludos/packages/{rpm_file}" for rpm_file in rpm_files)


def _create_package_image(
    *,
    podman: str,
    build_dir: Path,
    image: str,
    package_dir: Path,
    rpm_files: tuple[str, ...],
) -> None:
    image_root = build_dir / "root"
    shutil.rmtree(build_dir, ignore_errors=True)
    image_root.mkdir(parents=True)
    for rpm_file in rpm_files:
        matches = list(package_dir.rglob(rpm_file))
        if not matches:
            raise ConfigError(f"downloaded RPM is missing from cache: {rpm_file}")
        target = image_root / rpm_file
        try:
            os.link(matches[0], target)
        except OSError:
            shutil.copy2(matches[0], target)

    containerfile = build_dir / "Containerfile"
    containerfile.write_text("FROM scratch\nCOPY root/ /\n", encoding="utf-8")
    subprocess.run(
        [
            podman,
            "build",
            "--layers",
            "--pull=missing",
            "--tag",
            image,
            "--file",
            str(containerfile),
            str(build_dir),
        ],
        check=True,
    )


def _package_hash(packages: tuple[str, ...]) -> str:
    payload = "\n".join(sorted(packages)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _starts_with_set_command(script: str) -> bool:
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return re.fullmatch(r"set\s+-[A-Za-z]+(?:\s+#.*)?", stripped) is not None
    return False


def _run_container_build(command: list[str], containerfile: Path) -> None:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output_lines = []
    assert process.stdout is not None
    for line in process.stdout:
        output_lines.append(line)
        print(line, end="")

    returncode = process.wait()
    if returncode == 0:
        return

    location = _containerfile_error_location(containerfile, "".join(output_lines))
    command_line = " ".join(shlex.quote(str(part)) for part in command)
    message = f"command failed with exit status {returncode}: {command_line}"
    if location is not None:
        message = f"{message}\n\nThe error occurred in:\n{location}"
    raise ConfigError(message)


def _containerfile_error_location(containerfile: Path, output: str) -> str | None:
    lines = containerfile.read_text(encoding="utf-8").splitlines()
    step_line = _last_build_step_line(lines, output)
    trace_command = _last_shell_trace_command(output)
    if step_line is not None and trace_command:
        trace_line = _find_trace_command_line(lines, trace_command, step_line)
        if trace_line is not None:
            return _format_source_location(containerfile, trace_line)
    if step_line is not None:
        return _format_source_location(containerfile, step_line)
    return None


def _last_build_step_line(lines: list[str], output: str) -> int | None:
    step = None
    for output_line in output.splitlines():
        match = re.search(r"(?:\[[^]]+]\s+)?STEP\s+\d+/\d+:\s+(.*)", output_line)
        if match:
            step = match.group(1).strip()
            continue
        match = re.search(r'building at STEP "([^"]+)"', output_line)
        if match:
            step = match.group(1).strip()
    if step is None:
        return None

    line_match = None
    for line_number, line in enumerate(lines, 1):
        if line.strip() == step:
            line_match = line_number
    return line_match


def _last_shell_trace_command(output: str) -> str | None:
    trace_command = None
    for output_line in output.splitlines():
        stripped = output_line.strip()
        if not stripped.startswith("+"):
            continue
        trace_command = stripped.lstrip("+").strip()
    return trace_command


def _find_trace_command_line(
    lines: list[str], trace_command: str, step_line: int
) -> int | None:
    for line_number, line in enumerate(lines[step_line:], step_line + 1):
        if line.strip() == trace_command:
            return line_number
    return None


def _format_source_location(path: Path, line_number: int) -> str:
    try:
        display_path = f"./{path.resolve().relative_to(Path.cwd())}"
    except ValueError:
        display_path = str(path)
    return f"{display_path}:{line_number}"


def _cache_name(value: str, description: str) -> str:
    if "/" in value or value in ("", ".", ".."):
        raise ConfigError(f"invalid {description} cache name '{value}'")
    return value


def _card_name(source: Path, root_dir: Path) -> str:
    source = source.resolve()
    try:
        relative_source = source.relative_to(root_dir)
    except ValueError:
        relative_source = source

    parts = list(relative_source.parts)
    if parts and parts[0] == "cards":
        parts = parts[1:]

    if parts and parts[-1] in ("card.yml", "card.yaml"):
        parts = parts[:-1]
    elif parts and Path(parts[-1]).suffix in (".yml", ".yaml"):
        parts[-1] = Path(parts[-1]).stem

    if len(parts) >= 2 and parts[-1] == parts[-2]:
        parts = parts[:-1]

    if not parts:
        return "card"

    return "-".join(parts)


def _drop_minimal_provider_conflicts(packages):
    package_set = set(packages)
    filtered = []
    for package in packages:
        full_package = _full_provider_package(package)
        if full_package != package and full_package in package_set:
            continue
        filtered.append(package)
    if isinstance(packages, set):
        return set(filtered)
    return tuple(filtered)


def _full_provider_package(package: str) -> str:
    return package.replace("-minimal-", "-", 1)


def _substitute_variables(value: str, variables: dict[str, str]) -> str:
    for key, replacement in variables.items():
        value = value.replace(f"${key}", replacement)
    return value

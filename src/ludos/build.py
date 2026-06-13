from __future__ import annotations

import datetime as _datetime
import fnmatch
import hashlib
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .logging import log, stream
from .model import ConfigError, validate_manifest


HASH_LENGTH = 8
BOOTSTRAP_BLOCK = "bootstrap"


@dataclass(frozen=True)
class BuildResult:
    image: str
    distro: str
    orchestrator: str
    output_image: str
    requested_packages: tuple[str, ...]
    resolved_packages: tuple[str, ...]
    package_blocks: tuple[tuple[str, tuple[str, ...]], ...]
    package_dir: Path
    repo_dir: Path
    podman: str
    cache_version: str
    repo_images: tuple[str, ...]
    package_images: tuple[str, ...]
    build_images: tuple[str, ...] = tuple()
    build_blocks: tuple[str, ...] = tuple()
    builder_images: tuple[str, ...] = tuple()


@dataclass(frozen=True)
class CardBuildOutput:
    rpm_files: tuple[str, ...] = tuple()
    file_count: int = 0
    rpm_dir: Path | None = None
    files_dir: Path | None = None


@dataclass(frozen=True)
class FileRef:
    original: str
    source: str
    target: str


def build_manifest(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    resolve_only: bool = False,
) -> BuildResult:
    log(f"Validating manifest: {manifest_path}")
    validation = validate_manifest(manifest_path, cards_dir)
    if validation.missing_bootstrap:
        raise ConfigError(
            f"{manifest_path}: missing bootstrap card: {validation.missing_bootstrap}"
        )
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
    local_prefix = _local_prefix(local_prefix)
    manifest_env.update(local_values)
    releasever = _cache_name(
        _substitute_variables(validation.manifest.releasever, manifest_env),
        "releasever",
    )
    manifest_env["releasever"] = releasever
    distro = _cache_name(
        _substitute_variables(validation.manifest.distro, manifest_env),
        "distro",
    )
    orchestrator = _substitute_variables(validation.manifest.orchestrator, manifest_env)
    output_image = f"localhost/{local_prefix}{image}:{distro}"
    if cache_version is None:
        cache_version = _datetime.date.today().strftime("%Y%m%d")
        load_only_version = False
    else:
        cache_version = _cache_name(cache_version, "version")
        load_only_version = True
    if cache_only:
        log("Using cache-only mode")

    if cache_dir is None:
        cache_dir = root_dir / "cache"
    else:
        cache_dir = cache_dir.expanduser().resolve()
    log(f"Preparing cache directories under {cache_dir}")
    distro_cache_dir = cache_dir / distro
    package_dir = distro_cache_dir / "packages"
    dnf_dir = distro_cache_dir / "dnf"
    build_dir = distro_cache_dir / "build" / image
    repo_dir = dnf_dir / "repos"
    dnf_cache_dir = dnf_dir / "cache"
    dnf_persist_dir = dnf_dir / "persist"
    dnf_log_dir = dnf_dir / "log"
    oci_dir = build_dir / "oci"
    mock_cache_dir = distro_cache_dir / "mock"
    mock_dnf_cache_dir = mock_cache_dir / "dnf"
    mock_root_cache_dir = mock_cache_dir / "root"
    build_artifact_cache_dir = distro_cache_dir / "build-artifacts"

    distro_cache_dir.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    mock_cache_dir.mkdir(parents=True, exist_ok=True)
    mock_dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    mock_root_cache_dir.mkdir(parents=True, exist_ok=True)
    build_artifact_cache_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)
    dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_persist_dir.mkdir(parents=True, exist_ok=True)
    dnf_log_dir.mkdir(parents=True, exist_ok=True)

    podman = shutil.which("podman")
    if not podman:
        raise ConfigError("podman must be installed to build")
    log(f"Using Podman: {podman}")
    buildah = shutil.which("buildah")
    if buildah:
        log(f"Using Buildah: {buildah}")

    orchestrator_image = _local_image(
        local_prefix,
        "orchestrator",
        f"{releasever}-{cache_version}",
    )
    if _image_exists(podman, orchestrator_image):
        log(f"Reusing orchestrator image: {orchestrator_image}")
    elif load_only_version or cache_only:
        raise ConfigError(f"orchestrator image is not cached: {orchestrator_image}")
    else:
        log(f"Creating orchestrator image: {orchestrator_image}")
        _create_orchestrator_image(
            podman=podman,
            source=orchestrator,
            image=orchestrator_image,
        )
    orchestrator = orchestrator_image

    log("Resetting DNF metadata workspace")
    for existing in repo_dir.glob("*.repo"):
        existing.unlink()
    shutil.rmtree(dnf_cache_dir)
    shutil.rmtree(dnf_persist_dir)
    dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_persist_dir.mkdir(parents=True, exist_ok=True)

    repo_images = []
    for repo in validation.repos:
        log(f"Rendering repository metadata: {repo.ref.repo}")
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
        repo_image = _local_image(
            local_prefix,
            "repos",
            f"{distro}-{repo.ref.repo}-{cache_version}",
        )
        repo_images.append(repo_image)
        if _image_exists(podman, repo_image):
            log(f"Reusing repository metadata image: {repo_image}")
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
        if load_only_version or cache_only:
            raise ConfigError(
                f"repository metadata image is not cached: {repo_image}"
            )

        log(f"Creating repository metadata image: {repo_image}")
        _create_repo_image(
            podman=podman,
            orchestrator=orchestrator,
            root_dir=root_dir,
            build_dir=oci_dir / "repos" / f"{distro}-{repo.ref.repo}-{cache_version}",
            image=repo_image,
            repo_name=repo.source.name,
            repo_id=repo_id,
            rendered_repo=rendered_repo,
        )
        log(f"Extracting repository metadata: {repo.ref.repo}")
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
    card_requests = []
    card_names = []
    card_file_sets = []
    card_builds = {}
    card_build_deps = {}
    card_hashes = {}
    card_envs = {}
    card_sources = {}
    postprocess_blocks = []
    bootstrap_card = validation.bootstrap
    if bootstrap_card is None:
        raise ConfigError(f"{manifest_path}: missing bootstrap card")
    if bootstrap_card.source is None:
        raise ConfigError("bootstrap card has no source path")
    if not bootstrap_card.packages:
        raise ConfigError(f"{bootstrap_card.source}: bootstrap card must define packages")
    if (
        bootstrap_card.files
        or bootstrap_card.build.strip()
        or bootstrap_card.postprocess.strip()
    ):
        raise ConfigError(
            f"{bootstrap_card.source}: bootstrap card may only define packages, env, and prepare"
        )

    bootstrap_env = _card_env(manifest_env, bootstrap_card.env)
    if bootstrap_card.prepare.strip():
        log("Preparing bootstrap card")
        prepared_env = _run_prepare_block(
            card_source=bootstrap_card.source,
            card_env=bootstrap_env,
            prepare_script=bootstrap_card.prepare.rstrip(),
        )
        bootstrap_env.update(prepared_env)

    inherited_env = dict(manifest_env)
    requested_packages = list(bootstrap_card.packages)
    for _priority, _insertion_order, card_name, card in card_entries:
        if card.source is None:
            raise ConfigError(f"card '{card_name}' has no source path")
        card_env = _card_env(inherited_env, card.env)
        if card.prepare.strip():
            log(f"Preparing card: {card_name}")
            prepared_env = _run_prepare_block(
                card_source=card.source,
                card_env=card_env,
                prepare_script=card.prepare.rstrip(),
            )
            card_env.update(prepared_env)
        inherited_env.update(card_env)
        card_names.append(card_name)
        card_envs[card_name] = card_env
        card_sources[card_name] = card.source
        card_packages = []
        for package in card.packages:
            card_packages.append(package)
            requested_packages.append(package)
        card_requests.append(tuple(card_packages))
        parsed_file_refs = tuple(_parse_file_ref(file_ref) for file_ref in card.files)
        card_file_sets.append((card_name, card.source, parsed_file_refs))
        if card.build.strip():
            card_builds[card_name] = card.build.rstrip()
            card_build_deps[card_name] = card.build_deps
        if card.hash.strip():
            card_hashes[card_name] = card.hash.strip()
        if card.postprocess.strip():
            postprocess_blocks.append((card_name, card.postprocess.rstrip()))
    requested_packages = tuple(requested_packages)
    if not requested_packages and not card_builds:
        raise ConfigError(f"{manifest_path}: no packages requested by cards")
    log(
        f"Collected {len(requested_packages)} requested packages from "
        f"bootstrap and {len(card_entries)} cards"
    )
    if card_builds:
        log(f"Collected {len(card_builds)} build cards")

    orchestrator_dnf_base = [
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
        "--workdir",
        "/workspace/repos",
        orchestrator,
        "dnf5",
    ]

    log("Resolving package transaction for bootstrap")
    bootstrap_resolved_packages = _resolve_packages(
        orchestrator_dnf_base,
        releasever,
        tuple(bootstrap_card.packages),
    )
    if not bootstrap_resolved_packages:
        raise ConfigError("dnf did not resolve packages for bootstrap")
    bootstrap_package_set = set(bootstrap_resolved_packages)

    card_resolutions = []
    for card_name, card_packages in zip(card_names, card_requests):
        if not card_packages:
            if card_name in card_builds:
                log(f"Package resolution not needed for build-only card: {card_name}")
            else:
                log(f"Skipping package resolution for package-less card: {card_name}")
            card_resolutions.append(tuple())
            continue

        log(f"Resolving package transaction for card: {card_name}")
        card_resolved_package_list = _resolve_packages(
            orchestrator_dnf_base,
            releasever,
            card_packages,
        )
        if not card_resolved_package_list:
            raise ConfigError(f"dnf did not resolve packages for {card_name}")
        card_resolutions.append(tuple(card_resolved_package_list))

    log("Resolving package transactions with prior-card context")
    contextual_requests = []
    previous_contextual_resolved = set()
    for index, (card_name, card_packages) in enumerate(zip(card_names, card_requests)):
        if not card_packages:
            continue
        contextual_requests.extend(card_packages)
        contextual_resolved = _resolve_packages(
            orchestrator_dnf_base,
            releasever,
            tuple(contextual_requests),
        )
        contextual_additions = tuple(
            package
            for package in contextual_resolved
            if package not in previous_contextual_resolved
            and package not in card_resolutions[index]
        )
        if contextual_additions:
            log(
                f"Adding {len(contextual_additions)} contextual dependencies to card: {card_name}"
            )
            card_resolutions[index] = (
                *card_resolutions[index],
                *contextual_additions,
            )
        previous_contextual_resolved = set(contextual_resolved)

    log("Grouping resolved packages into install blocks")
    package_counts = {}
    for card_resolution in card_resolutions:
        for package in set(card_resolution):
            package_counts[package] = package_counts.get(package, 0) + 1

    common_package_set = {
        package for package, count in package_counts.items() if count > 1
    }
    common_package_set = _drop_minimal_provider_conflicts(common_package_set)
    common_package_set = {
        package for package in common_package_set if package not in bootstrap_package_set
    }
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
        package_block_hashes = [_nevra_hash(tuple(common_packages))]
    else:
        package_block_hashes = []

    resolved_package_list = list(bootstrap_resolved_packages)
    resolved_package_list.extend(common_packages)
    selected_package_set = set(bootstrap_resolved_packages)
    selected_package_set.update(common_packages)
    for card_name, card_request, card_resolution in zip(
        card_names, card_requests, card_resolutions
    ):
        card_packages = []
        for package in card_resolution:
            if package in bootstrap_package_set or package in common_package_set:
                continue
            full_package = _full_provider_package(package)
            if full_package != package and full_package in selected_package_set:
                continue
            card_packages.append(package)
        if not card_packages and card_name not in card_builds:
            continue
        card_packages = tuple(card_packages)
        package_blocks.append((card_name, card_packages))
        package_block_hashes.append(_nevra_hash(card_packages))
        resolved_package_list.extend(card_packages)
        selected_package_set.update(card_packages)
    package_blocks = tuple(package_blocks)
    bootstrap_package_block = (BOOTSTRAP_BLOCK, tuple(bootstrap_resolved_packages))
    bootstrap_package_block_hash = _nevra_hash(tuple(bootstrap_resolved_packages))
    package_download_blocks = (bootstrap_package_block, *package_blocks)
    package_download_hashes = (bootstrap_package_block_hash, *package_block_hashes)
    resolved_packages = tuple(resolved_package_list)
    resolved_packages = _drop_minimal_provider_conflicts(resolved_packages)
    if not resolved_packages and not package_blocks:
        raise ConfigError("dnf did not resolve any packages")
    log(
        f"Resolved {len(resolved_packages)} packages into "
        f"{len(package_download_blocks)} install blocks"
    )

    builder_images = {}
    for card_name in card_builds:
        build_deps = _build_deps(card_build_deps.get(card_name, tuple()))
        if not build_deps:
            raise ConfigError(f"build card '{card_name}' must define build-deps")
        log(f"Resolving builder packages for card: {card_name}")
        builder_packages = _resolve_packages(
            orchestrator_dnf_base,
            releasever,
            build_deps,
        )
        if not builder_packages:
            raise ConfigError(f"dnf did not resolve build-deps for {card_name}")
        builder_hash = _nevra_hash(builder_packages)
        builder_image = _local_image(
            local_prefix,
            "builders",
            f"{distro}-{builder_hash}",
        )
        builder_images[card_name] = builder_image
        if resolve_only:
            continue
        if _image_exists(podman, builder_image):
            log(f"Reusing builder image: {builder_image}")
            continue
        if cache_only:
            raise ConfigError(f"builder image is not cached: {builder_image}")

        log(f"Creating builder image: {builder_image}")
        _create_builder_image(
            podman=podman,
            buildah=_require_buildah(buildah),
            orchestrator=orchestrator,
            root_dir=root_dir,
            repo_dir=repo_dir,
            dnf_cache_dir=dnf_cache_dir,
            dnf_persist_dir=dnf_persist_dir,
            dnf_log_dir=dnf_log_dir,
            build_dir=oci_dir / "builders" / f"{distro}-{builder_hash}",
            image=builder_image,
            releasever=releasever,
            build_packages=builder_packages,
        )

    package_images = []
    package_images_by_block = {}
    build_images = []
    build_images_by_block = {}
    for (block_name, block_packages), block_hash in zip(
        package_download_blocks, package_download_hashes
    ):
        if not block_packages:
            continue
        package_image = _local_image(
            local_prefix,
            "cards",
            f"{distro}-{block_name}-{block_hash}",
        )
        if resolve_only:
            package_images.append(package_image)
            package_images_by_block[block_name] = package_image
            continue
        if _image_exists(podman, package_image):
            log(f"Reusing card package image: {package_image}")
        elif cache_only:
            raise ConfigError(f"card package image is not cached: {package_image}")
        else:
            repo_rpm_files = _download_block_packages(
                orchestrator_dnf_base,
                block_packages,
            )
            log(f"Creating card package image: {package_image}")
            _create_package_image(
                buildah=_require_buildah(buildah),
                build_dir=oci_dir / "cards" / f"{distro}-{block_name}-{block_hash}",
                image=package_image,
                package_dir=package_dir,
                rpm_files=repo_rpm_files,
            )
        package_images.append(package_image)
        package_images_by_block[block_name] = package_image

    for block_name, block_packages in package_blocks:
        if block_name not in card_builds:
            continue
        build_hash = _card_build_hash(
            block_name,
            block_packages,
            card_hashes,
            card_envs,
            card_sources,
        )
        build_image = _local_image(
            local_prefix,
            "builds",
            f"{distro}-{block_name}-{build_hash}",
        )
        if resolve_only:
            build_images.append(build_image)
            build_images_by_block[block_name] = build_image
            continue
        if _image_exists(podman, build_image):
            log(f"Reusing build output image: {build_image}")
            build_images.append(build_image)
            build_images_by_block[block_name] = build_image
            continue
        if cache_only:
            raise ConfigError(f"build output image is not cached: {build_image}")

        log(f"Running build for card: {block_name} (:{_image_tag(build_image)})")
        build_output = _run_card_build(
            podman=podman,
            orchestrator=builder_images[block_name],
            build_dir=build_dir / "build" / _identifier(block_name),
            mock_dir=mock_cache_dir / _identifier(block_name),
            mock_dnf_dir=mock_dnf_cache_dir,
            mock_root_cache_dir=mock_root_cache_dir,
            artifact_cache_dir=build_artifact_cache_dir / _identifier(block_name),
            card_name=block_name,
            card_source=card_sources[block_name],
            card_env=card_envs[block_name],
            build_script=card_builds[block_name],
        )
        if not build_output.rpm_files and build_output.file_count == 0:
            log(f"No build outputs found for card: {block_name}")
            continue

        log(f"Creating build output image: {build_image}")
        _create_build_output_image(
            buildah=_require_buildah(buildah),
            build_dir=oci_dir / "builds" / f"{distro}-{block_name}-{build_hash}",
            image=build_image,
            rpm_dir=build_output.rpm_dir,
            files_dir=build_output.files_dir,
        )
        build_images.append(build_image)
        build_images_by_block[block_name] = build_image

    expanded_package_blocks = []
    for block_name, block_packages in package_blocks:
        if block_name not in package_images_by_block and block_name not in build_images_by_block:
            log(f"No RPMs found for block, skipping install block: {block_name}")
            continue
        expanded_package_blocks.append((block_name, block_packages))

    package_blocks = tuple(expanded_package_blocks)
    final_package_blocks = (bootstrap_package_block, *package_blocks)
    resolved_packages = tuple(
        package
        for _block_name, block_packages in final_package_blocks
        for package in block_packages
    )

    if resolve_only:
        return BuildResult(
            image=image,
            distro=distro,
            orchestrator=orchestrator,
            output_image=output_image,
            requested_packages=requested_packages,
            resolved_packages=resolved_packages,
            package_blocks=final_package_blocks,
            package_dir=package_dir,
            repo_dir=repo_dir,
            podman=str(podman),
            cache_version=cache_version,
            repo_images=tuple(repo_images),
            package_images=tuple(package_images),
            build_images=tuple(build_images),
            build_blocks=tuple(build_images_by_block),
            builder_images=tuple(builder_images.values()),
        )

    label_lines = "".join(
        f"LABEL {json.dumps(key)}={json.dumps(value)}\n"
        for key, value in validation.manifest.labels.items()
    )
    card_files_dir = build_dir / "files"
    log("Staging card files")
    shutil.rmtree(card_files_dir, ignore_errors=True)
    card_file_cards = set()
    for card_name, card_source, file_refs in card_file_sets:
        if not file_refs:
            continue
        if card_source is None:
            raise ConfigError(f"card '{card_name}' has files but no source path")
        card_source_dir = card_source.parent.resolve()
        card_context_dir = card_files_dir / _identifier(card_name)
        staged_file_count = 0
        git_cache_dir = build_dir / "file-sources" / _identifier(card_name)
        for file_ref in file_refs:
            target_relpath = _validate_relative_file_path(
                file_ref.target,
                card_source,
                "files destination",
            )
            target_path = card_context_dir / target_relpath
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if _is_http_source(file_ref.source):
                _download_file_source(file_ref.source, target_path)
            elif _is_git_source(file_ref.source):
                _copy_git_file_source(
                    file_ref.source,
                    target_path,
                    git_cache_dir / target_relpath,
                )
            else:
                source_relpath = _validate_relative_file_path(
                    file_ref.source,
                    card_source,
                    "files source",
                )
                source_path = (card_source_dir / source_relpath).resolve()
                try:
                    source_path.relative_to(card_source_dir)
                except ValueError as exc:
                    raise ConfigError(
                        f"{card_source}: files entry '{file_ref.original}' escapes the card directory"
                    ) from exc
                if not source_path.is_file():
                    raise ConfigError(
                        f"{card_source}: files entry '{file_ref.original}' is missing"
                    )
                shutil.copy2(source_path, target_path)
            staged_file_count += 1
        card_file_cards.add(card_name)
        log(f"Staged {staged_file_count} files for card: {card_name}")

    log(f"Generating Containerfile: {build_dir / 'Containerfile'}")
    package_stage_names = {
        block_name: f"cards_{_identifier(block_name)}"
        for block_name, _block_packages in final_package_blocks
        if block_name in package_images_by_block
    }
    build_stage_names = {
        block_name: f"builds_{_identifier(block_name)}"
        for block_name, _block_packages in package_blocks
        if block_name in build_images_by_block
    }
    package_stage_lines = "".join(
        f"FROM {package_images_by_block[block_name]} AS {package_stage_names[block_name]}\n"
        for block_name, _block_packages in final_package_blocks
        if block_name in package_images_by_block
    )
    build_stage_lines = "".join(
        f"FROM {build_images_by_block[block_name]} AS {build_stage_names[block_name]}\n"
        for block_name, _block_packages in package_blocks
        if block_name in build_images_by_block
    )
    install_steps = []
    bootstrap_stage_name = package_stage_names[BOOTSTRAP_BLOCK]
    bootstrap_copy_lines = (
        f"COPY --from={bootstrap_stage_name} /rpms/ /rpms/{BOOTSTRAP_BLOCK}/packages/\n"
    )
    bootstrap_package_globs = f"/rpms/{BOOTSTRAP_BLOCK}/packages/*.rpm"
    bootstrap_step = f"""FROM {orchestrator} AS bootstrap
WORKDIR /workspace/repos
RUN mkdir -p /target

#
# Bootstrap root
#

{bootstrap_copy_lines}\
RUN /bin/sh <<'LUDOS_BOOTSTRAP'
set -e
rpm_args=
for rpm in {bootstrap_package_globs}; do
    [ -e "$rpm" ] || continue
    rpm_args="$rpm_args $rpm"
done
[ -n "$rpm_args" ] || exit 0
dnf5 -y \\
    --installroot=/target \\
    --releasever={releasever} \\
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
    $rpm_args \\
    && \\
    rm -rf /target/var/cache/dnf /target/var/log/dnf*
LUDOS_BOOTSTRAP
"""
    for block_name, block_packages in package_blocks:
        rpm_sources = []
        if block_name in package_stage_names:
            rpm_sources.append(("packages", package_stage_names[block_name]))
        if block_name in build_stage_names:
            rpm_sources.append(("build", build_stage_names[block_name]))
        if not rpm_sources:
            continue
        copy_lines = "".join(
            f"COPY --from={stage_name} /rpms/ /rpms/{block_name}/{source_name}/\n"
            for source_name, stage_name in rpm_sources
        )
        package_globs = " ".join(
            f"/rpms/{block_name}/{source_name}/*.rpm"
            for source_name, _stage_name in rpm_sources
        )
        install_steps.append(
            f"""#
# Install: {block_name}
#

{copy_lines}\
RUN /bin/sh <<'LUDOS_INSTALL_{block_name}'
set -e
rpm_args=
for rpm in {package_globs}; do
    [ -e "$rpm" ] || continue
    rpm_args="$rpm_args $rpm"
done
[ -n "$rpm_args" ] || exit 0
dnf5 -y \\
    --releasever={releasever} \\
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
    $rpm_args \\
    && \\
    rm -rf /var/cache/dnf /var/log/dnf*
LUDOS_INSTALL_{block_name}
"""
        )
    install_step_lines = "\n".join(install_steps)
    postprocess_steps = []
    for block_name, postprocess in postprocess_blocks:
        file_steps = []
        if block_name in build_stage_names:
            file_steps.append(
                f"COPY --from={build_stage_names[block_name]} /files/ /files/\n"
            )
        if block_name in card_file_cards:
            file_steps.append(f"COPY files/{_identifier(block_name)}/ /files/\n")
        file_step = "".join(file_steps)
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
        f"""{package_stage_lines}{build_stage_lines}{bootstrap_step}
FROM scratch AS install
COPY --from=bootstrap /target /
WORKDIR /workspace/repos

#
# Install packages
#

{install_step_lines}

#
# Normalize root
#

RUN rm -rf /etc/machine-id /var/lib/dbus/machine-id

#
# Run postprocessing
#

{postprocess_step_lines}
{label_lines}""",
        encoding="utf-8",
    )

    log(f"Building final image: {output_image}")
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
        orchestrator=orchestrator,
        output_image=output_image,
        requested_packages=requested_packages,
        resolved_packages=resolved_packages,
        package_blocks=final_package_blocks,
        package_dir=package_dir,
        repo_dir=repo_dir,
        podman=str(podman),
        cache_version=cache_version,
        repo_images=tuple(repo_images),
        package_images=tuple(package_images),
        build_images=tuple(build_images),
        build_blocks=tuple(build_images_by_block),
        builder_images=tuple(builder_images.values()),
    )


def resolve_manifest_images(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
) -> BuildResult:
    return build_manifest(
        manifest_path,
        cards_dir=cards_dir,
        cache_dir=cache_dir,
        cache_version=cache_version,
        cache_only=True,
        resolve_only=True,
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


def _image_tag(image: str) -> str:
    return image.rsplit(":", 1)[-1]


def _image_exists(podman: str, image: str) -> bool:
    return subprocess.run([podman, "image", "exists", image], check=False).returncode == 0


def _require_buildah(buildah: str | None) -> str:
    if buildah is None:
        raise ConfigError("buildah must be installed to create card/build output images")
    return buildah


def _create_orchestrator_image(*, podman: str, source: str, image: str) -> None:
    returncode, _output = _run_streamed_command([podman, "pull", source])
    if returncode != 0:
        raise ConfigError(f"failed to pull orchestrator image: {source}")
    subprocess.run([podman, "tag", source, image], check=True)


def _repo_id(rendered_repo: str, source: Path) -> str:
    for line in rendered_repo.splitlines():
        match = re.fullmatch(r"\[([^]]+)]", line.strip())
        if match:
            return match.group(1)
    raise ConfigError(f"{source}: repository definition does not contain a repo id")


def _build_deps(card_build_deps: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(card_build_deps))


def _create_builder_image(
    *,
    podman: str,
    buildah: str,
    orchestrator: str,
    root_dir: Path,
    repo_dir: Path,
    dnf_cache_dir: Path,
    dnf_persist_dir: Path,
    dnf_log_dir: Path,
    build_dir: Path,
    image: str,
    releasever: str,
    build_packages: tuple[str, ...],
) -> None:
    _remove_tree(build_dir, podman=podman)
    image_root = build_dir / "root"
    image_root.mkdir(parents=True)
    _run_logged_command(
        [
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
            f"{image_root}:/target",
            "--workdir",
            "/workspace/repos",
            orchestrator,
            "dnf5",
            "-y",
            "--installroot=/target",
            f"--releasever={releasever}",
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--setopt=install_weak_deps=False",
            "--disable-repo=*",
            "--enable-repo=*",
            "install",
            "--allowerasing",
            *build_packages,
        ],
        "builder root bootstrap",
    )
    shutil.rmtree(image_root / "var/cache/dnf", ignore_errors=True)
    for log_path in (image_root / "var/log").glob("dnf*"):
        if log_path.is_dir():
            shutil.rmtree(log_path, ignore_errors=True)
        else:
            log_path.unlink(missing_ok=True)
    _create_scratch_image(buildah=buildah, image_root=image_root, image=image)
    _remove_tree(build_dir, podman=podman)


def _create_repo_image(
    *,
    podman: str,
    orchestrator: str,
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

    _run_logged_command(
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
            orchestrator,
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
        "repository metadata refresh",
    )

    containerfile = build_dir / "Containerfile"
    containerfile.write_text("FROM scratch\nCOPY root/ /\n", encoding="utf-8")
    _run_logged_command(
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
        "repository metadata image build",
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


def _resolve_packages(
    orchestrator_dnf_base: list[str],
    releasever: str,
    packages: tuple[str, ...],
) -> tuple[str, ...]:
    transaction_preview = subprocess.run(
        [
            *orchestrator_dnf_base,
            "--assumeno",
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--setopt=install_weak_deps=False",
            "--disable-repo=*",
            "--enable-repo=*",
            "--installroot=/ludos/resolve-root",
            f"--releasever={releasever}",
            "install",
            *packages,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    output = transaction_preview.stdout + "\n" + transaction_preview.stderr
    if transaction_preview.returncode not in (0, 1):
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not resolve packages:\n{detail}")
    return _parse_resolved_packages(output)


def _parse_resolved_packages(output: str) -> tuple[str, ...]:
    resolved_packages = []
    in_install_section = False
    for line in output.splitlines():
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
        resolved_packages.append(f"{package}-{version}.{arch}")
    return tuple(resolved_packages)


def _package_rpm_files(
    orchestrator_dnf_base: list[str], block_packages: tuple[str, ...]
) -> tuple[str, ...]:
    query = subprocess.run(
        [
            *orchestrator_dnf_base,
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
    orchestrator_dnf_base: list[str], block_packages: tuple[str, ...]
) -> tuple[str, ...]:
    if not block_packages:
        return tuple()
    rpm_files = _package_rpm_files(orchestrator_dnf_base, block_packages)
    _run_logged_command(
        [
            *orchestrator_dnf_base,
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
        "package download",
    )
    return rpm_files


def _create_package_image(
    *,
    buildah: str,
    build_dir: Path,
    image: str,
    package_dir: Path,
    rpm_files: tuple[str, ...],
    files_dir: Path | None = None,
) -> None:
    image_root = build_dir / "root"
    image_rpm_dir = image_root / "rpms"
    image_files_dir = image_root / "files"
    shutil.rmtree(build_dir, ignore_errors=True)
    image_rpm_dir.mkdir(parents=True)
    image_files_dir.mkdir(parents=True)
    for rpm_file in rpm_files:
        matches = list(package_dir.rglob(rpm_file))
        if not matches:
            raise ConfigError(f"downloaded RPM is missing from cache: {rpm_file}")
        target = image_rpm_dir / rpm_file
        try:
            os.link(matches[0], target)
        except OSError:
            shutil.copy2(matches[0], target)

    if files_dir is not None and files_dir.exists():
        for source_path in files_dir.rglob("*"):
            if source_path.is_dir():
                continue
            relative = source_path.relative_to(files_dir)
            target = image_files_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)

    _create_scratch_image(buildah=buildah, image_root=image_root, image=image)
    _remove_tree(build_dir)


def _create_build_output_image(
    *,
    buildah: str,
    build_dir: Path,
    image: str,
    rpm_dir: Path | None,
    files_dir: Path | None,
) -> None:
    image_root = build_dir / "root"
    image_rpm_dir = image_root / "rpms"
    image_files_dir = image_root / "files"
    shutil.rmtree(build_dir, ignore_errors=True)
    image_rpm_dir.mkdir(parents=True)
    image_files_dir.mkdir(parents=True)

    if rpm_dir is not None and rpm_dir.exists():
        for source_path in rpm_dir.rglob("*.rpm"):
            if source_path.name.endswith(".src.rpm"):
                continue
            target = image_rpm_dir / source_path.name
            try:
                os.link(source_path, target)
            except OSError:
                shutil.copy2(source_path, target)

    if files_dir is not None and files_dir.exists():
        for source_path in files_dir.rglob("*"):
            if source_path.is_dir():
                continue
            relative = source_path.relative_to(files_dir)
            target = image_files_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)

    _create_scratch_image(buildah=buildah, image_root=image_root, image=image)
    _remove_tree(build_dir)


def _create_scratch_image(*, buildah: str, image_root: Path, image: str) -> None:
    container = subprocess.run(
        [buildah, "from", "--quiet", "scratch"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    try:
        subprocess.run(
            [buildah, "copy", "--quiet", container, f"{image_root}/.", "/"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                buildah,
                "commit",
                "--rm",
                "--quiet",
                "--format",
                "oci",
                container,
                image,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        container = ""
    finally:
        if container:
            subprocess.run(
                [buildah, "rm", container],
                check=False,
                stdout=subprocess.DEVNULL,
            )


def _run_prepare_block(
    *,
    card_source: Path,
    card_env: dict[str, str],
    prepare_script: str,
) -> dict[str, str]:
    card_base_dir = _card_base_dir(card_source)
    with tempfile.TemporaryDirectory(prefix="ludos-prepare-") as temp_dir:
        env_file = Path(temp_dir) / "env"
        env = dict(os.environ)
        env.update(card_env)
        env["LUDOS_ENV"] = str(env_file)
        returncode, _output = _run_streamed_command(
            ["/bin/sh", "-s"],
            input_text=prepare_script + "\n",
            cwd=card_base_dir,
            env=env,
        )
        if returncode != 0:
            raise ConfigError(
                f"card prepare failed with exit status {returncode}: {card_source}"
            )
        values = _load_dotenv(env_file)
        if values:
            card_name = _card_name(card_source, Path.cwd())
            log(f"Prepared {len(values)} environment values for card: {card_name}")
        return values


def _run_card_build(
    *,
    podman: str,
    orchestrator: str,
    build_dir: Path,
    mock_dir: Path,
    mock_dnf_dir: Path,
    mock_root_cache_dir: Path,
    artifact_cache_dir: Path,
    card_name: str,
    card_source: Path,
    card_env: dict[str, str],
    build_script: str,
) -> CardBuildOutput:
    card_base_dir = _card_base_dir(card_source)
    workspace_dir = build_dir / "workspace"
    rpm_dir = build_dir / "rpms"
    files_dir = build_dir / "files"
    _remove_tree(build_dir, podman=podman)
    workspace_dir.mkdir(parents=True)
    rpm_dir.mkdir(parents=True)
    files_dir.mkdir(parents=True)
    mock_dir.mkdir(parents=True, exist_ok=True)
    mock_dnf_dir.mkdir(parents=True, exist_ok=True)
    mock_root_cache_dir.mkdir(parents=True, exist_ok=True)
    artifact_cache_dir.mkdir(parents=True, exist_ok=True)
    podman_cache_dir = artifact_cache_dir / "podman"
    podman_cache_dir.mkdir(parents=True, exist_ok=True)

    ignore_rules = _load_containerignore(card_base_dir)
    _copy_build_context(card_base_dir, workspace_dir, ignore_rules)

    command = [
        podman,
        "run",
        "--rm",
        "--interactive",
        "--privileged",
        "--volume",
        f"{workspace_dir}:/workspace",
        "--volume",
        f"{rpm_dir}:/rpms",
        "--volume",
        f"{files_dir}:/files",
        "--volume",
        f"{mock_dir}:/workspace/build/MOCK",
        "--volume",
        f"{mock_dnf_dir}:/cache/dnf",
        "--volume",
        f"{mock_root_cache_dir}:/cache/mock",
        "--volume",
        f"{artifact_cache_dir}:/cache/artifacts",
        "--volume",
        f"{podman_cache_dir}:/cache/podman",
        "--workdir",
        "/workspace",
    ]
    for key, value in sorted(card_env.items()):
        command.extend(["--env", f"{key}={value}"])
    command.extend(["--env", "PS4=+ "])
    command.extend([orchestrator, "/bin/sh", "-ex", "-s"])
    returncode, _output = _run_streamed_command(
        command,
        input_text=build_script + "\n",
        line_rewriter=_workspace_path_rewriter(
            source_dir=card_base_dir,
            workspace_dir=workspace_dir,
            root_dir=Path.cwd(),
        ),
    )
    if returncode != 0:
        command_line = " ".join(shlex.quote(str(part)) for part in command)
        raise ConfigError(f"card build failed with exit status {returncode}: {command_line}")

    rpm_files = []
    rpm_sources = sorted(rpm_dir.rglob("*.rpm"))
    if not rpm_sources:
        rpm_sources = sorted((workspace_dir / "build" / "RPMS").rglob("*.rpm"))
    for rpm_path in rpm_sources:
        if rpm_path.name.endswith(".src.rpm"):
            continue
        rpm_files.append(rpm_path.name)

    log(f"Collected {len(rpm_files)} built RPMs for card: {card_name}")
    file_count = sum(1 for path in files_dir.rglob("*") if path.is_file())
    if file_count:
        log(f"Collected {file_count} built files for card: {card_name}")
    return CardBuildOutput(
        rpm_files=tuple(rpm_files),
        file_count=file_count,
        rpm_dir=rpm_dir,
        files_dir=files_dir,
    )


def _package_hash(packages: tuple[str, ...]) -> str:
    payload = "\n".join(sorted(packages)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def _nevra_hash(nevras: tuple[str, ...]) -> str:
    return _package_hash(nevras)


def _card_build_hash(
    card_name: str,
    card_packages: tuple[str, ...],
    card_hashes: dict[str, str],
    card_envs: dict[str, str],
    card_sources: dict[str, Path],
) -> str:
    if card_name not in card_hashes:
        return _package_hash(card_packages)
    return _cache_name(
        _expand_expression(
            card_hashes[card_name],
            card_envs[card_name],
            _card_base_dir(card_sources[card_name]),
        ),
        f"{card_name} build hash",
    )


def _parse_file_ref(value: str) -> FileRef:
    if "::" not in value:
        source = value.strip()
        if _is_remote_file_source(source):
            raise ConfigError(
                f"remote files entry '{value}' must use '<destination>::<source>'"
            )
        return FileRef(original=value, source=source, target=source)

    target, source = (part.strip() for part in value.split("::", 1))
    if not target or not source:
        raise ConfigError(f"files entry '{value}' must be '<destination>::<source>'")
    return FileRef(original=value, source=source, target=target)


def _is_remote_file_source(source: str) -> bool:
    return _is_http_source(source) or _is_git_source(source)


def _is_http_source(source: str) -> bool:
    return source.startswith(("https://", "http://"))


def _is_git_source(source: str) -> bool:
    return source.startswith(("git+https://", "git+http://", "git+ssh://"))


def _validate_relative_file_path(value: str, source: Path, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ConfigError(
            f"{source}: {label} '{value}' must be a relative path without '..'"
        )
    return path


def _download_file_source(source: str, target: Path) -> None:
    log(f"Downloading file source: {source}")
    try:
        with urllib.request.urlopen(source) as response:
            with target.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except OSError as exc:
        raise ConfigError(f"failed to download file source '{source}': {exc}") from exc


def _copy_git_file_source(source: str, target: Path, cache_dir: Path) -> None:
    git = shutil.which("git")
    if not git:
        raise ConfigError("git must be installed to use git files sources")

    repo_url, ref, repo_path = _parse_git_file_source(source)
    source_dir = cache_dir
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    if not _is_git_repository(git, source_dir):
        shutil.rmtree(source_dir, ignore_errors=True)
        log(f"Initializing git file source: {source}")
        _run_logged_command([git, "init", str(source_dir)], "git file source init")
        _run_logged_command(
            [git, "-C", str(source_dir), "remote", "add", "origin", repo_url],
            "git file source remote setup",
        )
    else:
        log(f"Updating git file source: {source}")
        _run_logged_command(
            [git, "-C", str(source_dir), "remote", "set-url", "origin", repo_url],
            "git file source remote update",
        )
    _run_logged_command(
        [
            git,
            "-C",
            str(source_dir),
            "fetch",
            "--depth=1",
            "--filter=blob:none",
            "--prune",
            "origin",
            _git_fetch_ref(ref),
        ],
        "git file source fetch",
    )
    if repo_path != Path("."):
        raise ConfigError(f"git files source '{source}' does not support subpaths yet")
    if target.exists():
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    target.mkdir(parents=True)
    _run_logged_command(
        [
            git,
            "-C",
            str(source_dir),
            "--work-tree",
            str(target),
            "checkout",
            "--force",
            "FETCH_HEAD",
            "--",
            ".",
        ],
        "git file source checkout",
    )


def _is_git_repository(git: str, path: Path) -> bool:
    if not (path / ".git").is_dir():
        return False
    result = subprocess.run(
        [git, "-C", str(path), "rev-parse", "--show-toplevel"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return False
    return Path(result.stdout.strip()).resolve() == path.resolve()


def _run_logged_command(command: list[str], description: str) -> None:
    returncode, _output = _run_streamed_command(command)
    if returncode == 0:
        return
    command_line = " ".join(shlex.quote(str(part)) for part in command)
    raise ConfigError(f"{description} failed with exit status {returncode}: {command_line}")


def _parse_git_file_source(source: str) -> tuple[str, tuple[str, str], Path]:
    raw_source = source.removeprefix("git+")
    parsed = urllib.parse.urlsplit(raw_source)
    if parsed.scheme not in ("https", "http", "ssh"):
        raise ConfigError(f"unsupported git files source protocol in '{source}'")
    if parsed.fragment:
        ref_expr = parsed.fragment
        if ":" in ref_expr:
            raise ConfigError(f"git files source '{source}' must not include a subpath")
        if "=" not in ref_expr:
            raise ConfigError(f"git files source '{source}' has invalid ref selector")
        ref_kind, ref_value = ref_expr.split("=", 1)
        if ref_kind not in ("commit", "tag", "branch", "ref") or not ref_value:
            raise ConfigError(f"git files source '{source}' has invalid ref selector")
        ref = (ref_kind, ref_value)
    else:
        ref = ("default", "")
    repo_url = urllib.parse.urlunsplit(parsed._replace(fragment=""))
    return repo_url, ref, Path(".")


def _git_fetch_ref(ref: tuple[str, str]) -> str:
    ref_kind, ref_value = ref
    if ref_kind == "default":
        return "HEAD"
    if ref_kind == "branch":
        return f"refs/heads/{ref_value}"
    if ref_kind == "tag":
        return f"refs/tags/{ref_value}"
    return ref_value


def _card_env(
    manifest_env: dict[str, str], card_env: dict[str, str | int]
) -> dict[str, str]:
    values = dict(manifest_env)
    for key, value in card_env.items():
        values[key] = _expand_expression(str(value), values, None)
    return values


def _expand_expression(
    value: str, variables: dict[str, str], base_dir: Path | None
) -> str:
    value = _substitute_variables(value, variables)

    def replace_hash(match: re.Match[str]) -> str:
        if base_dir is None:
            raise ConfigError("@hash() requires a base directory")
        paths = tuple(
            item.strip()
            for item in match.group(1).split(",")
            if item.strip()
        )
        if not paths:
            raise ConfigError("@hash() requires at least one path")
        return _hash_paths(base_dir, paths)

    return re.sub(r"@hash\(([^)]*)\)", replace_hash, value)


def _hash_paths(base_dir: Path, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    ignore_rules = _load_containerignore(base_dir)
    files = []
    for path_value in paths:
        path = Path(path_value)
        if path.is_absolute() or ".." in path.parts:
            raise ConfigError(f"@hash path '{path_value}' must stay inside the card")
        source = (base_dir / path).resolve()
        try:
            source.relative_to(base_dir.resolve())
        except ValueError as exc:
            raise ConfigError(f"@hash path '{path_value}' escapes the card") from exc
        if not source.exists():
            raise ConfigError(f"@hash path '{path_value}' does not exist")
        if source.is_file():
            relative = source.relative_to(base_dir).as_posix()
            if not _ignored_by_containerignore(relative, False, ignore_rules):
                files.append(source)
            continue
        for file_path in source.rglob("*"):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(base_dir).as_posix()
            if _ignored_by_containerignore(relative, False, ignore_rules):
                continue
            files.append(file_path)

    for file_path in sorted(set(files), key=lambda item: item.relative_to(base_dir).as_posix()):
        relative = file_path.relative_to(base_dir).as_posix()
        try:
            contents = file_path.read_bytes()
        except FileNotFoundError:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(contents)
        digest.update(b"\0")
    return digest.hexdigest()[:HASH_LENGTH]


def _copy_build_context(
    source_dir: Path, destination_dir: Path, ignore_rules: tuple["_IgnoreRule", ...]
) -> None:
    destination_dir = destination_dir.resolve()
    for source_path in source_dir.rglob("*"):
        try:
            source_path.resolve().relative_to(destination_dir)
            continue
        except ValueError:
            pass
        relative = source_path.relative_to(source_dir).as_posix()
        is_dir = source_path.is_dir()
        if _ignored_by_containerignore(relative, is_dir, ignore_rules):
            if is_dir:
                continue
            continue
        target_path = destination_dir / relative
        if is_dir:
            target_path.mkdir(parents=True, exist_ok=True)
            continue
        if source_path.is_file():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)


@dataclass(frozen=True)
class _IgnoreRule:
    pattern: str
    negated: bool
    directory_only: bool
    anchored: bool


def _load_containerignore(base_dir: Path) -> tuple[_IgnoreRule, ...]:
    path = base_dir / ".containerignore"
    if not path.exists():
        return tuple()
    rules = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        negated = stripped.startswith("!")
        if negated:
            stripped = stripped[1:]
        anchored = stripped.startswith("/")
        stripped = stripped.lstrip("/")
        directory_only = stripped.endswith("/")
        stripped = stripped.rstrip("/")
        if not stripped:
            continue
        rules.append(
            _IgnoreRule(
                pattern=stripped,
                negated=negated,
                directory_only=directory_only,
                anchored=anchored,
            )
        )
    return tuple(rules)


def _ignored_by_containerignore(
    relative_path: str, is_dir: bool, rules: tuple[_IgnoreRule, ...]
) -> bool:
    if (
        relative_path == ".git"
        or relative_path.startswith(".git/")
        or relative_path.endswith("/.git")
        or "/.git/" in relative_path
    ):
        return True
    ignored = False
    for rule in rules:
        if _ignore_rule_matches(rule, relative_path, is_dir):
            ignored = not rule.negated
    return ignored


def _ignore_rule_matches(rule: _IgnoreRule, relative_path: str, is_dir: bool) -> bool:
    path = relative_path.rstrip("/")
    pattern = rule.pattern
    if rule.directory_only and is_dir:
        return _ignore_path_matches(rule, path, pattern)
    if rule.directory_only:
        return _ignore_path_matches(rule, path, pattern) or any(
            _ignore_path_matches(rule, parent, pattern)
            for parent in _parent_paths(path)
        )
    return _ignore_path_matches(rule, path, pattern)


def _ignore_path_matches(rule: _IgnoreRule, path: str, pattern: str) -> bool:
    if rule.anchored or "/" in pattern:
        return (
            fnmatch.fnmatch(path, pattern)
            or path == pattern
            or path.startswith(f"{pattern}/")
        )
    return any(fnmatch.fnmatch(part, pattern) for part in path.split("/"))


def _parent_paths(path: str) -> tuple[str, ...]:
    parts = path.split("/")[:-1]
    return tuple("/".join(parts[:index]) for index in range(1, len(parts) + 1))


def _card_base_dir(source: Path) -> Path:
    if source.name in ("card.yml", "card.yaml"):
        return source.parent.resolve()
    return source.parent.resolve()


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
    returncode, output = _run_streamed_command(command)
    if returncode == 0:
        return

    location = _containerfile_error_location(containerfile, output)
    command_line = " ".join(shlex.quote(str(part)) for part in command)
    message = f"command failed with exit status {returncode}: {command_line}"
    if location is not None:
        message = f"{message}\n\nThe error occurred in:\n{location}"
    raise ConfigError(message)


def _run_streamed_command(
    command: list[str],
    input_text: str | None = None,
    line_rewriter: Callable[[str], str] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    output_lines = []
    try:
        if input_text is not None:
            assert process.stdin is not None
            process.stdin.write(input_text)
            process.stdin.close()

        assert process.stdout is not None
        for line in process.stdout:
            output_lines.append(line)
            if line_rewriter is not None:
                line = line_rewriter(line)
            stream(line)

        return process.wait(), "".join(output_lines)
    finally:
        if process.poll() is None:
            _terminate_process_group(process)


def _workspace_path_rewriter(
    *,
    source_dir: Path,
    workspace_dir: Path,
    root_dir: Path,
) -> Callable[[str], str]:
    source = _display_path(source_dir, root_dir)
    workspace = _display_path(workspace_dir, root_dir)

    def rewrite(line: str) -> str:
        line = re.sub(
            r"(?<![A-Za-z0-9_./-])/workspace/build(?=/?|\W)",
            f"{workspace}/build",
            line,
        )
        return re.sub(
            r"(?<![A-Za-z0-9_./-])/workspace(?=/?|\W)",
            source,
            line,
        )

    return rewrite


def _display_path(path: Path, root_dir: Path) -> str:
    try:
        return f"./{path.relative_to(root_dir)}"
    except ValueError:
        return str(path)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _remove_tree(path: Path, *, podman: str | None = None) -> None:
    shutil.rmtree(path, ignore_errors=True)
    if not path.exists():
        return
    if podman is not None:
        subprocess.run([podman, "unshare", "rm", "-rf", str(path)], check=False)
    if path.exists():
        raise ConfigError(f"failed to remove cache directory: {path}")


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

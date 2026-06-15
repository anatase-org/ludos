from __future__ import annotations

import datetime as _datetime
import fnmatch
import glob
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
from .model import ConfigError, SpecBuild, validate_manifest


HASH_LENGTH = 8
RPM_ARCH_SUFFIXES = frozenset(
    (
        "aarch64",
        "armv7hl",
        "i386",
        "i486",
        "i586",
        "i686",
        "noarch",
        "ppc64le",
        "riscv64",
        "s390x",
        "x86_64",
    )
)


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
class PackageImagePlan:
    block: str
    packages: tuple[str, ...]
    image: str


@dataclass(frozen=True)
class BuildImagePlan:
    block: str
    image: str
    builder_image: str
    builder_packages: tuple[str, ...]
    declared_package_ids: tuple[tuple[str, str], ...] = tuple()


@dataclass(frozen=True)
class BuildImageOutputs:
    images_by_block: tuple[tuple[str, str], ...] = tuple()
    rpm_files_by_block: tuple[tuple[str, tuple[str, ...]], ...] = tuple()
    file_blocks: tuple[str, ...] = tuple()


@dataclass(frozen=True)
class ResolvedBuildMetadata:
    image: str
    distro: str
    releasever: str
    arch: str
    root_dir: str
    local_prefix: str
    orchestrator: str
    output_image: str
    manifest_labels: tuple[tuple[str, str], ...]
    manifest_env: tuple[tuple[str, str], ...]
    requested_packages: tuple[str, ...]
    resolved_packages: tuple[str, ...]
    common_packages: tuple[str, ...]
    bootstrap_packages: tuple[str, ...]
    card_order: tuple[str, ...]
    card_packages: tuple[tuple[str, tuple[str, ...]], ...]
    card_resolutions: tuple[tuple[str, tuple[str, ...]], ...]
    package_ids: tuple[tuple[str, str, str], ...]
    package_images: tuple[PackageImagePlan, ...]
    build_images: tuple[BuildImagePlan, ...]
    package_dir: str
    repo_dir: str
    cache_dir: str
    build_dir: str
    card_build_dir: str
    build_artifact_cache_dir: str
    dnf_cache_dir: str
    dnf_persist_dir: str
    dnf_log_dir: str
    dnf_resolve_dir: str
    podman: str
    buildah: str | None
    cache_version: str
    repo_images: tuple[str, ...]
    orchestrator_dnf_base: tuple[str, ...]
    package_blocks: tuple[tuple[str, tuple[str, ...]], ...]
    card_file_sets: tuple[tuple[str, str, tuple[FileRef, ...]], ...]
    postprocess_blocks: tuple[tuple[str, str], ...]
    card_envs: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    card_sources: tuple[tuple[str, str], ...]
    card_prepare_scripts: tuple[tuple[str, str], ...]
    card_builds: tuple[tuple[str, str], ...]
    card_specs: tuple[tuple[str, tuple[SpecBuild, ...]], ...]


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


@dataclass(frozen=True)
class StagedSpec:
    spec: SpecBuild
    spec_path: Path
    source_dir: Path
    packages: tuple[str, ...]
    targets: tuple[str, ...]


def build_manifest(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ci: bool = False,
) -> BuildResult:
    metadata = resolve_build_manifests(
        (manifest_path,),
        cards_dir=cards_dir,
        cache_dir=cache_dir,
        cache_version=cache_version,
        cache_only=cache_only,
    )
    build_package_card_images(metadata, cache_only=cache_only)
    build_outputs = build_build_images(metadata, cache_only=cache_only)
    return build_final_manifest_images(
        metadata,
        build_outputs=build_outputs,
        mode="combined" if ci else "separated",
    )[0]


def resolve_build_manifests(
    manifest_paths: tuple[Path, ...],
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
) -> tuple[ResolvedBuildMetadata, ...]:
    if not manifest_paths:
        raise ConfigError("at least one manifest is required")
    metadata = tuple(
        _resolve_manifest_metadata(
            manifest_path,
            cards_dir=cards_dir,
            cache_dir=cache_dir,
            cache_version=cache_version,
            cache_only=cache_only,
        )
        for manifest_path in manifest_paths
    )
    return _merge_common_packages(metadata)


def _resolve_manifest_metadata(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
) -> ResolvedBuildMetadata:
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
    arch = _cache_name(
        _substitute_variables(str(manifest_env.get("arch", "")), manifest_env),
        "arch",
    )
    manifest_env["arch"] = arch
    distro = _cache_name(
        _substitute_variables(validation.manifest.distro, manifest_env),
        "distro",
    )
    orchestrator_source = _substitute_variables(
        validation.manifest.orchestrator, manifest_env
    )
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
    card_build_dir = distro_cache_dir / "cards"
    repo_dir = dnf_dir / "repos"
    dnf_cache_dir = dnf_dir / "cache"
    dnf_persist_dir = dnf_dir / "persist"
    dnf_log_dir = dnf_dir / "log"
    dnf_resolve_dir = dnf_dir / "resolves"
    build_artifact_cache_dir = distro_cache_dir / "build-artifacts"

    distro_cache_dir.mkdir(parents=True, exist_ok=True)
    package_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    card_build_dir.mkdir(parents=True, exist_ok=True)
    build_artifact_cache_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)
    dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_persist_dir.mkdir(parents=True, exist_ok=True)
    dnf_log_dir.mkdir(parents=True, exist_ok=True)
    dnf_resolve_dir.mkdir(parents=True, exist_ok=True)

    podman = shutil.which("podman")
    if not podman:
        raise ConfigError("podman must be installed to build")
    log(f"Using Podman: {podman}")
    buildah = shutil.which("buildah")
    if buildah:
        log(f"Using Buildah: {buildah}")

    orchestrator_deps = tuple(
        _substitute_variables(package, manifest_env)
        for package in validation.manifest.orchestrator_deps
    )
    if orchestrator_deps:
        orchestrator_tag = f"{distro}-{_package_hash(orchestrator_deps)}-{cache_version}"
    else:
        orchestrator_tag = f"{distro}-base-{cache_version}"

    orchestrator_image = _local_image(local_prefix, "orchestrator", orchestrator_tag)
    if _image_exists(podman, orchestrator_image):
        log(f"Reusing orchestrator image: {orchestrator_image}")
    elif load_only_version or cache_only:
        raise ConfigError(f"orchestrator image is not cached: {orchestrator_image}")
    else:
        log(f"Creating orchestrator image: {orchestrator_image}")
        _create_orchestrator_image(
            podman=podman,
            buildah=buildah,
            source=orchestrator_source,
            image=orchestrator_image,
            packages=_build_deps(orchestrator_deps),
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
            buildah=_require_buildah(buildah),
            orchestrator=orchestrator,
            root_dir=root_dir,
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
    card_specs = {}
    card_build_deps = {}
    card_hashes = {}
    card_spec_hashes = {}
    card_envs = {}
    card_sources = {}
    card_prepare_scripts = {}
    postprocess_blocks = []
    bootstrap_card = validation.bootstrap
    if bootstrap_card is None:
        raise ConfigError(f"{manifest_path}: missing bootstrap card")
    if bootstrap_card.source is None:
        raise ConfigError("bootstrap card has no source path")
    bootstrap_packages = _packages_for_arch(bootstrap_card.packages, arch)
    if not bootstrap_packages:
        raise ConfigError(
            f"{bootstrap_card.source}: bootstrap card must define packages for {arch}"
        )
    if (
        bootstrap_card.files
        or bootstrap_card.specs
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
    requested_packages = list(bootstrap_packages)
    for _priority, _insertion_order, card_name, card in card_entries:
        if card.source is None:
            raise ConfigError(f"card '{card_name}' has no source path")
        if card.build.strip() and card.specs:
            raise ConfigError(f"{card.source}: card cannot define both build and specs")
        card_env = _card_env(inherited_env, card.env)
        inherited_env.update(card_env)
        card_names.append(card_name)
        card_envs[card_name] = card_env
        card_sources[card_name] = card.source
        if card.prepare.strip():
            card_prepare_scripts[card_name] = card.prepare.rstrip()
        card_packages = list(_packages_for_arch(card.packages, arch))
        for package in card_packages:
            requested_packages.append(package)
        card_requests.append(tuple(card_packages))
        parsed_file_refs = tuple(_parse_file_ref(file_ref) for file_ref in card.files)
        card_file_sets.append((card_name, card.source, parsed_file_refs))
        if card.build.strip():
            card_builds[card_name] = card.build.rstrip()
            card_build_deps[card_name] = card.build_deps
        if card.specs:
            active_specs = tuple(
                spec for spec in card.specs if _spec_packages_for_arch(spec, arch)
            )
            if active_specs:
                card_specs[card_name] = active_specs
            else:
                log(f"Skipping specs for card without packages on {arch}: {card_name}")
        if card_name in card_specs:
            card_build_deps[card_name] = card.build_deps
            card_spec_hashes[card_name] = _card_specs_hash(
                card.source,
                card_specs[card_name],
                card_env,
                card.prepare.rstrip(),
            )
        if card.hash.strip():
            card_hashes[card_name] = card.hash.strip()
        if card.postprocess.strip():
            postprocess_blocks.append((card_name, card.postprocess.rstrip()))
    build_card_names = set(card_builds) | set(card_specs)
    package_id_by_nevra: dict[str, tuple[str, str]] = {}
    requested_packages = tuple(requested_packages)
    if not requested_packages and not build_card_names:
        raise ConfigError(f"{manifest_path}: no packages requested by cards")
    log(
        f"Collected {len(requested_packages)} requested packages from "
        f"bootstrap and {len(card_entries)} cards"
    )
    if build_card_names:
        log(f"Collected {len(build_card_names)} build cards")

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

    locally_built_package_ids_by_card = _locally_built_package_ids_by_card(
        card_specs,
        arch,
    )
    locally_built_package_ids = set().union(
        *locally_built_package_ids_by_card.values()
    ) if locally_built_package_ids_by_card else set()

    log("Resolving package transaction for bootstrap")
    bootstrap_resolved_packages = _resolve_packages(
        orchestrator_dnf_base,
        releasever,
        bootstrap_packages,
        package_id_by_nevra,
        dnf_resolve_dir,
        tuple(repo_images),
    )
    if not bootstrap_resolved_packages:
        raise ConfigError("dnf did not resolve packages for bootstrap")
    bootstrap_package_set = set(bootstrap_resolved_packages)

    card_resolutions = []
    for card_name, card_packages in zip(card_names, card_requests):
        if not card_packages:
            if card_name in build_card_names:
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
            package_id_by_nevra,
            dnf_resolve_dir,
            tuple(repo_images),
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
            package_id_by_nevra,
            dnf_resolve_dir,
            tuple(repo_images),
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

    if previous_contextual_resolved:
        pruned_count = 0
        filtered_card_resolutions = []
        for card_resolution in card_resolutions:
            filtered_resolution = tuple(
                package
                for package in card_resolution
                if package in previous_contextual_resolved
            )
            pruned_count += len(card_resolution) - len(filtered_resolution)
            filtered_card_resolutions.append(filtered_resolution)
        card_resolutions = filtered_card_resolutions
        if pruned_count:
            log(
                f"Pruned {pruned_count} replaced packages from package transactions"
            )

    log("Grouping resolved packages into install blocks")
    package_counts = {}
    for card_resolution in card_resolutions:
        for package in set(card_resolution):
            package_counts[package] = package_counts.get(package, 0) + 1

    common_package_set = {
        package for package, count in package_counts.items() if count > 1
    }
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
    common_block_packages = tuple((*bootstrap_resolved_packages, *common_packages))
    package_blocks.append(("common", common_block_packages))
    package_block_hashes = [_nevra_hash(common_block_packages)]

    resolved_package_list = list(bootstrap_resolved_packages)
    resolved_package_list.extend(common_packages)
    for card_name, card_resolution in zip(card_names, card_resolutions):
        card_packages = []
        for package in card_resolution:
            if package in bootstrap_package_set or package in common_package_set:
                continue
            if (
                _resolved_package_id(package_id_by_nevra, package)
                in locally_built_package_ids
            ):
                continue
            card_packages.append(package)
        if not card_packages and card_name not in build_card_names:
            continue
        card_packages = tuple(card_packages)
        package_blocks.append((card_name, card_packages))
        package_block_hashes.append(_nevra_hash(card_packages))
        resolved_package_list.extend(card_packages)
    package_blocks = tuple(package_blocks)
    package_block_hashes = tuple(package_block_hashes)
    resolved_packages = tuple(resolved_package_list)
    if not resolved_packages and not package_blocks:
        raise ConfigError("dnf did not resolve any packages")
    log(
        f"Resolved {len(resolved_packages)} packages into "
        f"{len(package_blocks)} install blocks"
    )

    builder_images = {}
    builder_package_map = {}
    build_declared_package_map = {}
    for card_name in card_names:
        if card_name not in build_card_names:
            continue
        build_deps = _build_deps(card_build_deps.get(card_name, tuple()))
        if not build_deps:
            raise ConfigError(f"build card '{card_name}' must define build-deps")
        log(f"Resolving builder packages for card: {card_name}")
        explicit_builder_packages = _resolve_packages(
            orchestrator_dnf_base,
            releasever,
            build_deps,
            package_id_by_nevra,
            dnf_resolve_dir,
            tuple(repo_images),
        )
        if not explicit_builder_packages:
            raise ConfigError(f"dnf did not resolve build-deps for {card_name}")
        spec_builder_packages = tuple()
        if card_name in card_specs:
            spec_scan_dir = card_build_dir / _identifier(card_name) / "spec-scan"
            staged_specs = _stage_card_specs(
                card_source=card_sources[card_name],
                specs=card_specs[card_name],
                card_env=card_envs[card_name],
                workspace_dir=spec_scan_dir,
                arch=arch,
            )
            spec_builder_package_list = []
            for target, spec_paths in _spec_paths_by_build_target(staged_specs):
                log(f"Resolving spec BuildRequires for card: {card_name} ({target})")
                target_builder_packages = _resolve_spec_build_requires(
                    orchestrator_dnf_base,
                    releasever,
                    spec_scan_dir,
                    spec_paths,
                    target,
                    package_id_by_nevra,
                )
                spec_builder_package_list.extend(target_builder_packages)
                if target != arch:
                    log(
                        f"Resolving spec BuildRequires arch variants for card: "
                        f"{card_name} ({target})"
                    )
                    spec_builder_package_list.extend(
                        _resolve_package_arch_variants(
                            orchestrator_dnf_base,
                            releasever,
                            target_builder_packages,
                            target,
                            package_id_by_nevra,
                        )
                    )
            spec_builder_packages = _unique_packages(tuple(spec_builder_package_list))
        builder_package_requests = _unique_packages(
            (
                *build_deps,
                *spec_builder_packages,
            )
        )
        builder_packages = _resolve_packages(
            orchestrator_dnf_base,
            releasever,
            builder_package_requests,
            package_id_by_nevra,
            dnf_resolve_dir,
            tuple(repo_images),
        )
        if not builder_packages:
            raise ConfigError(f"dnf did not resolve builder packages for {card_name}")
        builder_package_map[card_name] = builder_packages
        if card_name in card_specs:
            declared_package_ids = []
            for spec in card_specs[card_name]:
                declared_package_ids.extend(
                    _package_request_ids(_spec_packages_for_arch(spec, arch), arch)
                )
            build_declared_package_map[card_name] = tuple(
                dict.fromkeys(declared_package_ids)
            )
        builder_hash = _nevra_hash(builder_packages)
        builder_image = _local_image(
            local_prefix,
            "builders",
            f"{distro}-{builder_hash}",
        )
        builder_images[card_name] = builder_image

    package_images_by_block = {}
    build_images_by_block = {}
    for (block_name, block_packages), block_hash in zip(
        package_blocks, package_block_hashes
    ):
        if not block_packages:
            continue
        package_image = _local_image(
            local_prefix,
            "cards",
            f"{distro}-{block_name}-{block_hash}",
        )
        package_images_by_block[block_name] = package_image

    for block_name, block_packages in package_blocks:
        if block_name not in build_card_names:
            continue
        if block_name in card_spec_hashes:
            build_hash = card_spec_hashes[block_name]
        else:
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
        build_images_by_block[block_name] = build_image

    expanded_package_blocks = []
    for block_name, block_packages in package_blocks:
        if block_name not in package_images_by_block and block_name not in build_images_by_block:
            log(f"No RPMs found for block, skipping install block: {block_name}")
            continue
        expanded_package_blocks.append((block_name, block_packages))

    package_blocks = tuple(expanded_package_blocks)
    resolved_packages = tuple(
        package
        for _block_name, block_packages in package_blocks
        for package in block_packages
    )

    return ResolvedBuildMetadata(
        image=image,
        distro=distro,
        releasever=releasever,
        arch=arch,
        root_dir=str(root_dir),
        local_prefix=local_prefix,
        orchestrator=orchestrator,
        output_image=output_image,
        manifest_labels=tuple(validation.manifest.labels.items()),
        manifest_env=tuple(sorted(manifest_env.items())),
        requested_packages=requested_packages,
        resolved_packages=resolved_packages,
        common_packages=tuple(common_packages),
        bootstrap_packages=tuple(bootstrap_resolved_packages),
        card_order=tuple(card_names),
        card_packages=tuple(
            (block_name, block_packages)
            for block_name, block_packages in package_blocks
            if block_name != "common"
        ),
        card_resolutions=tuple(zip(card_names, card_resolutions)),
        package_ids=tuple(
            (package, package_id[0], package_id[1])
            for package, package_id in sorted(package_id_by_nevra.items())
        ),
        package_images=tuple(
            PackageImagePlan(
                block=block_name,
                packages=block_packages,
                image=package_images_by_block[block_name],
            )
            for block_name, block_packages in package_blocks
            if block_name in package_images_by_block
        ),
        build_images=tuple(
            BuildImagePlan(
                block=block_name,
                image=build_images_by_block[block_name],
                builder_image=builder_images[block_name],
                builder_packages=builder_package_map[block_name],
                declared_package_ids=build_declared_package_map.get(
                    block_name, tuple()
                ),
            )
            for block_name in build_images_by_block
        ),
        package_dir=str(package_dir),
        repo_dir=str(repo_dir),
        cache_dir=str(distro_cache_dir),
        build_dir=str(build_dir),
        card_build_dir=str(card_build_dir),
        build_artifact_cache_dir=str(build_artifact_cache_dir),
        dnf_cache_dir=str(dnf_cache_dir),
        dnf_persist_dir=str(dnf_persist_dir),
        dnf_log_dir=str(dnf_log_dir),
        dnf_resolve_dir=str(dnf_resolve_dir),
        podman=str(podman),
        buildah=buildah,
        cache_version=cache_version,
        repo_images=tuple(repo_images),
        orchestrator_dnf_base=tuple(orchestrator_dnf_base),
        package_blocks=package_blocks,
        card_file_sets=tuple(
            (
                card_name,
                str(card_source),
                file_refs,
            )
            for card_name, card_source, file_refs in card_file_sets
        ),
        postprocess_blocks=tuple(postprocess_blocks),
        card_envs=tuple(
            (card_name, tuple(sorted(card_env.items())))
            for card_name, card_env in card_envs.items()
        ),
        card_sources=tuple(
            (card_name, str(card_source))
            for card_name, card_source in card_sources.items()
        ),
        card_prepare_scripts=tuple(card_prepare_scripts.items()),
        card_builds=tuple(card_builds.items()),
        card_specs=tuple(card_specs.items()),
    )


def resolve_manifest_images(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
) -> BuildResult:
    metadata = resolve_build_manifests(
        (manifest_path,),
        cards_dir=cards_dir,
        cache_dir=cache_dir,
        cache_version=cache_version,
        cache_only=True,
    )[0]
    return _metadata_build_result(metadata)


def _merge_common_packages(
    metadata: tuple[ResolvedBuildMetadata, ...],
) -> tuple[ResolvedBuildMetadata, ...]:
    if len(metadata) <= 1:
        return metadata

    contexts = {
        (
            item.root_dir,
            item.distro,
            item.releasever,
            item.arch,
            item.local_prefix,
            item.orchestrator,
            item.repo_images,
        )
        for item in metadata
    }
    if len(contexts) != 1:
        raise ConfigError(
            "multi-manifest resolution requires compatible root, distro, "
            "releasever, arch, orchestrator, and repository metadata"
        )
    return metadata


def build_package_card_images(
    metadata: tuple[ResolvedBuildMetadata, ...],
    *,
    cache_only: bool = False,
) -> None:
    created: set[str] = set()
    built_builders: set[str] = set()
    for manifest in metadata:
        for plan in manifest.package_images:
            if not plan.packages or plan.image in created:
                continue
            if _image_exists(manifest.podman, plan.image):
                log(f"Reusing card package image: {plan.image}")
                created.add(plan.image)
                continue
            if cache_only:
                raise ConfigError(f"card package image is not cached: {plan.image}")

            rpm_files = _download_block_packages(
                list(manifest.orchestrator_dnf_base),
                plan.packages,
            )
            log(f"Creating card package image: {plan.image}")
            _create_package_image(
                buildah=_require_buildah(manifest.buildah),
                image=plan.image,
                package_dir=Path(manifest.package_dir),
                rpm_files=rpm_files,
            )
            created.add(plan.image)

        for plan in manifest.build_images:
            if plan.builder_image in built_builders:
                continue
            if _image_exists(manifest.podman, plan.builder_image):
                log(f"Reusing builder image: {plan.builder_image}")
                built_builders.add(plan.builder_image)
                continue
            if cache_only:
                raise ConfigError(f"builder image is not cached: {plan.builder_image}")

            builder_rpm_files = _download_block_packages(
                list(manifest.orchestrator_dnf_base),
                plan.builder_packages,
                package_dir=Path(manifest.package_dir),
                resolve_dependencies=True,
            )
            log(f"Creating builder image: {plan.builder_image}")
            _create_builder_image(
                podman=manifest.podman,
                buildah=_require_buildah(manifest.buildah),
                orchestrator=manifest.orchestrator,
                root_dir=Path(manifest.root_dir),
                repo_dir=Path(manifest.repo_dir),
                dnf_cache_dir=Path(manifest.dnf_cache_dir),
                dnf_persist_dir=Path(manifest.dnf_persist_dir),
                dnf_log_dir=Path(manifest.dnf_log_dir),
                image=plan.builder_image,
                package_dir=Path(manifest.package_dir),
                rpm_files=builder_rpm_files,
                releasever=manifest.releasever,
            )
            built_builders.add(plan.builder_image)


def build_build_images(
    metadata: tuple[ResolvedBuildMetadata, ...],
    *,
    targets: tuple[str, ...] = tuple(),
    cache_only: bool = False,
) -> BuildImageOutputs:
    target_set = set(targets)
    images_by_block: dict[str, str] = {}
    rpm_files_by_block: dict[str, tuple[str, ...]] = {}
    file_blocks: set[str] = set()

    for manifest in metadata:
        card_envs = {
            name: dict(values)
            for name, values in manifest.card_envs
        }
        card_sources = {name: Path(source) for name, source in manifest.card_sources}
        card_prepare_scripts = dict(manifest.card_prepare_scripts)
        card_builds = dict(manifest.card_builds)
        card_specs = dict(manifest.card_specs)

        for plan in manifest.build_images:
            if target_set and plan.block not in target_set and plan.image not in target_set:
                continue

            if _image_exists(manifest.podman, plan.image):
                log(f"Reusing build output image: {plan.image}")
                images_by_block[plan.block] = plan.image
                rpm_files_by_block[plan.block] = _rpm_files_in_image(
                    manifest.podman, plan.image
                )
                if _image_has_files(manifest.podman, plan.image, "/files"):
                    file_blocks.add(plan.block)
                continue
            if cache_only:
                raise ConfigError(f"build output image is not cached: {plan.image}")
            if not _image_exists(manifest.podman, plan.builder_image):
                raise ConfigError(
                    f"builder image is missing: {plan.builder_image}; "
                    "create card images before running builds"
                )

            card_env = dict(card_envs[plan.block])
            if plan.block in card_prepare_scripts and plan.block not in card_specs:
                log(f"Preparing build for card: {plan.block}")
                prepared_env = _run_prepare_block(
                    card_source=card_sources[plan.block],
                    card_env=card_env,
                    prepare_script=card_prepare_scripts[plan.block],
                )
                card_env.update(prepared_env)

            log(f"Running build for card: {plan.block} (:{_image_tag(plan.image)})")
            if plan.block in card_specs:
                build_output = _run_specs_build(
                    podman=manifest.podman,
                    orchestrator=plan.builder_image,
                    build_dir=Path(manifest.card_build_dir) / _identifier(plan.block),
                    artifact_cache_dir=Path(manifest.build_artifact_cache_dir)
                    / _identifier(plan.block),
                    card_name=plan.block,
                    card_source=card_sources[plan.block],
                    card_env=card_env,
                    specs=card_specs[plan.block],
                    prepare_script=card_prepare_scripts.get(plan.block, ""),
                    arch=manifest.arch,
                )
            else:
                build_output = _run_card_build(
                    podman=manifest.podman,
                    orchestrator=plan.builder_image,
                    build_dir=Path(manifest.card_build_dir) / _identifier(plan.block),
                    artifact_cache_dir=Path(manifest.build_artifact_cache_dir)
                    / _identifier(plan.block),
                    card_name=plan.block,
                    card_source=card_sources[plan.block],
                    card_env=card_env,
                    build_script=card_builds[plan.block],
                )
            if not build_output.rpm_files and build_output.file_count == 0:
                log(f"No build outputs found for card: {plan.block}")
                continue

            log(f"Creating build output image: {plan.image}")
            _create_build_output_image(
                buildah=_require_buildah(manifest.buildah),
                image=plan.image,
                rpm_dir=build_output.rpm_dir,
                files_dir=build_output.files_dir,
            )
            images_by_block[plan.block] = plan.image
            rpm_files_by_block[plan.block] = build_output.rpm_files
            if build_output.file_count:
                file_blocks.add(plan.block)

    return BuildImageOutputs(
        images_by_block=tuple(sorted(images_by_block.items())),
        rpm_files_by_block=tuple(sorted(rpm_files_by_block.items())),
        file_blocks=tuple(sorted(file_blocks)),
    )


def build_final_manifest_images(
    metadata: tuple[ResolvedBuildMetadata, ...],
    *,
    build_outputs: BuildImageOutputs | None = None,
    mode: str = "separated",
) -> tuple[BuildResult, ...]:
    if mode not in ("separated", "combined"):
        raise ConfigError(f"unknown final image build mode: {mode}")
    build_outputs = build_outputs or BuildImageOutputs()
    results = []
    for manifest in metadata:
        results.append(
            _build_final_manifest_image(
                manifest,
                build_outputs=build_outputs,
                mode=mode,
            )
        )
    return tuple(results)


def _build_final_manifest_image(
    metadata: ResolvedBuildMetadata,
    *,
    build_outputs: BuildImageOutputs,
    mode: str,
) -> BuildResult:
    build_dir = Path(metadata.build_dir)
    card_files_dir = build_dir / "files"
    log("Staging card files")
    shutil.rmtree(card_files_dir, ignore_errors=True)
    card_file_cards: set[str] = set()
    for card_name, card_source_text, file_refs in metadata.card_file_sets:
        if not file_refs:
            continue
        card_source = Path(card_source_text)
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

    package_images_by_block = {
        plan.block: plan.image for plan in metadata.package_images
    }
    build_images_by_block = dict(build_outputs.images_by_block)
    build_rpm_files_by_block = dict(build_outputs.rpm_files_by_block)
    build_file_blocks = set(build_outputs.file_blocks)
    package_blocks = tuple(
        (block_name, block_packages)
        for block_name, block_packages in metadata.package_blocks
        if block_name == "common"
        or block_name in package_images_by_block
        or block_name in build_images_by_block
    )

    log(f"Generating Containerfile: {build_dir / 'Containerfile'}")
    containerfile = build_dir / "Containerfile"
    containerfile.write_text(
        _render_final_containerfile(
            metadata,
            mode=mode,
            package_blocks=package_blocks,
            package_images_by_block=package_images_by_block,
            build_images_by_block=build_images_by_block,
            build_rpm_files_by_block=build_rpm_files_by_block,
            card_file_cards=card_file_cards,
            build_file_blocks=build_file_blocks,
        ),
        encoding="utf-8",
    )

    log(f"Building final image: {metadata.output_image}")
    _run_container_build(
        [
            metadata.podman,
            "build",
            "--layers",
            "--pull=missing",
            "--tag",
            metadata.output_image,
            "--volume",
            f"{Path(metadata.root_dir) / 'repos'}:/workspace/repos:ro",
            "--volume",
            f"{metadata.repo_dir}:/ludos/dnf/repos:ro",
            "--volume",
            f"{metadata.dnf_cache_dir}:/ludos/dnf/cache",
            "--volume",
            f"{metadata.dnf_persist_dir}:/ludos/dnf/persist",
            "--volume",
            f"{metadata.dnf_log_dir}:/ludos/dnf/log",
            "--file",
            str(containerfile),
            str(build_dir),
        ],
        containerfile,
    )

    return _metadata_build_result(
        metadata,
        package_blocks=package_blocks,
        build_outputs=build_outputs,
    )


def _render_final_containerfile(
    metadata: ResolvedBuildMetadata,
    *,
    mode: str,
    package_blocks: tuple[tuple[str, tuple[str, ...]], ...],
    package_images_by_block: dict[str, str],
    build_images_by_block: dict[str, str],
    build_rpm_files_by_block: dict[str, tuple[str, ...]],
    card_file_cards: set[str],
    build_file_blocks: set[str],
) -> str:
    package_stage_names = {
        block_name: f"cards_{_identifier(block_name)}"
        for block_name, _block_packages in package_blocks
        if block_name in package_images_by_block
    }
    build_stage_names = {
        block_name: f"builds_{_identifier(block_name)}"
        for block_name in build_images_by_block
    }
    stage_lines = "".join(
        f"FROM {package_images_by_block[block_name]} AS {package_stage_names[block_name]}\n"
        for block_name, _block_packages in package_blocks
        if block_name in package_stage_names
    )
    stage_lines += "".join(
        f"FROM {image} AS {build_stage_names[block_name]}\n"
        for block_name, image in build_images_by_block.items()
    )

    label_lines = "".join(
        f"LABEL {json.dumps(key)}={json.dumps(value)}\n"
        for key, value in metadata.manifest_labels
    )
    common_stage = package_stage_names["common"]
    bootstrap_paths = _rpm_paths_for_packages(
        "/rpms/common",
        metadata.bootstrap_packages,
    )
    bootstrap_step = f"""FROM {metadata.orchestrator} AS bootstrap
WORKDIR /workspace/repos
RUN mkdir -p /target

#
# Bootstrap root
#

RUN --mount=type=bind,from={common_stage},source=/rpms,target=/rpms/common,ro /bin/sh <<'LUDOS_BOOTSTRAP'
{_dnf_install_script(metadata.releasever, bootstrap_paths, installroot="/target")}
LUDOS_BOOTSTRAP
"""

    card_packages = dict(metadata.card_packages)
    card_resolutions = dict(metadata.card_resolutions)
    postprocess_blocks = dict(metadata.postprocess_blocks)
    package_id_by_nevra = {
        package: (name, arch)
        for package, name, arch in metadata.package_ids
    }
    built_package_ids_by_block = {
        plan.block: set(plan.declared_package_ids)
        for plan in metadata.build_images
    }
    install_steps = []
    postprocess_steps = []

    if mode == "combined":
        all_built_package_ids = set().union(
            *built_package_ids_by_block.values()
        ) if built_package_ids_by_block else set()
        install_paths = _rpm_paths_for_packages(
            "/rpms/common",
            tuple(
                package
                for package in metadata.common_packages
                if _resolved_package_id(package_id_by_nevra, package)
                not in all_built_package_ids
            ),
        )
        mounts = [("type=bind", f"from={common_stage}", "source=/rpms", "target=/rpms/common", "ro")]
        for card_name in metadata.card_order:
            card_block_packages = card_packages.get(card_name, tuple())
            if card_block_packages and card_name in package_stage_names:
                mounts.append(
                    (
                        "type=bind",
                        f"from={package_stage_names[card_name]}",
                        "source=/rpms",
                        f"target=/rpms/{_identifier(card_name)}",
                        "ro",
                    )
                )
                install_paths += _rpm_paths_for_packages(
                    f"/rpms/{_identifier(card_name)}",
                    card_block_packages,
                )
            build_rpm_files = build_rpm_files_by_block.get(card_name, tuple())
            if build_rpm_files and card_name in build_stage_names:
                mounts.append(
                    (
                        "type=bind",
                        f"from={build_stage_names[card_name]}",
                        "source=/rpms",
                        f"target=/rpms/{_identifier(card_name)}-build",
                        "ro",
                    )
                )
                install_paths += tuple(
                    f"/rpms/{_identifier(card_name)}-build/{rpm_file}"
                    for rpm_file in build_rpm_files
                )
        if install_paths:
            install_steps.append(
                _run_with_mounts(
                    mounts,
                    "LUDOS_INSTALL",
                    _dnf_install_script(metadata.releasever, install_paths),
                )
            )
        postprocess_steps.append(
            _combined_postprocess_step(
                metadata,
                postprocess_blocks,
                card_file_cards,
                build_file_blocks,
                build_stage_names,
            )
        )
    else:
        installed_common = set(metadata.bootstrap_packages)
        common_set = set(metadata.common_packages)
        for card_name in metadata.card_order:
            card_built_package_ids = built_package_ids_by_block.get(card_name, set())
            mounts = [
                (
                    "type=bind",
                    f"from={common_stage}",
                    "source=/rpms",
                    "target=/rpms/common",
                    "ro",
                )
            ]
            common_needed = tuple(
                package
                for package in card_resolutions.get(card_name, tuple())
                if package in common_set and package not in installed_common
                and _resolved_package_id(package_id_by_nevra, package)
                not in card_built_package_ids
            )
            installed_common.update(
                package
                for package in card_resolutions.get(card_name, tuple())
                if package in common_set
                and _resolved_package_id(package_id_by_nevra, package)
                in card_built_package_ids
            )
            installed_common.update(common_needed)
            install_paths = _rpm_paths_for_packages("/rpms/common", common_needed)
            card_block_packages = card_packages.get(card_name, tuple())
            if card_block_packages and card_name in package_stage_names:
                mounts.append(
                    (
                        "type=bind",
                        f"from={package_stage_names[card_name]}",
                        "source=/rpms",
                        f"target=/rpms/{_identifier(card_name)}",
                        "ro",
                    )
                )
                install_paths += _rpm_paths_for_packages(
                    f"/rpms/{_identifier(card_name)}",
                    card_block_packages,
                )
            build_rpm_files = build_rpm_files_by_block.get(card_name, tuple())
            if build_rpm_files and card_name in build_stage_names:
                mounts.append(
                    (
                        "type=bind",
                        f"from={build_stage_names[card_name]}",
                        "source=/rpms",
                        f"target=/rpms/{_identifier(card_name)}-build",
                        "ro",
                    )
                )
                install_paths += tuple(
                    f"/rpms/{_identifier(card_name)}-build/{rpm_file}"
                    for rpm_file in build_rpm_files
                )
            if install_paths:
                install_steps.append(
                    f"""#
# Install packages: {card_name}
#

{_run_with_mounts(
    mounts,
    f"LUDOS_INSTALL_{_identifier(card_name)}",
    _dnf_install_script(metadata.releasever, install_paths),
)}
"""
                )
            if card_name in postprocess_blocks:
                postprocess_steps.append(
                    _postprocess_step(
                        card_name,
                        postprocess_blocks[card_name],
                        card_name in card_file_cards,
                        card_name in build_file_blocks,
                        build_stage_names.get(card_name, ""),
                    )
                )

    install_step_lines = "\n".join(step for step in install_steps if step)
    postprocess_step_lines = "\n".join(step for step in postprocess_steps if step)
    return f"""{stage_lines}{bootstrap_step}
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
{label_lines}"""


def _dnf_install_script(
    releasever: str,
    rpm_paths: tuple[str, ...],
    *,
    installroot: str | None = None,
) -> str:
    if not rpm_paths:
        return "set -e\nexit 0\n"
    installroot_line = f"    --installroot={installroot} \\\n" if installroot else ""
    clean_root = installroot or ""
    clean_cache = f"{clean_root}/var/cache/dnf".replace("//", "/")
    clean_logs = f"{clean_root}/var/log/dnf*".replace("//", "/")
    rpm_lines = " \\\n".join(f"    {shlex.quote(path)}" for path in rpm_paths)
    return f"""set -e
dnf5 -y \\
{installroot_line}    --releasever={releasever} \\
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
{rpm_lines} \\
    && \\
    rm -rf {clean_cache} {clean_logs}
"""


def _run_with_mounts(
    mounts: list[tuple[str, ...]],
    heredoc: str,
    script: str,
) -> str:
    mount_args = " ".join(
        "--mount=" + ",".join(parts)
        for parts in mounts
    )
    return f"RUN {mount_args} /bin/sh <<'{heredoc}'\n{script}{heredoc}\n"


def _postprocess_step(
    block_name: str,
    postprocess: str,
    has_card_files: bool,
    has_build_files: bool,
    build_stage_name: str,
) -> str:
    mounts = []
    identifier = _identifier(block_name)
    if has_card_files:
        mounts.append(
            (
                "type=bind",
                f"source=files/{identifier}",
                "target=/ludos/card-files",
                "ro",
            )
        )
    if has_build_files and build_stage_name:
        mounts.append(
            (
                "type=bind",
                f"from={build_stage_name}",
                "source=/files",
                "target=/ludos/build-files",
                "ro",
            )
        )
    set_command = "" if _starts_with_set_command(postprocess) else "set -e\n"
    setup = _postprocess_file_setup(has_card_files, has_build_files)
    return f"""#
# Postprocess: {block_name}
#

{_run_with_mounts(
    mounts,
    f"LUDOS_POSTPROCESS_{identifier}",
    f"{setup}{set_command}{postprocess}\nrm -rf /files\n",
) if mounts else f"RUN /bin/sh <<'LUDOS_POSTPROCESS_{identifier}'\n{set_command}{postprocess}\nrm -rf /files\nLUDOS_POSTPROCESS_{identifier}\n"}
"""


def _combined_postprocess_step(
    metadata: ResolvedBuildMetadata,
    postprocess_blocks: dict[str, str],
    card_file_cards: set[str],
    build_file_blocks: set[str],
    build_stage_names: dict[str, str],
) -> str:
    if not postprocess_blocks:
        return ""
    mounts = []
    for card_name in metadata.card_order:
        identifier = _identifier(card_name)
        if card_name in card_file_cards:
            mounts.append(
                (
                    "type=bind",
                    f"source=files/{identifier}",
                    f"target=/ludos/card-files/{identifier}",
                    "ro",
                )
            )
        if card_name in build_file_blocks and card_name in build_stage_names:
            mounts.append(
                (
                    "type=bind",
                    f"from={build_stage_names[card_name]}",
                    "source=/files",
                    f"target=/ludos/build-files/{identifier}",
                    "ro",
                )
            )

    scripts = []
    for card_name in metadata.card_order:
        postprocess = postprocess_blocks.get(card_name)
        if not postprocess:
            continue
        identifier = _identifier(card_name)
        set_command = "" if _starts_with_set_command(postprocess) else "set -e\n"
        scripts.append(
            f"""#
# Postprocess: {card_name}
#
rm -rf /files
mkdir -p /files
if [ -d /ludos/card-files/{identifier} ]; then cp -a /ludos/card-files/{identifier}/. /files/; fi
if [ -d /ludos/build-files/{identifier} ]; then cp -a /ludos/build-files/{identifier}/. /files/; fi
{set_command}{postprocess}
rm -rf /files
"""
        )
    return _run_with_mounts(
        mounts,
        "LUDOS_POSTPROCESS",
        "\n".join(scripts),
    ) if mounts else "RUN /bin/sh <<'LUDOS_POSTPROCESS'\n" + "\n".join(scripts) + "LUDOS_POSTPROCESS\n"


def _postprocess_file_setup(has_card_files: bool, has_build_files: bool) -> str:
    if not has_card_files and not has_build_files:
        return ""
    lines = ["rm -rf /files", "mkdir -p /files"]
    if has_card_files:
        lines.append("cp -a /ludos/card-files/. /files/")
    if has_build_files:
        lines.append("cp -a /ludos/build-files/. /files/")
    return "\n".join(lines) + "\n"


def _rpm_paths_for_packages(mount_dir: str, packages: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        f"{mount_dir}/{_rpm_filename_nevra(package)}.rpm"
        for package in packages
    )


def _rpm_files_in_image(podman: str, image: str) -> tuple[str, ...]:
    with tempfile.TemporaryDirectory(prefix="ludos-image-rpms-") as temp_dir:
        output_dir = Path(temp_dir)
        _extract_image_paths(podman, image, {"rpms": output_dir})
        return tuple(sorted(path.name for path in output_dir.glob("*.rpm")))


def _image_has_files(podman: str, image: str, source: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="ludos-image-files-") as temp_dir:
        output_dir = Path(temp_dir)
        try:
            _extract_image_paths(podman, image, {source.strip("/"): output_dir})
        except subprocess.CalledProcessError:
            return False
        return any(path.is_file() for path in output_dir.rglob("*"))


def _metadata_build_result(
    metadata: ResolvedBuildMetadata,
    *,
    package_blocks: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
    build_outputs: BuildImageOutputs | None = None,
) -> BuildResult:
    package_blocks = package_blocks or metadata.package_blocks
    build_outputs = build_outputs or BuildImageOutputs()
    build_images_by_block = dict(build_outputs.images_by_block)
    if not build_images_by_block:
        build_images_by_block = {
            plan.block: plan.image for plan in metadata.build_images
        }
    return BuildResult(
        image=metadata.image,
        distro=metadata.distro,
        orchestrator=metadata.orchestrator,
        output_image=metadata.output_image,
        requested_packages=metadata.requested_packages,
        resolved_packages=tuple(
            package
            for _block_name, block_packages in package_blocks
            for package in block_packages
        ),
        package_blocks=package_blocks,
        package_dir=Path(metadata.package_dir),
        repo_dir=Path(metadata.repo_dir),
        podman=metadata.podman,
        cache_version=metadata.cache_version,
        repo_images=metadata.repo_images,
        package_images=tuple(plan.image for plan in metadata.package_images),
        build_images=tuple(build_images_by_block.values()),
        build_blocks=tuple(build_images_by_block),
        builder_images=tuple(plan.builder_image for plan in metadata.build_images),
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


def _create_orchestrator_image(
    *,
    podman: str,
    buildah: str | None,
    source: str,
    image: str,
    packages: tuple[str, ...],
) -> None:
    returncode, _output = _run_streamed_command([podman, "pull", source])
    if returncode != 0:
        raise ConfigError(f"failed to pull orchestrator image: {source}")

    if not packages:
        subprocess.run([podman, "tag", source, image], check=True)
        return

    package_args = " ".join(shlex.quote(package) for package in packages)
    buildah = _require_buildah(buildah)
    buildah_command = shlex.quote(buildah)
    script = "\n".join(
        [
            "set -eu",
            "container=",
            "mounted=0",
            'cleanup() {',
            '  if [ "$mounted" = 1 ]; then '
            f"{buildah_command} unmount \"$container\" >/dev/null 2>&1 || true; fi",
            '  if [ -n "$container" ]; then '
            f"{buildah_command} rm \"$container\" >/dev/null 2>&1 || true; fi",
            "}",
            "trap cleanup EXIT INT TERM",
            f"container=$({buildah_command} from --quiet {shlex.quote(source)})",
            f"mount_path=$({buildah_command} mount \"$container\")",
            "mounted=1",
            _shell_command(
                [
                    podman,
                    "run",
                    "--rm",
                    "--volume",
                    "$mount_path:/target",
                    source,
                    "dnf5",
                    "-y",
                    "--installroot=/target",
                    "--setopt=install_weak_deps=False",
                    "install",
                    "--allowerasing",
                ],
                raw_suffix=f" {package_args}",
            ),
            'rm -rf "$mount_path/var/cache/dnf"',
            'find "$mount_path/var/log" -maxdepth 1 -name "dnf*" '
            "-exec rm -rf {} + 2>/dev/null || true",
            f"{buildah_command} unmount \"$container\" >/dev/null",
            "mounted=0",
            f"{buildah_command} commit --rm --quiet --format oci \"$container\" {shlex.quote(image)} >/dev/null",
            "container=",
        ]
    )
    returncode, _output = _run_streamed_command(
        [buildah, "unshare", "/bin/sh", "-s"],
        input_text=script + "\n",
    )
    if returncode != 0:
        raise ConfigError(
            f"orchestrator image build failed with exit status {returncode}"
        )


def _repo_id(rendered_repo: str, source: Path) -> str:
    for line in rendered_repo.splitlines():
        match = re.fullmatch(r"\[([^]]+)]", line.strip())
        if match:
            return match.group(1)
    raise ConfigError(f"{source}: repository definition does not contain a repo id")


def _build_deps(card_build_deps: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(card_build_deps))


def _unique_packages(packages: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(packages))


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
    image: str,
    package_dir: Path,
    rpm_files: tuple[str, ...],
    releasever: str,
) -> None:
    rpm_copy_lines = _copy_files_to_shell_dir_lines(
        (_cached_rpm_path(package_dir, rpm_file) for rpm_file in rpm_files),
        "$rpm_dir",
    )
    rpm_paths = " ".join(shlex.quote(f"/rpms/{rpm_file}") for rpm_file in rpm_files)
    body = [
        "rpm_dir=$(mktemp -d)",
        'cleanup_dirs="$rpm_dir"',
        *rpm_copy_lines,
        _shell_command(
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
                "$mount_path:/target",
                "--volume",
                "$rpm_dir:/rpms:ro",
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
                "--cacheonly",
                "--disable-repo=*",
                "--enable-repo=*",
                "install",
                "--allowerasing",
            ],
            raw_suffix=f" {rpm_paths}",
        ),
        'rm -rf "$mount_path/var/cache/dnf"',
        'find "$mount_path/var/log" -maxdepth 1 -name "dnf*" -exec rm -rf {} + 2>/dev/null || true',
    ]
    _create_scratch_image(buildah=buildah, image=image, body=body)


def _create_repo_image(
    *,
    podman: str,
    buildah: str,
    orchestrator: str,
    root_dir: Path,
    image: str,
    repo_name: str,
    repo_id: str,
    rendered_repo: str,
) -> None:
    body = [
        'mkdir -p "$mount_path/repos" "$mount_path/cache" "$mount_path/persist"',
        f"printf %s {shlex.quote(rendered_repo)} > \"$mount_path/repos/{shlex.quote(repo_name)}\"",
        "log_dir=$(mktemp -d)",
        'cleanup_dirs="$log_dir"',
        _shell_command(
            [
                podman,
                "run",
                "--rm",
                "--volume",
                f"{root_dir / 'repos'}:/workspace/repos:ro",
                "--volume",
                "$mount_path/repos:/ludos/dnf/repos:ro",
                "--volume",
                "$mount_path/cache:/ludos/dnf/cache",
                "--volume",
                "$mount_path/persist:/ludos/dnf/persist",
                "--volume",
                "$log_dir:/ludos/dnf/log",
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
        ),
    ]
    _create_scratch_image(buildah=buildah, image=image, body=body)


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
    package_id_by_nevra: dict[str, tuple[str, str]],
    resolve_cache_dir: Path,
    repo_images: tuple[str, ...],
) -> tuple[str, ...]:
    cmd = [
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
        "--allowerasing",
        *packages,
    ]
    transaction_preview = _run_cached_transaction_preview(
        cmd,
        resolve_cache_dir,
        repo_images,
    )
    output = transaction_preview.stdout + "\n" + transaction_preview.stderr
    if transaction_preview.returncode not in (0, 1):
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not resolve packages:\n{detail}")
    entries = _parse_resolved_package_entries(output, include_dependencies=True)
    package_id_by_nevra.update(entries)
    return tuple(package for package, _package_id in entries)


def _run_cached_transaction_preview(
    cmd: list[str],
    resolve_cache_dir: Path,
    repo_images: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    repo_tags = tuple(_image_tag(image) for image in repo_images)
    cache_key = _resolve_cache_key(cmd, repo_tags)
    cache_file = resolve_cache_dir / f"{cache_key}.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            args=data.get("args", cmd),
            returncode=int(data["returncode"]),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
        )

    transaction_preview = subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
    )
    resolve_cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "args": cmd,
        "repo_tags": repo_tags,
        "returncode": transaction_preview.returncode,
        "stdout": transaction_preview.stdout,
        "stderr": transaction_preview.stderr,
    }
    temp_file = cache_file.with_suffix(".json.tmp")
    temp_file.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temp_file.replace(cache_file)
    return transaction_preview


def _resolve_cache_key(cmd: list[str], repo_tags: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "cmd": cmd,
            "repo_tags": repo_tags,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_resolved_package_entries(
    output: str,
    include_dependencies: bool,
) -> tuple[tuple[str, tuple[str, str]], ...]:
    resolved_packages = []
    in_install_section = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped == "Transaction Summary:":
            break
        if not stripped:
            continue
        if stripped.startswith("Installing"):
            if stripped != "Installing:" and not include_dependencies:
                in_install_section = False
                continue
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
        resolved_packages.append((f"{package}-{version}.{arch}", (package, arch)))
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
    orchestrator_dnf_base: list[str],
    block_packages: tuple[str, ...],
    *,
    package_dir: Path | None = None,
    resolve_dependencies: bool = False,
) -> tuple[str, ...]:
    if not block_packages:
        return tuple()
    if resolve_dependencies:
        if package_dir is None:
            raise ConfigError("package_dir is required when resolving download dependencies")
    else:
        rpm_files = _package_rpm_files(orchestrator_dnf_base, block_packages)
        download_options = ["--destdir=/ludos/packages"]
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
                *download_options,
                *block_packages,
            ],
            "package download",
        )
        return rpm_files

    if resolve_dependencies:
        rpm_files = _package_rpm_files(orchestrator_dnf_base, block_packages)
        if not _rpm_files_cached(package_dir, rpm_files):
            _download_exact_packages(
                orchestrator_dnf_base,
                block_packages,
                "/ludos/packages",
            )
        return rpm_files

    raise AssertionError("unreachable")


def _download_exact_packages(
    orchestrator_dnf_base: list[str],
    packages: tuple[str, ...],
    destdir: str,
) -> None:
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
            f"--destdir={destdir}",
            *packages,
        ],
        "package download",
    )


def _rpm_files_cached(package_dir: Path, rpm_files: tuple[str, ...]) -> bool:
    return all((package_dir / rpm_file).is_file() for rpm_file in rpm_files)


def _rpm_filename_nevra(nevra: str) -> str:
    if ":" not in nevra:
        return nevra
    name_epoch, version_release_arch = nevra.split(":", 1)
    name, _epoch = name_epoch.rsplit("-", 1)
    return f"{name}-{version_release_arch}"


def _cached_rpm_path(package_dir: Path, rpm_file: str) -> Path:
    matches = list(package_dir.rglob(rpm_file))
    if not matches:
        raise ConfigError(f"downloaded RPM is missing from cache: {rpm_file}")
    return matches[0]


def _create_package_image(
    *,
    buildah: str,
    image: str,
    package_dir: Path,
    rpm_files: tuple[str, ...],
    files_dir: Path | None = None,
) -> None:
    body = ['mkdir -p "$mount_path/rpms" "$mount_path/files"']
    body.extend(
        _copy_files_to_shell_dir_lines(
            (_cached_rpm_path(package_dir, rpm_file) for rpm_file in rpm_files),
            "$mount_path/rpms",
        )
    )
    if files_dir is not None and files_dir.exists():
        body.extend(_copy_tree_to_shell_dir_lines(files_dir, "$mount_path/files"))

    _create_scratch_image(buildah=buildah, image=image, body=body)


def _create_build_output_image(
    *,
    buildah: str,
    image: str,
    rpm_dir: Path | None,
    files_dir: Path | None,
) -> None:
    body = ['mkdir -p "$mount_path/rpms" "$mount_path/files"']
    if rpm_dir is not None and rpm_dir.exists():
        body.extend(
            _copy_files_to_shell_dir_lines(
                (
                    source_path
                    for source_path in rpm_dir.rglob("*.rpm")
                    if not source_path.name.endswith(".src.rpm")
                ),
                "$mount_path/rpms",
            )
        )
    if files_dir is not None and files_dir.exists():
        body.extend(_copy_tree_to_shell_dir_lines(files_dir, "$mount_path/files"))

    _create_scratch_image(buildah=buildah, image=image, body=body)


def _copy_files_to_shell_dir_lines(
    source_paths, destination_expr: str
) -> list[str]:
    lines = [f"mkdir -p {destination_expr}"]
    for source_path in source_paths:
        source_path = Path(source_path)
        lines.append(
            f"cp -a -- {shlex.quote(str(source_path))} {destination_expr}/{shlex.quote(source_path.name)}"
        )
    return lines


def _copy_tree_to_shell_dir_lines(source_dir: Path, destination_expr: str) -> list[str]:
    lines = []
    for source_path in source_dir.rglob("*"):
        if source_path.is_dir():
            continue
        relative = source_path.relative_to(source_dir).as_posix()
        lines.append(f"target={destination_expr}/{shlex.quote(relative)}")
        lines.append('mkdir -p "$(dirname "$target")"')
        lines.append(f"cp -a -- {shlex.quote(str(source_path))} \"$target\"")
    return lines


def _shell_command(command: list[str], raw_suffix: str = "") -> str:
    return " ".join(_shell_arg(str(part)) for part in command) + raw_suffix


def _shell_arg(value: str) -> str:
    if value.startswith("$") or value.startswith('"$'):
        return value
    return shlex.quote(value)


def _create_scratch_image(*, buildah: str, image: str, body: list[str]) -> None:
    buildah_command = shlex.quote(buildah)
    script = "\n".join(
        [
            "set -eu",
            "container=",
            "mounted=0",
            "cleanup_dirs=",
            "cleanup() {",
            '  if [ "$mounted" = 1 ]; then '
            f"{buildah_command} unmount \"$container\" >/dev/null 2>&1 || true; fi",
            '  if [ -n "$container" ]; then '
            f"{buildah_command} rm \"$container\" >/dev/null 2>&1 || true; fi",
            '  if [ -n "$cleanup_dirs" ]; then rm -rf $cleanup_dirs; fi',
            "}",
            "trap cleanup EXIT INT TERM",
            f"container=$({buildah_command} from --quiet scratch)",
            f"mount_path=$({buildah_command} mount \"$container\")",
            "mounted=1",
            *body,
            f"{buildah_command} unmount \"$container\" >/dev/null",
            "mounted=0",
            f"{buildah_command} commit --rm --quiet --format oci \"$container\" {shlex.quote(image)} >/dev/null",
            "container=",
        ]
    )
    returncode, _output = _run_streamed_command(
        [buildah, "unshare", "/bin/sh", "-s"],
        input_text=script + "\n",
    )
    if returncode != 0:
        raise ConfigError(f"scratch image build failed with exit status {returncode}")


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
        raise ConfigError(f"card build failed with exit status {returncode}")

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


def _run_specs_build(
    *,
    podman: str,
    orchestrator: str,
    build_dir: Path,
    artifact_cache_dir: Path,
    card_name: str,
    card_source: Path,
    card_env: dict[str, str],
    specs: tuple[SpecBuild, ...],
    prepare_script: str,
    arch: str,
) -> CardBuildOutput:
    workspace_dir = build_dir / "workspace"
    rpm_dir = build_dir / "rpms"
    files_dir = build_dir / "files"
    _remove_tree(build_dir, podman=podman)
    workspace_dir.mkdir(parents=True)
    rpm_dir.mkdir(parents=True)
    files_dir.mkdir(parents=True)
    artifact_cache_dir.mkdir(parents=True, exist_ok=True)

    staged_specs = _stage_card_specs(
        card_source=card_source,
        specs=specs,
        card_env=card_env,
        workspace_dir=workspace_dir,
        arch=arch,
    )
    if not staged_specs:
        raise ConfigError(f"{card_source}: specs build has no specs")

    if prepare_script.strip():
        prepared_env = _run_specs_prepare(
            podman=podman,
            orchestrator=orchestrator,
            workspace_dir=workspace_dir,
            rpm_dir=rpm_dir,
            files_dir=files_dir,
            artifact_cache_dir=artifact_cache_dir,
            card_env=card_env,
            prepare_script=prepare_script,
            card_source=card_source,
            card_name=card_name,
        )
        if prepared_env:
            card_env.update(prepared_env)

    build_script = _specs_build_script(staged_specs, workspace_dir, arch)
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
        f"{artifact_cache_dir}:/cache/artifacts",
        "--workdir",
        "/workspace",
    ]
    for key, value in sorted(card_env.items()):
        command.extend(["--env", f"{key}={value}"])
    command.extend(["--env", "PS4=+ "])
    command.extend([orchestrator, "/bin/sh", "-ex", "-s"])
    returncode, _output = _run_streamed_command(
        command,
        input_text=build_script,
        line_rewriter=_workspace_path_rewriter(
            source_dir=_card_base_dir(card_source),
            workspace_dir=workspace_dir,
            root_dir=Path.cwd(),
        ),
    )
    if returncode != 0:
        command_line = " ".join(shlex.quote(str(part)) for part in command)
        raise ConfigError(f"spec build failed with exit status {returncode}")

    rpm_files = tuple(sorted(path.name for path in rpm_dir.rglob("*.rpm")))
    log(f"Collected {len(rpm_files)} built RPMs for card: {card_name}")
    file_count = sum(1 for path in files_dir.rglob("*") if path.is_file())
    if file_count:
        log(f"Collected {file_count} built files for card: {card_name}")
    return CardBuildOutput(
        rpm_files=rpm_files,
        file_count=file_count,
        rpm_dir=rpm_dir,
        files_dir=files_dir,
    )


def _run_specs_prepare(
    *,
    podman: str,
    orchestrator: str,
    workspace_dir: Path,
    rpm_dir: Path,
    files_dir: Path,
    artifact_cache_dir: Path,
    card_env: dict[str, str],
    prepare_script: str,
    card_source: Path,
    card_name: str,
) -> dict[str, str]:
    env_file = workspace_dir / ".ludos-env"
    env_file.unlink(missing_ok=True)
    command = [
        podman,
        "run",
        "--rm",
        "--interactive",
        "--volume",
        f"{workspace_dir}:/workspace",
        "--volume",
        f"{rpm_dir}:/rpms",
        "--volume",
        f"{files_dir}:/files",
        "--volume",
        f"{artifact_cache_dir}:/cache/artifacts",
        "--workdir",
        "/workspace",
    ]
    for key, value in sorted(card_env.items()):
        command.extend(["--env", f"{key}={value}"])
    command.extend(["--env", "LUDOS_ENV=/workspace/.ludos-env"])
    command.extend(["--env", "PS4=+ "])
    command.extend([orchestrator, "/bin/sh", "-ex", "-s"])
    returncode, _output = _run_streamed_command(
        command,
        input_text=prepare_script + "\n",
        line_rewriter=_workspace_path_rewriter(
            source_dir=_card_base_dir(card_source),
            workspace_dir=workspace_dir,
            root_dir=Path.cwd(),
        ),
    )
    if returncode != 0:
        command_line = " ".join(shlex.quote(str(part)) for part in command)
        raise ConfigError(
            f"spec prepare failed with exit status {returncode}"
        )
    values = _load_dotenv(env_file)
    if values:
        log(f"Prepared {len(values)} environment values for card: {card_name}")
    return values


def _stage_card_specs(
    *,
    card_source: Path,
    specs: tuple[SpecBuild, ...],
    card_env: dict[str, str],
    workspace_dir: Path,
    arch: str,
) -> tuple[StagedSpec, ...]:
    _remove_tree(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    card_base_dir = _card_base_dir(card_source)
    ignore_rules = _load_containerignore(card_base_dir)
    staged = []
    for spec in specs:
        packages = _spec_packages_for_arch(spec, arch)
        if not packages:
            log(f"Skipping spec without packages on {arch}: {spec.spec}")
            continue
        spec_relpath = _validate_relative_file_path(spec.spec, card_source, "spec")
        spec_source = (card_base_dir / spec_relpath).resolve()
        try:
            spec_source.relative_to(card_base_dir)
        except ValueError as exc:
            raise ConfigError(f"{card_source}: spec '{spec.spec}' escapes the card") from exc
        if not spec_source.is_file():
            raise ConfigError(f"{card_source}: spec '{spec.spec}' is missing")

        relative_dir = spec_source.parent.relative_to(card_base_dir)
        staged_source_dir = workspace_dir / relative_dir
        if spec.files:
            shutil.rmtree(staged_source_dir, ignore_errors=True)
            staged_source_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(spec_source, staged_source_dir / spec_source.name)
            _copy_spec_files(
                spec_source.parent,
                staged_source_dir,
                spec.files,
                ignore_rules,
                card_source,
            )
        else:
            _copy_directory_contents(spec_source.parent, staged_source_dir, ignore_rules)
        staged_spec_path = staged_source_dir / spec_source.name
        _transform_staged_spec(
            staged_spec_path,
            spec.replace,
            card_env,
            arch,
        )
        staged.append(
            StagedSpec(
                spec=spec,
                spec_path=staged_spec_path,
                source_dir=staged_source_dir,
                packages=packages,
                targets=_spec_build_targets(packages, arch),
            )
        )
    return tuple(staged)


def _copy_spec_files(
    source_dir: Path,
    destination_dir: Path,
    patterns: tuple[str, ...],
    ignore_rules: tuple["_IgnoreRule", ...],
    card_source: Path,
) -> None:
    for pattern in patterns:
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise ConfigError(
                f"{card_source}: spec files entry '{pattern}' escapes the card"
            )
        matches = sorted(source_dir.glob(pattern))
        if not matches:
            if glob.has_magic(pattern):
                continue
            raise ConfigError(
                f"{card_source}: spec files entry '{pattern}' is missing"
            )
        for source_path in matches:
            source_path = source_path.resolve()
            try:
                relative_path = source_path.relative_to(source_dir).as_posix()
            except ValueError as exc:
                raise ConfigError(
                    f"{card_source}: spec files entry '{pattern}' escapes the card"
                ) from exc
            is_dir = source_path.is_dir()
            if _ignored_by_containerignore(relative_path, is_dir, ignore_rules):
                continue
            target_path = destination_dir / relative_path
            if is_dir:
                _copy_directory_contents(source_path, target_path, ignore_rules)
            elif source_path.is_file():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)


def _copy_directory_contents(
    source_dir: Path,
    destination_dir: Path,
    ignore_rules: tuple["_IgnoreRule", ...],
) -> None:
    source_dir = source_dir.resolve()
    shutil.rmtree(destination_dir, ignore_errors=True)
    for source_path in source_dir.rglob("*"):
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


def _transform_staged_spec(
    spec_path: Path,
    replacements: dict[str, str],
    card_env: dict[str, str],
    arch: str,
) -> None:
    text = spec_path.read_text(encoding="utf-8")
    for field, value in replacements.items():
        replacement = _expand_expression(value, card_env, None)
        field_name = field.rstrip(":").strip()
        pattern = re.compile(rf"^(\s*{re.escape(field_name)}\s*:\s*).*$", re.MULTILINE)
        text, count = pattern.subn(rf"\g<1>{replacement}", text, count=1)
        if count == 0:
            raise ConfigError(f"{spec_path}: replacement field '{field}' was not found")
    text = _prune_arch_sources(text, arch)
    text = _drop_nvidia_kmod_runtime_requires(text)
    spec_path.write_text(text, encoding="utf-8")


def _prune_arch_sources(text: str, arch: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Source"):
            lines.append(line)
            continue
        if arch == "x86_64" and "-aarch64.tar.xz" in stripped:
            continue
        if arch == "aarch64" and (
            "-x86_64.tar.xz" in stripped or "-i386.tar.xz" in stripped
        ):
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def _drop_nvidia_kmod_runtime_requires(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*Requires:\s+nvidia-kmod\s+=", line):
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def _spec_packages_for_arch(spec: SpecBuild, arch: str) -> tuple[str, ...]:
    return _packages_for_arch(spec.packages, arch)


def _packages_for_arch(
    packages_by_arch: dict[str, tuple[str, ...]],
    arch: str,
) -> tuple[str, ...]:
    packages = list(packages_by_arch.get(arch, packages_by_arch.get("*", tuple())))
    return tuple(dict.fromkeys(packages))


def _locally_built_package_ids_by_card(
    card_specs: dict[str, tuple[SpecBuild, ...]],
    arch: str,
) -> dict[str, set[tuple[str, str]]]:
    ids_by_card = {}
    for card_name, specs in card_specs.items():
        package_ids = set()
        for spec in specs:
            package_ids.update(_package_request_ids(_spec_packages_for_arch(spec, arch), arch))
        if package_ids:
            ids_by_card[card_name] = package_ids
    return ids_by_card


def _package_request_ids(
    packages: tuple[str, ...],
    default_arch: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        dict.fromkeys(
            package_id
            for package in packages
            for package_id in _package_request_ids_one(package, default_arch)
        )
    )


def _package_request_ids_one(
    package: str,
    default_arch: str,
) -> tuple[tuple[str, str], ...]:
    name, separator, suffix = package.rpartition(".")
    if separator and suffix in RPM_ARCH_SUFFIXES:
        return ((name, suffix),)
    return ((package, default_arch), (package, "noarch"))


def _spec_build_targets(packages: tuple[str, ...], arch: str) -> tuple[str, ...]:
    targets = [arch]
    if arch == "x86_64":
        if all(package.endswith(".i686") for package in packages):
            targets = ["i686"]
        elif any(package.endswith(".i686") for package in packages):
            targets.append("i686")
    return tuple(dict.fromkeys(targets))


def _spec_paths_by_build_target(
    staged_specs: tuple[StagedSpec, ...],
) -> tuple[tuple[str, tuple[Path, ...]], ...]:
    targets: dict[str, list[Path]] = {}
    for staged in staged_specs:
        for target in staged.targets:
            targets.setdefault(target, []).append(staged.spec_path)
    return tuple((target, tuple(paths)) for target, paths in targets.items())


def _resolve_spec_build_requires(
    orchestrator_dnf_base: list[str],
    releasever: str,
    workspace_dir: Path,
    spec_paths: tuple[Path, ...],
    arch: str,
    package_id_by_nevra: dict[str, tuple[str, str]],
    *,
    include_dependencies: bool = True,
) -> tuple[str, ...]:
    if not spec_paths:
        return tuple()
    spec_args = []
    for spec_path in spec_paths:
        relative = spec_path.relative_to(workspace_dir).as_posix()
        spec_args.append(f"/ludos/specs/{relative}")
    dnf_base = _dnf_base_with_volume(
        orchestrator_dnf_base,
        workspace_dir,
        "/ludos/specs:ro",
    )
    transaction_preview = subprocess.run(
        [
            *dnf_base,
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
            "builddep",
            "--allowerasing",
            "--define",
            f"_target_cpu {arch}",
            "--define",
            f"_target {arch}-redhat-linux-gnu",
            "--spec",
            *spec_args,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    output = transaction_preview.stdout + "\n" + transaction_preview.stderr
    if transaction_preview.returncode not in (0, 1):
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not resolve spec BuildRequires:\n{detail}")
    entries = _parse_resolved_package_entries(output, include_dependencies)
    package_id_by_nevra.update(entries)
    return tuple(package for package, _package_id in entries)


def _resolve_package_arch_variants(
    orchestrator_dnf_base: list[str],
    releasever: str,
    packages: tuple[str, ...],
    arch: str,
    package_id_by_nevra: dict[str, tuple[str, str]],
) -> tuple[str, ...]:
    candidates = tuple(
        _package_with_arch(package, arch)
        for package in packages
        if _is_arch_variant_candidate(package_id_by_nevra, package, arch)
    )
    if not candidates:
        return tuple()

    query = subprocess.run(
        [
            *orchestrator_dnf_base,
            "--setopt=reposdir=/ludos/dnf/repos",
            "--setopt=cachedir=/ludos/dnf/cache",
            "--setopt=persistdir=/ludos/dnf/persist",
            "--setopt=logdir=/ludos/dnf/log",
            "--disable-repo=*",
            "--enable-repo=*",
            f"--releasever={releasever}",
            "repoquery",
            "--queryformat",
            "%{name}\\t%{name}-%{evr}.%{arch}\\n",
            *candidates,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    output = query.stdout + "\n" + query.stderr
    if query.returncode != 0:
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not resolve {arch} package variants:\n{detail}")

    variants = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.endswith(":") or " " in stripped:
            continue
        if "\t" not in stripped:
            continue
        name, package = stripped.split("\t", 1)
        if package.endswith(f".{arch}"):
            package_id_by_nevra[package] = (name, arch)
            variants.append(package)
    return _unique_packages(tuple(variants))


def _package_with_arch(package: str, arch: str) -> str:
    return re.sub(r"\.[^.]+$", f".{arch}", package)


def _is_arch_variant_candidate(
    package_id_by_nevra: dict[str, tuple[str, str]],
    package: str,
    arch: str,
) -> bool:
    if package.endswith(f".{arch}") or package.endswith(".noarch"):
        return False
    name, _package_arch = _resolved_package_id(package_id_by_nevra, package)
    if name in {"libatomic", "libgcc"}:
        return True
    return name.endswith("-devel") or name.endswith("-static")


def _resolved_package_id(
    package_id_by_nevra: dict[str, tuple[str, str]],
    package: str,
) -> tuple[str, str]:
    try:
        return package_id_by_nevra[package]
    except KeyError as exc:
        raise ConfigError(f"missing package mapping for resolved package: {package}") from exc


def _dnf_base_with_volume(
    orchestrator_dnf_base: list[str],
    source: Path,
    target: str,
) -> list[str]:
    return [
        *orchestrator_dnf_base[:-2],
        "--volume",
        f"{source}:{target}",
        *orchestrator_dnf_base[-2:],
    ]


def _specs_build_script(
    staged_specs: tuple[StagedSpec, ...],
    workspace_dir: Path,
    arch: str,
) -> str:
    topdir = "/workspace/rpmbuild"
    lines = [
        "set -eux",
        f"topdir={shlex.quote(topdir)}",
        'source_cache="/cache/artifacts/sources"',
        'mkdir -p "$topdir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}',
        'mkdir -p "$source_cache"',
        'cat > "$topdir/ludos-meson-i686-cross.ini" <<\'LUDOS_MESON_I686_CROSS\'',
        "[binaries]",
        "c = ['gcc', '-m32']",
        "cpp = ['g++', '-m32']",
        "rust = ['rustc', '--target', 'i686-unknown-linux-gnu']",
        "rust_ld = ['gcc', '-m32']",
        "ar = 'gcc-ar'",
        "strip = 'strip'",
        "pkg-config = 'pkg-config'",
        "",
        "[properties]",
        "pkg_config_libdir = ['/usr/lib/pkgconfig', '/usr/share/pkgconfig']",
        "needs_exe_wrapper = false",
        "",
        "[host_machine]",
        "system = 'linux'",
        "cpu_family = 'x86'",
        "cpu = 'i686'",
        "endian = 'little'",
        "LUDOS_MESON_I686_CROSS",
        'cat > "$topdir/ludos-meson-i686" <<\'LUDOS_MESON_I686_WRAPPER\'',
        "#!/bin/sh",
        'if [ "${1:-}" = setup ]; then',
        '  exec /usr/bin/meson "$@" --cross-file "$LUDOS_MESON_CROSS_FILE"',
        "fi",
        'exec /usr/bin/meson "$@"',
        "LUDOS_MESON_I686_WRAPPER",
        'chmod +x "$topdir/ludos-meson-i686"',
    ]
    wanted = tuple(
        dict.fromkeys(package for staged in staged_specs for package in staged.packages)
    )
    lines.extend(
        [
            'cat > "$topdir/wanted.txt" <<\'LUDOS_WANTED_RPMS\'',
            *wanted,
            "LUDOS_WANTED_RPMS",
        ]
    )
    for staged in staged_specs:
        source_dir = f"/workspace/{staged.source_dir.relative_to(workspace_dir).as_posix()}"
        spec_path = f"/workspace/{staged.spec_path.relative_to(workspace_dir).as_posix()}"
        spec_name = staged.spec_path.name
        targets = " ".join(shlex.quote(target) for target in staged.targets)
        lines.extend(
            [
                f"find {shlex.quote(source_dir)} -maxdepth 1 -type f ! -name '*.spec' -exec cp -f -t \"$topdir/SOURCES\" {{}} +",
                f"cp -f {shlex.quote(spec_path)} \"$topdir/SPECS/{shlex.quote(spec_name)}\"",
                f"if grep -Eq '^(Source|Patch)[0-9]*:[[:space:]]+https?://' \"$topdir/SPECS/{shlex.quote(spec_name)}\"; then",
                f"  spectool -l \"$topdir/SPECS/{shlex.quote(spec_name)}\" > \"$topdir/sources.list\"",
                "  missing_sources=0",
                "  while IFS= read -r source_entry; do",
                "    source_url=${source_entry#*:}",
                "    source_url=$(printf '%s\\n' \"$source_url\" | sed 's/^[[:space:]]*//')",
                "    case \"$source_url\" in http://*|https://*) ;; *) continue ;; esac",
                "    source_name=${source_url##*/}",
                "    source_name=${source_name%%\\?*}",
                "    if [ ! -f \"$source_cache/$source_name\" ]; then",
                "      missing_sources=1",
                "      break",
                "    fi",
                "  done < \"$topdir/sources.list\"",
                "  if [ \"$missing_sources\" -eq 1 ]; then",
                f"    spectool -g -C \"$source_cache\" \"$topdir/SPECS/{shlex.quote(spec_name)}\"",
                "  fi",
                '  find "$source_cache" -maxdepth 1 -type f -exec cp -n -t "$topdir/SOURCES" {} +',
                "fi",
                f"for target in {targets}; do",
                "  if [ \"$target\" = i686 ]; then",
                "    export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig",
                "    export PKG_CONFIG_PATH=",
                "    export BINDGEN_EXTRA_CLANG_ARGS=\"${BINDGEN_EXTRA_CLANG_ARGS:+$BINDGEN_EXTRA_CLANG_ARGS }-m32\"",
                "    export LDFLAGS=\"${LDFLAGS:+$LDFLAGS }-Wl,--no-warn-rwx-segments\"",
                "    export LUDOS_MESON_CROSS_FILE=\"$topdir/ludos-meson-i686-cross.ini\"",
                "    if [ -x /usr/lib/llvm22/bin/llvm-config ]; then",
                "      export LLVM_CONFIG=/usr/lib/llvm22/bin/llvm-config",
                "      export PATH=/usr/lib/llvm22/bin:$PATH",
                "    fi",
                "  fi",
                f"  echo {shlex.quote(f'Building packages from {topdir}/SPECS/{spec_name}')}",
                "  if [ \"$target\" = i686 ]; then",
                f"    rpmbuild -ba \"$topdir/SPECS/{shlex.quote(spec_name)}\" --target \"$target\" --define \"_topdir $topdir\" --define \"__meson $topdir/ludos-meson-i686\"",
                "  else",
                f"    rpmbuild -ba \"$topdir/SPECS/{shlex.quote(spec_name)}\" --target \"$target\" --define \"_topdir $topdir\"",
                "  fi",
                "done",
            ]
        )
    lines.extend(
        [
            'find "$topdir/RPMS" -type f -name "*.rpm" | sort | while read -r rpm; do',
            "  name=$(rpm -qp --queryformat '%{NAME}' \"$rpm\")",
            "  rpm_arch=$(rpm -qp --queryformat '%{ARCH}' \"$rpm\")",
            '  if grep -Fxq "$name.$rpm_arch" "$topdir/wanted.txt"; then',
            '    cp -f "$rpm" /rpms/',
            '    echo "$name.$rpm_arch" >> "$topdir/matched.txt"',
            f"  elif [ \"$rpm_arch\" = {shlex.quote(arch)} ] || [ \"$rpm_arch\" = noarch ]; then",
            '    if grep -Fxq "$name" "$topdir/wanted.txt"; then',
            '      cp -f "$rpm" /rpms/',
            '      echo "$name" >> "$topdir/matched.txt"',
            "    fi",
            "  fi",
            "done",
            'touch "$topdir/matched.txt"',
            'while read -r wanted; do',
            '  grep -Fxq "$wanted" "$topdir/matched.txt" || { echo "Missing built RPM for $wanted"; exit 1; }',
            'done < "$topdir/wanted.txt"',
        ]
    )
    return "\n".join(lines) + "\n"


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


def _card_specs_hash(
    card_source: Path,
    specs: tuple[SpecBuild, ...],
    card_env: dict[str, str],
    prepare_script: str,
) -> str:
    card_base_dir = _card_base_dir(card_source)
    digest = hashlib.sha256()
    digest.update(card_source.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(prepare_script.encode("utf-8"))
    digest.update(b"\0")
    for key, value in sorted(card_env.items()):
        digest.update(key.encode("utf-8"))
        digest.update(b"=")
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\0")
    for spec in specs:
        digest.update(spec.spec.encode("utf-8"))
        digest.update(b"\0")
        for key, value in sorted(spec.replace.items()):
            digest.update(key.encode("utf-8"))
            digest.update(b"=")
            digest.update(_expand_expression(value, card_env, None).encode("utf-8"))
            digest.update(b"\0")
        for arch, packages in sorted(spec.packages.items()):
            digest.update(arch.encode("utf-8"))
            digest.update(b"\0")
            for package in packages:
                digest.update(package.encode("utf-8"))
                digest.update(b"\0")
        spec_path = card_base_dir / spec.spec
        spec_dir = spec_path.parent
        if spec.files:
            hash_paths = (
                spec_path.relative_to(card_base_dir).as_posix(),
                *_spec_file_hash_paths(
                    card_source,
                    card_base_dir,
                    spec_dir,
                    spec.files,
                ),
            )
        else:
            hash_paths = (spec_dir.relative_to(card_base_dir).as_posix(),)
        digest.update(_hash_paths(card_base_dir, hash_paths).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:HASH_LENGTH]


def _spec_file_hash_paths(
    card_source: Path,
    card_base_dir: Path,
    spec_dir: Path,
    patterns: tuple[str, ...],
) -> tuple[str, ...]:
    paths = []
    for pattern in patterns:
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise ConfigError(
                f"{card_source}: spec files entry '{pattern}' escapes the card"
            )
        matches = sorted(spec_dir.glob(pattern))
        if not matches:
            if glob.has_magic(pattern):
                continue
            raise ConfigError(
                f"{card_source}: spec files entry '{pattern}' is missing"
            )
        for match in matches:
            try:
                paths.append(match.relative_to(card_base_dir).as_posix())
            except ValueError as exc:
                raise ConfigError(
                    f"{card_source}: spec files entry '{pattern}' escapes the card"
                ) from exc
    return tuple(paths)


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
    raise ConfigError(f"{description} failed with exit status {returncode}")


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
    message = f"command failed with exit status {returncode}"
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
        line = re.sub(
            r"(?<![A-Za-z0-9_./-])/workspace/rpmbuild(?=/?|\W)",
            f"{workspace}/rpmbuild",
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


def _substitute_variables(value: str, variables: dict[str, str]) -> str:
    for key, replacement in variables.items():
        value = value.replace(f"${key}", replacement)
    return value

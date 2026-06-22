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
from .model import ConfigError, SpecBuild, _resolve_card_path, validate_manifest


HASH_LENGTH = 8
CCACHE_CONTAINER_DIR = "/cache/ccache"
CCACHE_PATH_PREFIX = "/usr/lib64/ccache:/usr/lib/ccache"
SCCACHE_CONTAINER_DIR = f"{CCACHE_CONTAINER_DIR}/sccache"
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
ENV_ALWAYS_AVAILABLE = ("arch", "releasever")


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
    spec_source_cache_dir: str
    build_artifact_cache_dir: str
    ccache_dir: str | None
    dnf_workspace_dir: str
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
    spec_source_revisions: tuple[tuple[str, str, str], ...]


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


@dataclass(frozen=True)
class SpecSource:
    base_dir: Path
    spec_path: Path
    spec_relpath: Path
    revision: str = ""
    stage_prefix: Path = Path(".")


def _split_target_card(value: str) -> tuple[str, str | None]:
    card_text, separator, spec = value.rpartition(":")
    if not separator:
        return value, None
    if not card_text or not spec:
        raise ConfigError("targeted card spec must be '<card>:<spec>'")
    return card_text, spec


def _select_target_spec(
    card_source: Path,
    specs: tuple[SpecBuild, ...],
    spec_key: str,
    arch: str,
) -> SpecBuild:
    matches = []
    for spec in specs:
        if spec_key in _target_spec_keys(card_source, spec):
            matches.append(spec)

    if not matches:
        available = ", ".join(spec.spec for spec in specs)
        raise ConfigError(
            f"{card_source}: spec '{spec_key}' not found. Available: {available}"
        )
    if len(matches) > 1:
        raise ConfigError(f"{card_source}: ambiguous spec '{spec_key}'")

    spec = matches[0]
    if not _spec_packages_for_arch(spec, arch):
        raise ConfigError(f"{card_source}: spec '{spec_key}' has no packages on {arch}")
    return spec


def _target_spec_keys(card_source: Path, spec: SpecBuild) -> tuple[str, ...]:
    spec_path = Path(spec.spec)
    keys = [spec.spec, spec_path.name, spec_path.stem]
    if spec.patch is not None and spec.patch.type == "git":
        if spec.patch.name:
            keys.append(spec.patch.name)
        elif not _is_git_source(spec.spec):
            source_dir = spec_path.parent
            if not source_dir.is_absolute() and ".." not in source_dir.parts:
                source_key = source_dir.as_posix()
                if source_key == ".":
                    source_key = _card_base_dir(card_source).name
                keys.append(source_key)
    return tuple(dict.fromkeys(keys))


def build_manifest(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ci: bool = False,
    ccache: bool = True,
    card: str | None = None,
) -> BuildResult:
    metadata: tuple[ResolvedBuildMetadata, ...] = tuple()
    try:
        metadata = resolve_build_manifests(
            (manifest_path,),
            cards_dir=cards_dir,
            cache_dir=cache_dir,
            cache_version=cache_version,
            cache_only=cache_only,
            ccache=ccache,
            card=card,
        )
        if card is not None:
            target = _resolve_target_card(
                metadata[0],
                manifest_path=manifest_path,
                cards_dir=cards_dir,
                card=card,
            )
            build_builder_images(metadata, targets=(target,), cache_only=cache_only)
            build_outputs = build_build_images(
                metadata,
                targets=(target,),
                cache_only=cache_only,
            )
            return _target_card_build_result(metadata[0], target, build_outputs)

        build_package_card_images(metadata, cache_only=cache_only)
        build_outputs = build_build_images(metadata, cache_only=cache_only)
        return build_final_manifest_images(
            metadata,
            build_outputs=build_outputs,
            mode="combined" if ci else "separated",
            cache_only=cache_only,
        )[0]
    finally:
        _cleanup_dnf_workspaces(metadata)


def _resolve_target_card(
    metadata: ResolvedBuildMetadata,
    *,
    manifest_path: Path,
    cards_dir: Path | None,
    card: str,
) -> str:
    root_dir = manifest_path.resolve().parent
    target_card, _target_spec = _split_target_card(card)
    target_source = _resolve_card_path(target_card, root_dir, cards_dir).resolve()
    blocks_by_source = {
        Path(source).resolve(): block
        for block, source in metadata.card_sources
    }
    target = blocks_by_source.get(target_source)
    if target is None:
        raise ConfigError(f"{manifest_path}: card not listed in manifest: {card}")
    if target not in {plan.block for plan in metadata.build_images}:
        raise ConfigError(f"{target_source}: card has no build or specs")
    return target


def resolve_build_manifests(
    manifest_paths: tuple[Path, ...],
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ccache: bool = True,
    card: str | None = None,
) -> tuple[ResolvedBuildMetadata, ...]:
    if not manifest_paths:
        raise ConfigError("at least one manifest is required")
    if card is not None and len(manifest_paths) != 1:
        raise ConfigError("targeted card builds require exactly one manifest")
    dnf_workspace_dirs: list[Path] = []
    try:
        metadata = tuple(
            _resolve_manifest_metadata(
                manifest_path,
                cards_dir=cards_dir,
                cache_dir=cache_dir,
                cache_version=cache_version,
                cache_only=cache_only,
                ccache=ccache,
                target_card=card,
                dnf_workspace_dirs=dnf_workspace_dirs,
            )
            for manifest_path in manifest_paths
        )
        return _merge_common_packages(metadata)
    except Exception:
        _cleanup_dnf_workspace_paths(tuple(dnf_workspace_dirs))
        raise


def _resolve_manifest_metadata(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ccache: bool = True,
    target_card: str | None = None,
    dnf_workspace_dirs: list[Path] | None = None,
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
    if cache_version is None:
        cache_version = _datetime.date.today().strftime("%Y%m%d")
        load_only_version = False
    else:
        cache_version = _cache_name(cache_version, "version")
        load_only_version = True
    manifest_env["version"] = cache_version
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
    manifest_env = {
        key: _substitute_variables(value, manifest_env)
        for key, value in manifest_env.items()
    }
    distro = _cache_name(
        _substitute_variables(validation.manifest.distro, manifest_env),
        "distro",
    )
    orchestrator_source = _substitute_variables(
        validation.manifest.orchestrator, manifest_env
    )
    output_image = f"localhost/{local_prefix}{image}:{distro}"
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
    dnf_resolve_dir = dnf_dir / "resolves"
    build_artifact_cache_dir = distro_cache_dir / "build-artifacts"
    spec_source_cache_dir = cache_dir / "spec-sources" / "git"
    ccache_dir = cache_dir / "ccache" if ccache else None

    distro_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_dir.mkdir(parents=True, exist_ok=True)
    dnf_workspace_dir = Path(tempfile.mkdtemp(prefix="run-", dir=dnf_dir))
    if dnf_workspace_dirs is not None:
        dnf_workspace_dirs.append(dnf_workspace_dir)
    repo_dir = dnf_workspace_dir / "repos"
    dnf_cache_dir = dnf_workspace_dir / "cache"
    dnf_persist_dir = dnf_workspace_dir / "persist"
    dnf_log_dir = dnf_workspace_dir / "log"
    package_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    card_build_dir.mkdir(parents=True, exist_ok=True)
    build_artifact_cache_dir.mkdir(parents=True, exist_ok=True)
    spec_source_cache_dir.mkdir(parents=True, exist_ok=True)
    if ccache_dir is not None:
        ccache_dir.mkdir(parents=True, exist_ok=True)
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

    log(f"Using DNF metadata workspace: {dnf_workspace_dir}")

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
            _apply_repo_priority(repo_dir / repo.source.name, repo.ref.priority)
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
        _apply_repo_priority(repo_dir / repo.source.name, repo.ref.priority)

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
    target_card_name = None
    target_spec = None
    if target_card is not None:
        target_card_path, target_spec = _split_target_card(target_card)
        target_source = _resolve_card_path(
            target_card_path,
            root_dir,
            cards_dir,
        ).resolve()
        for _priority, _insertion_order, card_name, card in card_entries:
            if card.source is not None and card.source.resolve() == target_source:
                target_card_name = card_name
                break
        if target_card_name is None:
            raise ConfigError(
                f"{manifest_path}: card not listed in manifest: {target_card}"
            )

    card_requests = []
    card_names = []
    card_file_sets = []
    card_builds = {}
    card_specs = {}
    card_build_specs = {}
    card_build_deps = {}
    card_hashes = {}
    card_spec_hashes = {}
    card_build_spec_hashes = {}
    spec_source_revisions = {}
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
    requested_packages = [] if target_card_name is not None else list(bootstrap_packages)
    for _priority, _insertion_order, card_name, card in card_entries:
        active_target = target_card_name is None or card_name == target_card_name
        if card.source is None:
            raise ConfigError(f"card '{card_name}' has no source path")
        if card.build.strip() and card.specs:
            raise ConfigError(f"{card.source}: card cannot define both build and specs")
        card_env = _card_env(inherited_env, card.env)
        inherited_env.update({k: v for k, v in card_env.items() if k not in inherited_env})
        card_names.append(card_name)
        card_envs[card_name] = card_env
        # log(f"{card_name}: {card_env} {inherited_env}")
        card_sources[card_name] = card.source
        if card.prepare.strip():
            card_prepare_scripts[card_name] = card.prepare.rstrip()
        card_packages = list(_packages_for_arch(card.packages, arch))
        if target_card_name is None:
            for package in card_packages:
                requested_packages.append(package)
        card_requests.append(tuple(card_packages))
        parsed_file_refs = tuple(_parse_file_ref(file_ref) for file_ref in card.files)
        card_file_sets.append((card_name, card.source, parsed_file_refs))
        if active_target and card.build.strip():
            card_builds[card_name] = card.build.rstrip()
            card_build_deps[card_name] = card.build_deps
        if active_target and card.specs:
            active_specs = tuple(
                spec for spec in card.specs if _spec_packages_for_arch(spec, arch)
            )
            if active_specs:
                card_specs[card_name] = active_specs
            else:
                log(f"Skipping specs for card without packages on {arch}: {card_name}")
            if target_spec is not None and active_specs:
                card_build_specs[card_name] = (
                    _select_target_spec(
                        card.source,
                        card.specs,
                        target_spec,
                        arch,
                    ),
                )
        if card_name in card_specs:
            card_build_deps[card_name] = card.build_deps
            card_spec_hash, card_spec_revisions = _card_specs_hash(
                card.source,
                card_specs[card_name],
                card_env,
                card.prepare.rstrip(),
                spec_source_cache_dir,
                hash_expression=card.hash.strip(),
                cache_only=cache_only,
            )
            card_spec_hashes[card_name] = card_spec_hash
            if card_spec_revisions:
                spec_source_revisions[card_name] = card_spec_revisions
            build_specs = card_build_specs.get(card_name)
            if build_specs is not None and build_specs != card_specs[card_name]:
                build_spec_hash, _ = _card_specs_hash(
                    card.source,
                    build_specs,
                    card_env,
                    card.prepare.rstrip(),
                    spec_source_cache_dir,
                    hash_expression=card.hash.strip(),
                    cache_only=cache_only,
                )
                card_build_spec_hashes[card_name] = build_spec_hash
        if active_target and target_spec is not None and not card.specs:
            raise ConfigError(f"{card.source}: card has no specs")
        if card.hash.strip():
            card_hashes[card_name] = card.hash.strip()
        if card.postprocess.strip():
            postprocess_blocks.append((card_name, card.postprocess.rstrip()))
    build_card_names = set(card_builds) | set(card_specs)
    if target_card_name is not None and target_card_name not in build_card_names:
        target_source = Path(card_sources[target_card_name]).resolve()
        raise ConfigError(f"{target_source}: card has no build or specs")
    package_id_by_nevra: dict[str, tuple[str, str]] = {}
    requested_packages = tuple(requested_packages)
    if not requested_packages and not build_card_names:
        raise ConfigError(f"{manifest_path}: no packages requested by cards")
    if target_card_name is None:
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

    if target_card_name is not None:
        log(
            "Skipping package transaction resolution for targeted card build: "
            f"{target_card_name}"
        )
        bootstrap_resolved_packages = tuple()
        card_resolutions = [tuple() for _card_name in card_names]
        common_packages = []
        package_blocks = tuple()
        package_block_hashes = tuple()
        resolved_packages = tuple()
    else:
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
                    log(
                        "Package resolution not needed for build-only card: "
                        f"{card_name}"
                    )
                else:
                    log(
                        "Skipping package resolution for package-less card: "
                        f"{card_name}"
                    )
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
        for index, (card_name, card_packages) in enumerate(
            zip(card_names, card_requests)
        ):
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
            package
            for package in common_package_set
            if package not in bootstrap_package_set
        }
        seen_common_packages = set()
        package_blocks = []
        common_packages = []
        for card_resolution in card_resolutions:
            for package in card_resolution:
                if (
                    package not in common_package_set
                    or package in seen_common_packages
                ):
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
                spec_source_cache_dir=spec_source_cache_dir,
                cache_only=True,
                source_revisions=spec_source_revisions.get(card_name, tuple()),
            )
            spec_builder_packages = _resolve_staged_spec_builder_packages(
                orchestrator_dnf_base,
                releasever,
                spec_scan_dir,
                staged_specs,
                arch,
                package_id_by_nevra,
                dnf_resolve_dir,
                tuple(repo_images),
                card_name=card_name,
            )
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
            for spec in card_build_specs.get(card_name, card_specs[card_name]):
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

    build_image_blocks = package_blocks
    if target_card_name is not None:
        target_index = card_names.index(target_card_name)
        build_image_blocks = ((target_card_name, card_requests[target_index]),)

    for block_name, block_packages in build_image_blocks:
        if block_name not in build_card_names:
            continue
        if block_name in card_build_spec_hashes:
            build_hash = card_build_spec_hashes[block_name]
        elif block_name in card_spec_hashes:
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

    manifest_labels = tuple(
        (key, _substitute_variables(value, manifest_env))
        for key, value in validation.manifest.labels.items()
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
        manifest_labels=manifest_labels,
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
        spec_source_cache_dir=str(spec_source_cache_dir),
        build_artifact_cache_dir=str(build_artifact_cache_dir),
        ccache_dir=str(ccache_dir) if ccache_dir is not None else None,
        dnf_workspace_dir=str(dnf_workspace_dir),
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
        card_specs=tuple(
            (
                card_name,
                card_build_specs.get(card_name, specs),
            )
            for card_name, specs in card_specs.items()
        ),
        spec_source_revisions=tuple(
            (card_name, spec_source, revision)
            for card_name, revisions in spec_source_revisions.items()
            for spec_source, revision in revisions
        ),
    )


def resolve_manifest_images(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
) -> BuildResult:
    metadata: tuple[ResolvedBuildMetadata, ...] = tuple()
    try:
        metadata = resolve_build_manifests(
            (manifest_path,),
            cards_dir=cards_dir,
            cache_dir=cache_dir,
            cache_version=cache_version,
            cache_only=True,
        )
        return _metadata_build_result(metadata[0])
    finally:
        _cleanup_dnf_workspaces(metadata)


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

    build_builder_images(metadata, cache_only=cache_only)


def build_builder_images(
    metadata: tuple[ResolvedBuildMetadata, ...],
    *,
    targets: tuple[str, ...] = tuple(),
    cache_only: bool = False,
) -> None:
    target_set = set(targets)
    built_builders: set[str] = set()
    for manifest in metadata:
        for plan in manifest.build_images:
            if (
                target_set
                and plan.block not in target_set
                and plan.image not in target_set
                and plan.builder_image not in target_set
            ):
                continue
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
        spec_source_revisions = _spec_source_revisions_by_card(
            manifest.spec_source_revisions
        )

        for plan in manifest.build_images:
            if target_set and plan.block not in target_set and plan.image not in target_set:
                continue

            if _image_exists(manifest.podman, plan.image):
                log(f"Reusing build output image: {plan.image}")
                images_by_block[plan.block] = plan.image
                rpm_files, has_files = _build_output_metadata_in_image(
                    manifest.podman, plan.image
                )
                rpm_files_by_block[plan.block] = rpm_files
                if has_files:
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
                    ccache_dir=(
                        Path(manifest.ccache_dir)
                        if manifest.ccache_dir is not None
                        else None
                    ),
                    card_name=plan.block,
                    card_source=card_sources[plan.block],
                    card_env=card_env,
                    specs=card_specs[plan.block],
                    prepare_script=card_prepare_scripts.get(plan.block, ""),
                    arch=manifest.arch,
                    spec_source_cache_dir=Path(manifest.spec_source_cache_dir),
                    source_revisions=spec_source_revisions.get(plan.block, tuple()),
                )
            else:
                build_output = _run_card_build(
                    podman=manifest.podman,
                    orchestrator=plan.builder_image,
                    build_dir=Path(manifest.card_build_dir) / _identifier(plan.block),
                    artifact_cache_dir=Path(manifest.build_artifact_cache_dir)
                    / _identifier(plan.block),
                    ccache_dir=(
                        Path(manifest.ccache_dir)
                        if manifest.ccache_dir is not None
                        else None
                    ),
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
            rpm_files, has_files = _build_output_metadata_in_image(
                manifest.podman, plan.image
            )
            rpm_files_by_block[plan.block] = rpm_files
            if has_files:
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
    cache_only: bool = False,
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
                cache_only=cache_only,
            )
        )
    return tuple(results)


def _build_final_manifest_image(
    metadata: ResolvedBuildMetadata,
    *,
    build_outputs: BuildImageOutputs,
    mode: str,
    cache_only: bool,
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
            remote_cache_path = git_cache_dir / target_relpath
            if _is_http_source(file_ref.source):
                _copy_http_file_source(
                    file_ref.source,
                    target_path,
                    remote_cache_path,
                    cache_only=cache_only,
                )
            elif _is_git_source(file_ref.source):
                _copy_git_file_source(
                    file_ref.source,
                    target_path,
                    remote_cache_path,
                    cache_only=cache_only,
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
    subprocess.run(
        [metadata.podman, "tag", metadata.output_image, _latest_image(metadata.output_image)],
        check=True,
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
# /run/ostree-booted changes the post scripts of a variety of packages
mkdir -p /target/run
install -m 0755 /dev/null /target/run/ostree-booted
install -m 0755 /dev/null /target/usr/lib/kernel
cat > /usr/lib/kernel/install.conf <<'EOF'
# kernel-install will not try to run dracut and allows the image builder to
# take over initramfs generation. This also tells tooling to keep one kernel.
layout=ostree
EOF
{_dnf_install_script(metadata.releasever, bootstrap_paths, installroot="/target")}
LUDOS_BOOTSTRAP
"""

    card_packages = dict(metadata.card_packages)
    card_resolutions = dict(metadata.card_resolutions)
    postprocess_blocks = dict(metadata.postprocess_blocks)
    card_envs = {
        card_name: dict(values)
        for card_name, values in metadata.card_envs
    }
    package_id_by_nevra = {
        package: (name, arch)
        for package, name, arch in metadata.package_ids
    }
    built_package_ids_by_block = {
        plan.block: set(plan.declared_package_ids)
        for plan in metadata.build_images
    }
    all_built_package_ids = set().union(
        *built_package_ids_by_block.values()
    ) if built_package_ids_by_block else set()
    install_steps = []
    postprocess_steps = []

    if mode == "combined":
        install_paths = _rpm_paths_for_packages(
            "/rpms/common",
            tuple(
                package
                for package in metadata.common_packages
                if _resolved_package_id(package_id_by_nevra, package)
                not in all_built_package_ids
            ),
        )
        mounts = [
            (
                "type=bind",
                f"from={common_stage}",
                "source=/rpms",
                "target=/rpms/common",
                "ro",
            )
        ]
        build_images = []
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
                build_images.append(build_images_by_block[card_name])
                install_paths += tuple(
                    f"/rpms/{_identifier(card_name)}-build/{rpm_file}"
                    for rpm_file in build_rpm_files
                )
        if install_paths:
            install_steps.append(
                _run_with_mounts(
                    mounts,
                    "LUDOS_INSTALL",
                    _dnf_install_script(
                        metadata.releasever,
                        install_paths,
                        cache_images=tuple(build_images),
                    ),
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
        installed_built_package_ids = set()
        common_set = set(metadata.common_packages)
        for card_name in metadata.card_order:
            card_built_package_ids = built_package_ids_by_block.get(card_name, set())
            skipped_built_package_ids = (
                installed_built_package_ids | card_built_package_ids
            )
            mounts = [
                (
                    "type=bind",
                    f"from={common_stage}",
                    "source=/rpms",
                    "target=/rpms/common",
                    "ro",
                )
            ]
            build_images = []
            common_needed = tuple(
                package
                for package in card_resolutions.get(card_name, tuple())
                if package in common_set and package not in installed_common
                and _resolved_package_id(package_id_by_nevra, package)
                not in skipped_built_package_ids
            )
            installed_common.update(
                package
                for package in card_resolutions.get(card_name, tuple())
                if package in common_set
                and _resolved_package_id(package_id_by_nevra, package)
                in skipped_built_package_ids
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
                build_images.append(build_images_by_block[card_name])
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
    _dnf_install_script(
        metadata.releasever,
        install_paths,
        cache_images=tuple(build_images),
    ),
)}
"""
                )
            installed_built_package_ids.update(card_built_package_ids)
            if card_name in postprocess_blocks:
                postprocess_steps.append(
                    _postprocess_step(
                        card_name,
                        postprocess_blocks[card_name],
                        card_name in card_file_cards,
                        card_name in build_file_blocks,
                        build_stage_names.get(card_name, ""),
                        card_envs.get(card_name, {}),
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
# Run postprocessing
#

{postprocess_step_lines}
{label_lines}"""


def _dnf_install_script(
    releasever: str,
    rpm_paths: tuple[str, ...],
    *,
    installroot: str | None = None,
    cache_images: tuple[str, ...] = tuple(),
) -> str:
    cache_comments = _mounted_image_cache_comments(cache_images)
    if not rpm_paths:
        return f"{cache_comments}set -e\nexit 0\n"
    installroot_line = f"    --installroot={installroot} \\\n" if installroot else ""
    clean_root = installroot or ""
    clean_cache = f"{clean_root}/var/cache/dnf".replace("//", "/")
    clean_logs = f"{clean_root}/var/log/dnf*".replace("//", "/")
    rpm_lines = " \\\n".join(f"    {shlex.quote(path)}" for path in rpm_paths)
    return f"""{cache_comments}set -e
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


def _mounted_image_cache_comments(images: tuple[str, ...]) -> str:
    return "".join(f"# build-image: {_image_tag(image)}\n" for image in images)


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
    card_env: dict[str, str],
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
    postprocess = _substitute_variables(postprocess, card_env)
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
    card_envs = {
        card_name: dict(values)
        for card_name, values in metadata.card_envs
    }
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
        postprocess = _substitute_variables(
            postprocess,
            card_envs.get(card_name, {}),
        )
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


def _build_output_metadata_in_image(
    podman: str,
    image: str,
) -> tuple[tuple[str, ...], bool]:
    script = r"""
image=$1
mount_path=$(podman image mount "$image")
cleanup() { podman image unmount "$image" >/dev/null; }
trap cleanup EXIT
if [ -d "$mount_path/rpms" ]; then
  find "$mount_path/rpms" -maxdepth 1 -type f -name "*.rpm" -printf "R\t%f\n" | sort
fi
if [ -d "$mount_path/files" ] && [ -n "$(find "$mount_path/files" -type f -print -quit)" ]; then
  printf "F\t1\n"
else
  printf "F\t0\n"
fi
"""
    listing = subprocess.run(
        [podman, "unshare", "/bin/sh", "-eu", "-c", script, "--", image],
        check=True,
        text=True,
        capture_output=True,
    )
    rpm_files = []
    has_files = False
    for line in listing.stdout.splitlines():
        if line.startswith("R\t"):
            rpm_files.append(line[2:])
        elif line == "F\t1":
            has_files = True
    return tuple(rpm_files), has_files


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


def _target_card_build_result(
    metadata: ResolvedBuildMetadata,
    target: str,
    build_outputs: BuildImageOutputs,
) -> BuildResult:
    package_blocks = tuple(
        (block_name, block_packages)
        for block_name, block_packages in metadata.card_packages
        if block_name == target
    )
    build_images_by_block = dict(build_outputs.images_by_block)
    build_plans = tuple(plan for plan in metadata.build_images if plan.block == target)
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
        package_images=tuple(),
        build_images=tuple(build_images_by_block.values()),
        build_blocks=tuple(build_images_by_block),
        builder_images=tuple(plan.builder_image for plan in build_plans),
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


def _latest_image(image: str) -> str:
    repository, _tag = image.rsplit(":", 1)
    return f"{repository}:latest"


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
        subprocess.run([podman, "tag", image, _latest_image(image)], check=True)
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
    subprocess.run([podman, "tag", image, _latest_image(image)], check=True)


def _repo_id(rendered_repo: str, source: Path) -> str:
    for line in rendered_repo.splitlines():
        match = re.fullmatch(r"\[([^]]+)]", line.strip())
        if match:
            return match.group(1)
    raise ConfigError(f"{source}: repository definition does not contain a repo id")


def _apply_repo_priority(repo_file: Path, priority: int) -> None:
    lines = [
        line
        for line in repo_file.read_text(encoding="utf-8").rstrip().splitlines()
        if not line.strip().startswith("priority=")
    ]
    lines.append(f"priority={priority}")
    repo_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    if not entries:
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not resolve packages:\n{detail}")
    package_id_by_nevra.update(entries)
    return tuple(package for package, _package_id in entries)


def _run_cached_transaction_preview(
    cmd: list[str],
    resolve_cache_dir: Path,
    repo_images: tuple[str, ...],
    extra_hash_inputs: tuple[tuple[str, str], ...] = tuple(),
) -> subprocess.CompletedProcess[str]:
    repo_tags = tuple(_image_tag(image) for image in repo_images)
    cache_hash_inputs = (*extra_hash_inputs, *_dnf_repo_hash_inputs(cmd))
    cache_key = _resolve_cache_key(cmd, repo_tags, cache_hash_inputs)
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
        "extra_hash_inputs": cache_hash_inputs,
        "returncode": transaction_preview.returncode,
        "stdout": transaction_preview.stdout,
        "stderr": transaction_preview.stderr,
    }
    fd, temp_name = tempfile.mkstemp(
        prefix=f"{cache_file.stem}.",
        suffix=".tmp",
        dir=resolve_cache_dir,
    )
    os.close(fd)
    temp_file = Path(temp_name)
    temp_file.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temp_file.replace(cache_file)
    return transaction_preview


def _resolve_cache_key(
    cmd: list[str],
    repo_tags: tuple[str, ...],
    extra_hash_inputs: tuple[tuple[str, str], ...] = tuple(),
) -> str:
    payload = json.dumps(
        {
            "cmd": _normalized_dnf_workspace_mounts(cmd),
            "extra_hash_inputs": extra_hash_inputs,
            "repo_tags": repo_tags,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_dnf_workspace_mounts(cmd: list[str]) -> list[str]:
    normalized = []
    for arg in cmd:
        normalized.append(
            re.sub(
                r"[^:]+/dnf/run-[^:/]+/(repos|cache|persist|log)"
                r":/ludos/dnf/\1(?=(:ro)?$)",
                r"<dnf-workspace>/\1:/ludos/dnf/\1",
                arg,
            )
        )
    return normalized


def _dnf_repo_hash_inputs(cmd: list[str]) -> tuple[tuple[str, str], ...]:
    repo_dir = _dnf_repo_dir(cmd)
    if repo_dir is None or not repo_dir.is_dir():
        return tuple()
    return tuple(
        (f"repo:{path.relative_to(repo_dir).as_posix()}", _hash_file(path))
        for path in sorted(repo_dir.rglob("*"))
        if path.is_file()
    )


def _dnf_repo_dir(cmd: list[str]) -> Path | None:
    for arg in cmd:
        match = re.fullmatch(r"(.+):/ludos/dnf/repos(?::ro)?", arg)
        if match:
            return Path(match.group(1))
    return None


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


def _add_ccache_builder_options(command: list[str], ccache_dir: Path | None) -> None:
    if ccache_dir is None:
        return

    command.extend(["--volume", f"{ccache_dir}:{CCACHE_CONTAINER_DIR}"])
    command.extend(["--env", f"CCACHE_DIR={CCACHE_CONTAINER_DIR}"])
    command.extend(["--env", f"SCCACHE_DIR={SCCACHE_CONTAINER_DIR}"])
    if "CCACHE_MAXSIZE" in os.environ:
        command.extend(["--env", f"CCACHE_MAXSIZE={os.environ['CCACHE_MAXSIZE']}"])
    if "SCCACHE_CACHE_SIZE" in os.environ:
        command.extend(
            ["--env", f"SCCACHE_CACHE_SIZE={os.environ['SCCACHE_CACHE_SIZE']}"]
        )


def _ccache_build_prelude(ccache_dir: Path | None) -> str:
    if ccache_dir is None:
        return ""
    return (
        f"export PATH={CCACHE_PATH_PREFIX}:$PATH\n"
        f"mkdir -p {shlex.quote(SCCACHE_CONTAINER_DIR)}\n"
        "if command -v sccache >/dev/null 2>&1; then\n"
        "  export RUSTC_WRAPPER=sccache\n"
        "fi\n"
    )


def _run_card_build(
    *,
    podman: str,
    orchestrator: str,
    build_dir: Path,
    artifact_cache_dir: Path,
    ccache_dir: Path | None,
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
    _add_ccache_builder_options(command, ccache_dir)
    command.extend(["--env", "PS4=+ "])
    command.extend([orchestrator, "/bin/sh", "-ex", "-s"])
    returncode, _output = _run_streamed_command(
        command,
        input_text=_ccache_build_prelude(ccache_dir) + build_script + "\n",
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
    ccache_dir: Path | None,
    card_name: str,
    card_source: Path,
    card_env: dict[str, str],
    specs: tuple[SpecBuild, ...],
    prepare_script: str,
    arch: str,
    spec_source_cache_dir: Path,
    source_revisions: tuple[tuple[str, str], ...],
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
        spec_source_cache_dir=spec_source_cache_dir,
        cache_only=True,
        source_revisions=source_revisions,
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
            ccache_dir=ccache_dir,
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
    _add_ccache_builder_options(command, ccache_dir)
    command.extend(["--env", "PS4=+ "])
    command.extend([orchestrator, "/bin/sh", "-ex", "-s"])
    returncode, _output = _run_streamed_command(
        command,
        input_text=_ccache_build_prelude(ccache_dir) + build_script,
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
    ccache_dir: Path | None,
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
    _add_ccache_builder_options(command, ccache_dir)
    command.extend(["--env", "LUDOS_ENV=/workspace/.ludos-env"])
    command.extend(["--env", "PS4=+ "])
    command.extend([orchestrator, "/bin/sh", "-ex", "-s"])
    returncode, _output = _run_streamed_command(
        command,
        input_text=_ccache_build_prelude(ccache_dir) + prepare_script + "\n",
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
    spec_source_cache_dir: Path,
    cache_only: bool,
    source_revisions: tuple[tuple[str, str], ...] = tuple(),
) -> tuple[StagedSpec, ...]:
    _remove_tree(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    source_revision_map = dict(source_revisions)
    staged = []
    for spec in specs:
        packages = _spec_packages_for_arch(spec, arch)
        if not packages:
            log(f"Skipping spec without packages on {arch}: {spec.spec}")
            continue
        spec_source = _resolve_spec_source(
            card_source,
            spec.spec,
            spec_source_cache_dir,
            cache_only=cache_only,
            revision=source_revision_map.get(spec.spec, ""),
        )
        ignore_rules = _load_containerignore(spec_source.base_dir)
        relative_dir = (
            spec_source.stage_prefix
            / spec_source.spec_path.parent.relative_to(spec_source.base_dir)
        )
        staged_source_dir = workspace_dir / relative_dir
        if spec.files:
            staged_source_dir.mkdir(parents=True, exist_ok=True)
            _remove_staged_spec_files(
                spec_source.spec_path.parent,
                staged_source_dir,
                (spec_source.spec_path.name, *spec.files),
                card_source,
            )
            shutil.copy2(
                spec_source.spec_path,
                staged_source_dir / spec_source.spec_path.name,
            )
            _copy_spec_files(
                spec_source.spec_path.parent,
                staged_source_dir,
                spec.files,
                ignore_rules,
                card_source,
                spec_source.base_dir,
            )
        else:
            _copy_directory_contents(
                spec_source.spec_path.parent,
                staged_source_dir,
                ignore_rules,
                ignore_base_dir=spec_source.base_dir,
            )
        staged_spec_path = staged_source_dir / spec_source.spec_path.name
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


def _remove_staged_spec_files(
    source_dir: Path,
    destination_dir: Path,
    patterns: tuple[str, ...],
    card_source: Path,
) -> None:
    source_dir = source_dir.resolve()
    for pattern in patterns:
        pattern_path = Path(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise ConfigError(
                f"{card_source}: spec files entry '{pattern}' escapes the card"
            )
        matches = sorted(source_dir.glob(pattern))
        if not matches:
            continue
        for source_path in matches:
            try:
                relative_path = source_path.resolve().relative_to(source_dir).as_posix()
            except ValueError as exc:
                raise ConfigError(
                    f"{card_source}: spec files entry '{pattern}' escapes the card"
                ) from exc
            target_path = destination_dir / relative_path
            if target_path.is_dir() and not target_path.is_symlink():
                shutil.rmtree(target_path)
            elif target_path.exists() or target_path.is_symlink():
                target_path.unlink()


def _copy_spec_files(
    source_dir: Path,
    destination_dir: Path,
    patterns: tuple[str, ...],
    ignore_rules: tuple["_IgnoreRule", ...],
    card_source: Path,
    ignore_base_dir: Path,
) -> None:
    ignore_base_dir = ignore_base_dir.resolve()
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
            try:
                ignore_path = source_path.relative_to(ignore_base_dir).as_posix()
            except ValueError:
                ignore_path = relative_path
            if _ignored_by_containerignore(ignore_path, is_dir, ignore_rules):
                continue
            target_path = destination_dir / relative_path
            if is_dir:
                _copy_directory_contents(
                    source_path,
                    target_path,
                    ignore_rules,
                    ignore_base_dir=ignore_base_dir,
                )
            elif source_path.is_file():
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)


def _copy_directory_contents(
    source_dir: Path,
    destination_dir: Path,
    ignore_rules: tuple["_IgnoreRule", ...],
    ignore_base_dir: Path | None = None,
) -> None:
    source_dir = source_dir.resolve()
    ignore_base_dir = (
        source_dir if ignore_base_dir is None else ignore_base_dir.resolve()
    )
    shutil.rmtree(destination_dir, ignore_errors=True)
    for source_path in source_dir.rglob("*"):
        relative = source_path.relative_to(source_dir).as_posix()
        is_dir = source_path.is_dir()
        try:
            ignore_path = source_path.relative_to(ignore_base_dir).as_posix()
        except ValueError:
            ignore_path = relative
        if _ignored_by_containerignore(ignore_path, is_dir, ignore_rules):
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


def _resolve_staged_spec_builder_packages(
    orchestrator_dnf_base: list[str],
    releasever: str,
    workspace_dir: Path,
    staged_specs: tuple[StagedSpec, ...],
    arch: str,
    package_id_by_nevra: dict[str, tuple[str, str]],
    resolve_cache_dir: Path,
    repo_images: tuple[str, ...],
    *,
    card_name: str,
) -> tuple[str, ...]:
    packages = []
    for target, spec_paths in _spec_paths_by_build_target(staged_specs):
        log(f"Resolving spec BuildRequires for card: {card_name} ({target})")
        target_builder_packages = _resolve_spec_build_requires(
            orchestrator_dnf_base,
            releasever,
            workspace_dir,
            spec_paths,
            target,
            package_id_by_nevra,
            resolve_cache_dir,
            repo_images,
            include_dependencies=False,
        )
        packages.extend(target_builder_packages)
        if target != arch:
            target_builder_closure = _resolve_spec_build_requires(
                orchestrator_dnf_base,
                releasever,
                workspace_dir,
                spec_paths,
                target,
                package_id_by_nevra,
                resolve_cache_dir,
                repo_images,
                include_dependencies=True,
            )
            log(
                f"Resolving spec BuildRequires arch variants for card: "
                f"{card_name} ({target})"
            )
            packages.extend(
                _resolve_package_arch_variants(
                    orchestrator_dnf_base,
                    releasever,
                    target_builder_closure,
                    target,
                    package_id_by_nevra,
                    resolve_cache_dir,
                    repo_images,
                )
            )
    return _unique_packages(tuple(packages))


def _resolve_spec_build_requires(
    orchestrator_dnf_base: list[str],
    releasever: str,
    workspace_dir: Path,
    spec_paths: tuple[Path, ...],
    arch: str,
    package_id_by_nevra: dict[str, tuple[str, str]],
    resolve_cache_dir: Path,
    repo_images: tuple[str, ...],
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
    cmd = [
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
    ]
    transaction_preview = _run_cached_transaction_preview(
        cmd,
        resolve_cache_dir,
        repo_images,
        _resolve_spec_hash_inputs(workspace_dir, spec_paths),
    )
    output = transaction_preview.stdout + "\n" + transaction_preview.stderr
    entries = _parse_resolved_package_entries(output, include_dependencies)
    if (
        transaction_preview.returncode != 0
        and not entries
        and "cannot install the best candidate for the job" in output
    ):
        log("Retrying spec BuildRequires resolution with --no-best")
        no_best_cmd = list(cmd)
        no_best_cmd.insert(no_best_cmd.index("builddep"), "--no-best")
        transaction_preview = _run_cached_transaction_preview(
            no_best_cmd,
            resolve_cache_dir,
            repo_images,
            _resolve_spec_hash_inputs(workspace_dir, spec_paths),
        )
        output = transaction_preview.stdout + "\n" + transaction_preview.stderr
        entries = _parse_resolved_package_entries(output, include_dependencies)

    if transaction_preview.returncode not in (0, 1):
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not resolve spec BuildRequires:\n{detail}")
    if transaction_preview.returncode != 0 and not entries:
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not resolve spec BuildRequires:\n{detail}")
    package_id_by_nevra.update(entries)
    return tuple(package for package, _package_id in entries)


def _resolve_package_arch_variants(
    orchestrator_dnf_base: list[str],
    releasever: str,
    packages: tuple[str, ...],
    arch: str,
    package_id_by_nevra: dict[str, tuple[str, str]],
    resolve_cache_dir: Path,
    repo_images: tuple[str, ...],
) -> tuple[str, ...]:
    candidates = tuple(
        _package_with_arch(package, arch)
        for package in packages
        if _is_arch_variant_candidate(package_id_by_nevra, package, arch)
    )
    if not candidates:
        return tuple()

    cmd = [
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
        "%{name}\t%{name}-%{evr}.%{arch}\n",
        *candidates,
    ]
    query = _run_cached_transaction_preview(
        cmd,
        resolve_cache_dir,
        repo_images,
        _resolve_package_id_hash_inputs(packages, package_id_by_nevra),
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


def _resolve_spec_hash_inputs(
    workspace_dir: Path,
    spec_paths: tuple[Path, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            spec_path.relative_to(workspace_dir).as_posix(),
            _hash_file(spec_path),
        )
        for spec_path in spec_paths
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _resolve_package_id_hash_inputs(
    packages: tuple[str, ...],
    package_id_by_nevra: dict[str, tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        ("package-id", f"{package}:{name}:{arch}")
        for package in packages
        for name, arch in (package_id_by_nevra.get(package, ("", "")),)
        if name and arch
    )


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
        spec_source_cache_name = _identifier(staged.spec_path.stem)
        targets = " ".join(shlex.quote(target) for target in staged.targets)
        lines.extend(
            [
                f"find {shlex.quote(source_dir)} -maxdepth 1 -type f ! -name '*.spec' -exec cp -f -t \"$topdir/SOURCES\" {{}} +",
                f"cp -f {shlex.quote(spec_path)} \"$topdir/SPECS/{shlex.quote(spec_name)}\"",
                f"spec_source_cache=\"$source_cache/{spec_source_cache_name}\"",
                'mkdir -p "$spec_source_cache"',
                f"if spectool -l \"$topdir/SPECS/{shlex.quote(spec_name)}\" > \"$topdir/sources.list\"; then",
                "  if grep -Eq '^(Source|Patch)[0-9]*:[[:space:]]+https?://' \"$topdir/sources.list\"; then",
                "  missing_sources=0",
                "  while IFS= read -r source_entry; do",
                "    source_url=${source_entry#*:}",
                "    source_url=$(printf '%s\\n' \"$source_url\" | sed 's/^[[:space:]]*//')",
                "    case \"$source_url\" in http://*|https://*) ;; *) continue ;; esac",
                "    source_name=${source_url##*/}",
                "    source_name=${source_name%%\\?*}",
                "    if [ ! -f \"$spec_source_cache/$source_name\" ]; then",
                "      missing_sources=1",
                "      break",
                "    fi",
                "  done < \"$topdir/sources.list\"",
                "  if [ \"$missing_sources\" -eq 1 ]; then",
                f"    spectool -g -C \"$spec_source_cache\" \"$topdir/SPECS/{shlex.quote(spec_name)}\"",
                "  fi",
                '  find "$spec_source_cache" -maxdepth 1 -type f -exec cp -f -t "$topdir/SOURCES" {} +',
                "  fi",
                "fi",
                f"for target in {targets}; do",
                "  if [ \"$target\" = i686 ]; then",
                "    export PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig:/usr/share/pkgconfig",
                "    export PKG_CONFIG_PATH=",
                "    export BINDGEN_EXTRA_CLANG_ARGS=\"${BINDGEN_EXTRA_CLANG_ARGS:+$BINDGEN_EXTRA_CLANG_ARGS }-m32\"",
                "    cxx_target_include=$(find /usr/include/c++ -mindepth 2 -maxdepth 2 -type d -path '*/i686-redhat-linux' -print -quit 2>/dev/null || true)",
                "    if [ -n \"$cxx_target_include\" ]; then",
                "      export CPLUS_INCLUDE_PATH=\"$cxx_target_include${CPLUS_INCLUDE_PATH:+:$CPLUS_INCLUDE_PATH}\"",
                "    fi",
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
    spec_source_cache_dir: Path,
    *,
    hash_expression: str = "",
    cache_only: bool,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    if hash_expression:
        return (
            _cache_name(
                _expand_expression(
                    hash_expression,
                    card_env,
                    _card_base_dir(card_source),
                ),
                "spec card hash",
            ),
            tuple(),
        )

    digest = hashlib.sha256()
    source_revisions = []
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
        spec_source = _resolve_spec_source(
            card_source,
            spec.spec,
            spec_source_cache_dir,
            cache_only=cache_only,
            revision="",
        )
        if spec_source.revision:
            source_revisions.append((spec.spec, spec_source.revision))
        if spec.hash_revision and spec_source.revision:
            digest.update(spec_source.revision.encode("utf-8"))
            digest.update(b"\0")
        spec_dir = spec_source.spec_path.parent
        if spec.files:
            hash_paths = (
                spec_source.spec_path.relative_to(spec_source.base_dir).as_posix(),
                *_spec_file_hash_paths(
                    card_source,
                    spec_source.base_dir,
                    spec_dir,
                    spec.files,
                ),
            )
        else:
            hash_paths = (spec_dir.relative_to(spec_source.base_dir).as_posix(),)
        digest.update(_hash_paths(spec_source.base_dir, hash_paths).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:HASH_LENGTH], tuple(source_revisions)


def _spec_source_revisions_by_card(
    revisions: tuple[tuple[str, str, str], ...],
) -> dict[str, tuple[tuple[str, str], ...]]:
    by_card: dict[str, list[tuple[str, str]]] = {}
    for card_name, spec_source, revision in revisions:
        by_card.setdefault(card_name, []).append((spec_source, revision))
    return {card_name: tuple(values) for card_name, values in by_card.items()}


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
    return source.startswith(
        ("git+https://", "git+http://", "git+ssh://", "git+file://")
    )


def _validate_relative_file_path(value: str, source: Path, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ConfigError(
            f"{source}: {label} '{value}' must be a relative path without '..'"
        )
    return path


def _resolve_spec_source(
    card_source: Path,
    source: str,
    spec_source_cache_dir: Path,
    *,
    cache_only: bool,
    revision: str = "",
) -> SpecSource:
    if _is_git_source(source):
        return _resolve_git_spec_source(
            card_source,
            source,
            spec_source_cache_dir,
            cache_only=cache_only,
            revision=revision,
        )

    card_base_dir = _card_base_dir(card_source)
    spec_relpath = _validate_relative_file_path(source, card_source, "spec")
    spec_path = (card_base_dir / spec_relpath).resolve()
    try:
        spec_path.relative_to(card_base_dir)
    except ValueError as exc:
        raise ConfigError(f"{card_source}: spec '{source}' escapes the card") from exc
    if not spec_path.is_file():
        raise ConfigError(f"{card_source}: spec '{source}' is missing")
    return SpecSource(
        base_dir=card_base_dir,
        spec_path=spec_path,
        spec_relpath=spec_relpath,
    )


def _resolve_git_spec_source(
    card_source: Path,
    source: str,
    spec_source_cache_dir: Path,
    *,
    cache_only: bool,
    revision: str = "",
) -> SpecSource:
    git = shutil.which("git")
    if not git:
        raise ConfigError("git must be installed to use git spec sources")

    repo_url, ref, spec_relpath = _parse_git_spec_source(source)
    source_key = _git_source_cache_key(repo_url)
    repo_dir = spec_source_cache_dir / source_key / "repo"
    spec_source_cache_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    if revision:
        _pin_git_source_cache(git, repo_dir, repo_url, revision, source)
    elif cache_only:
        if not _is_git_repository(git, repo_dir):
            raise ConfigError(
                f"{card_source}: git spec source is not cached: {source}"
            )
        log(f"Using cached git spec source: {source}")
    else:
        _update_git_source_cache(git, repo_dir, repo_url, ref, source)

    spec_path = (repo_dir / spec_relpath).resolve()
    try:
        spec_path.relative_to(repo_dir.resolve())
    except ValueError as exc:
        raise ConfigError(f"{card_source}: spec '{source}' escapes the git source") from exc
    if not spec_path.is_file():
        raise ConfigError(f"{card_source}: spec '{source}' is missing")
    revision = _git_stdout(
        git,
        repo_dir,
        ["rev-parse", "HEAD"],
        "git spec source revision",
    )
    return SpecSource(
        base_dir=repo_dir.resolve(),
        spec_path=spec_path,
        spec_relpath=spec_relpath,
        revision=revision,
        stage_prefix=Path("spec-sources") / source_key,
    )


def _update_git_source_cache(
    git: str,
    repo_dir: Path,
    repo_url: str,
    ref: tuple[str, str],
    source: str,
) -> None:
    if not _is_git_repository(git, repo_dir):
        shutil.rmtree(repo_dir, ignore_errors=True)
        log(f"Initializing git spec source: {source}")
        _run_logged_command([git, "init", str(repo_dir)], "git spec source init")
        _run_logged_command(
            [git, "-C", str(repo_dir), "remote", "add", "origin", repo_url],
            "git spec source remote setup",
        )
    else:
        log(f"Updating git spec source: {source}")
        _run_logged_command(
            [git, "-C", str(repo_dir), "remote", "set-url", "origin", repo_url],
            "git spec source remote update",
        )

    _run_logged_command(
        [
            git,
            "-C",
            str(repo_dir),
            "fetch",
            "--depth=1",
            "--prune",
            "origin",
            _git_fetch_ref(ref),
        ],
        "git spec source fetch",
    )
    _run_logged_command(
        [git, "-C", str(repo_dir), "checkout", "--force", "FETCH_HEAD"],
        "git spec source checkout",
    )


def _pin_git_source_cache(
    git: str,
    repo_dir: Path,
    repo_url: str,
    revision: str,
    source: str,
) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise ConfigError(f"git spec source '{source}' has invalid pinned revision")
    if _is_git_repository(git, repo_dir):
        if _git_head_revision(git, repo_dir) == revision:
            return
        log(f"Updating git spec source revision: {source}")
        _run_logged_command(
            [git, "-C", str(repo_dir), "remote", "set-url", "origin", repo_url],
            "git spec source remote update",
        )
    else:
        shutil.rmtree(repo_dir, ignore_errors=True)
        log(f"Cloning git spec source at pinned revision: {source}")
        _run_logged_command([git, "init", str(repo_dir)], "git spec source init")
        _run_logged_command(
            [git, "-C", str(repo_dir), "remote", "add", "origin", repo_url],
            "git spec source remote setup",
        )

    if not _git_has_revision(git, repo_dir, revision):
        _run_logged_command(
            [
                git,
                "-C",
                str(repo_dir),
                "fetch",
                "--depth=1",
                "origin",
                revision,
            ],
            "git spec source pinned fetch",
        )
    _run_logged_command(
        [git, "-C", str(repo_dir), "checkout", "--force", revision],
        "git spec source pinned checkout",
    )


def _git_head_revision(git: str, repo_dir: Path) -> str:
    result = subprocess.run(
        [git, "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_has_revision(git: str, repo_dir: Path, revision: str) -> bool:
    result = subprocess.run(
        [git, "-C", str(repo_dir), "cat-file", "-e", f"{revision}^{{commit}}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _parse_git_spec_source(source: str) -> tuple[str, tuple[str, str], Path]:
    raw_source = source.removeprefix("git+")
    source_without_fragment, separator, fragment = raw_source.partition("#")
    if not separator:
        fragment = ""
    parsed = urllib.parse.urlsplit(source_without_fragment)
    if parsed.scheme not in ("https", "http", "ssh", "file"):
        raise ConfigError(f"unsupported git spec source protocol in '{source}'")
    if parsed.query:
        raise ConfigError(f"git spec source '{source}' must not include a query")
    if ":" not in parsed.path:
        raise ConfigError(
            f"git spec source '{source}' must be 'git+<repo-url>:<spec-path>'"
        )

    repo_path, spec_path_value = parsed.path.rsplit(":", 1)
    if not repo_path or not spec_path_value:
        raise ConfigError(
            f"git spec source '{source}' must be 'git+<repo-url>:<spec-path>'"
        )
    spec_path = _validate_git_spec_path(
        urllib.parse.unquote(spec_path_value),
        source,
    )
    repo_url = urllib.parse.urlunsplit(
        parsed._replace(path=repo_path, query="", fragment="")
    )
    return (
        repo_url,
        _parse_git_ref_fragment(fragment, source, "git spec source"),
        spec_path,
    )


def _parse_git_ref_fragment(
    fragment: str,
    source: str,
    label: str,
) -> tuple[str, str]:
    if not fragment:
        return ("default", "")
    if ":" in fragment or "=" not in fragment:
        raise ConfigError(f"{label} '{source}' has invalid ref selector")
    ref_kind, ref_value = fragment.split("=", 1)
    if ref_kind not in ("commit", "tag", "branch", "ref") or not ref_value:
        raise ConfigError(f"{label} '{source}' has invalid ref selector")
    return (ref_kind, ref_value)


def _validate_git_spec_path(value: str, source: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ConfigError(
            f"git spec source '{source}' spec path must be relative without '..'"
        )
    return path


def _git_source_cache_key(repo_url: str) -> str:
    parsed = urllib.parse.urlsplit(repo_url)
    name = Path(parsed.path.rstrip("/")).name or "repo"
    if name.endswith(".git"):
        name = name[:-4]
    repo_hash = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:12]
    return f"{_identifier(name)}-{repo_hash}"


def _git_stdout(
    git: str,
    repo_dir: Path,
    args: list[str],
    description: str,
) -> str:
    result = subprocess.run(
        [git, "-C", str(repo_dir), *args],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    raise ConfigError(f"{description} failed with exit status {result.returncode}")


def _copy_http_file_source(
    source: str,
    target: Path,
    cache_path: Path,
    *,
    cache_only: bool,
) -> None:
    if cache_only:
        if not cache_path.is_file():
            raise ConfigError(f"file source is not cached: {source}")
        shutil.copy2(cache_path, target)
        return

    _download_file_source(source, cache_path)
    shutil.copy2(cache_path, target)


def _download_file_source(source: str, target: Path) -> None:
    log(f"Downloading file source: {source}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(source) as response:
            with target.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except OSError as exc:
        raise ConfigError(f"failed to download file source '{source}': {exc}") from exc


def _copy_git_file_source(
    source: str,
    target: Path,
    cache_dir: Path,
    *,
    cache_only: bool,
) -> None:
    git = shutil.which("git")
    if not git:
        raise ConfigError("git must be installed to use git files sources")

    repo_url, ref, repo_path = _parse_git_file_source(source)
    source_dir = cache_dir
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    if cache_only:
        if not _is_git_repository(git, source_dir) or not (
            source_dir / ".git" / "FETCH_HEAD"
        ).is_file():
            raise ConfigError(f"git file source is not cached: {source}")
        log(f"Using cached git file source: {source}")
    elif not _is_git_repository(git, source_dir):
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
    if not cache_only:
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
    if parsed.scheme not in ("https", "http", "ssh", "file"):
        raise ConfigError(f"unsupported git files source protocol in '{source}'")
    if parsed.fragment:
        ref_expr = parsed.fragment
        if ":" in ref_expr:
            raise ConfigError(f"git files source '{source}' must not include a subpath")
        ref = _parse_git_ref_fragment(ref_expr, source, "git files source")
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
    return {k: values[k] for k in ENV_ALWAYS_AVAILABLE + tuple(card_env) if k in values}

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
        if process.stdout is not None:
            process.stdout.close()
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


def _cleanup_dnf_workspaces(metadata: tuple[ResolvedBuildMetadata, ...]) -> None:
    workspaces = {
        Path(manifest.dnf_workspace_dir): manifest.podman
        for manifest in metadata
    }
    for workspace, podman in workspaces.items():
        _remove_tree(workspace, podman=podman)


def _cleanup_dnf_workspace_paths(paths: tuple[Path, ...]) -> None:
    for path in dict.fromkeys(paths):
        _remove_tree(path)


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

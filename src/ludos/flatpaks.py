from __future__ import annotations

import base64
import gzip
import hashlib
import json
import shlex
import shutil
import struct
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .build import (
    HASH_LENGTH,
    _card_specs_hash,
    _build_specs_output_image,
    _create_builder_image,
    _download_block_packages,
    _ensure_image,
    _identifier,
    _local_image,
    _output_metadata_in_image,
    _remove_tree,
    _require_buildah,
    _resolve_packages,
    _resolve_staged_spec_builder_packages,
    _stage_card_specs,
    _substitute_variables,
    _tag_image,
    _unique_packages,
)
from .common import (
    ResolvedManifestContext,
    resolve_manifest_context,
    _run_streamed_command,
)
from .logging import log
from .model import (
    ConfigError,
    FlatpakImagesConfig,
    ManifestRuntime,
    SpecBuild,
    _env_dict,
    _load_mapping,
    _optional_string,
    _optional_bool,
    _required_string,
    _required_string_tuple,
    _required_version,
    _spec_builds_tuple,
    _string_tuple,
)


FLATPAK_ARCHES = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
}
DEFAULT_FLATPAK_SDK = "org.anatase.ludos.Sdk"


@dataclass(frozen=True)
class FlatpakBuildResult:
    app_id: str
    branch: str
    ref: str
    image: str
    latest_image: str
    build_image: str
    builder_image: str
    podman: str
    orchestrator: str


@dataclass(frozen=True)
class FlatpakImageResolution:
    output_images: tuple[str, ...]
    latest_images: tuple[str, ...]
    build_images: tuple[str, ...]
    builder_images: tuple[str, ...]


@dataclass(frozen=True)
class FlatpakConfig:
    app_id: str
    command: str = ""
    runtime: bool = False
    version: str = "stable"
    finish_args: str = ""
    rename: str = ""
    rename_author: str = ""
    rename_icon: str = ""
    rename_desktop_file: str = ""
    rename_appdata_file: str = ""
    add_extensions: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = tuple()


@dataclass(frozen=True)
class FlatpakCard:
    version: int
    flatpak: FlatpakConfig
    env: dict[str, str | int]
    build_deps: tuple[str, ...]
    specs: tuple[SpecBuild, ...]
    files: tuple[str, ...] = tuple()
    prepare: str = ""
    postprocess: str = ""
    source: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> "FlatpakCard":
        data = _load_mapping(path)
        allowed = {
            "version",
            "flatpak",
            "env",
            "build-deps",
            "specs",
            "files",
            "prepare",
            "postprocess",
        }
        _reject_unknown_keys(path, data, allowed)
        version = _required_version(data, path)
        flatpak = _flatpak_config(data, path)
        env = _env_dict(data, path, include_default=False)
        build_deps = _required_string_tuple(data, "build-deps", path)
        specs = _required_spec_builds_tuple(data, "specs", path)
        files = _string_tuple(data, "files", path)
        prepare = _optional_string(data, "prepare", path)
        postprocess = _optional_string(data, "postprocess", path)
        return cls(
            version=version,
            flatpak=flatpak,
            env=env,
            build_deps=build_deps,
            specs=specs,
            files=files,
            prepare=prepare,
            postprocess=postprocess,
            source=path,
        )


@dataclass(frozen=True)
class FlatpakBuildPlan:
    card_path: Path
    card: FlatpakCard
    flatpak_dir: Path
    app_name: str
    block: str
    branch: str
    flatpak_arch: str
    app_ref: str
    output_image: str
    latest_image: str
    substitution_env: dict[str, str]
    build_env: dict[str, str]
    specs: tuple[SpecBuild, ...]
    prepare_script: str
    spec_revisions: tuple[tuple[str, str], ...]
    spec_build_dir: Path
    artifact_cache_dir: Path
    final_build_dir: Path
    rpmbuild_defines: tuple[str, ...]
    builder_packages: tuple[str, ...]
    builder_image: str
    build_image: str
    metadata: str
    rpm_files: tuple[str, ...] = tuple()


def build_flatpak(
    manifest_path: Path,
    flatpak_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ccache: bool = True,
    force: bool = False,
) -> FlatpakBuildResult:
    context: ResolvedManifestContext | None = None
    try:
        context = resolve_manifest_context(
            manifest_path,
            cards_dir=cards_dir,
            cache_dir=cache_dir,
            cache_version=cache_version,
            cache_only=cache_only,
            ccache=ccache,
        )
        return _build_flatpak_with_context(
            context,
            flatpak_path,
            cache_only=cache_only,
            force=force,
        )
    finally:
        if context is not None:
            _remove_tree(context.dnf_workspace_dir, podman=context.podman)


def build_flatpaks(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ccache: bool = True,
    force: bool = False,
) -> tuple[FlatpakBuildResult, ...]:
    context: ResolvedManifestContext | None = None
    try:
        context = resolve_manifest_context(
            manifest_path,
            cards_dir=cards_dir,
            cache_dir=cache_dir,
            cache_version=cache_version,
            cache_only=cache_only,
            ccache=ccache,
        )
        return build_flatpaks_with_context(
            context,
            manifest_path=manifest_path,
            cache_only=cache_only,
            force=force,
        )
    finally:
        if context is not None:
            _remove_tree(context.dnf_workspace_dir, podman=context.podman)


def build_flatpaks_with_context(
    context: ResolvedManifestContext,
    *,
    manifest_path: Path,
    cache_only: bool = False,
    force: bool = False,
) -> tuple[FlatpakBuildResult, ...]:
    if context.validation.missing_flatpaks:
        missing = ", ".join(context.validation.missing_flatpaks)
        raise ConfigError(
            f"{manifest_path}: missing flatpak definitions: {missing}"
        )
    flatpak_refs = context.validation.manifest.flatpaks
    if not flatpak_refs:
        raise ConfigError(
            f"{manifest_path}: 'flatpaks' must contain at least one item"
        )
    plans = _manifest_flatpak_build_plans(context, cache_only=cache_only)
    ci_registry = getattr(context, "ci_registry", "")
    if not force:
        missing_plans = tuple(
            plan
            for plan in plans
            if not hasattr(plan, "output_image")
            or not _ensure_image(
                context.podman,
                plan.output_image,
                ci_registry,
            )
        )
    else:
        missing_plans = plans
    _ensure_flatpak_builders(context, missing_plans, cache_only=cache_only)
    built_plans = _ensure_flatpak_rpm_builds(
        context, missing_plans, cache_only=cache_only
    )
    if any(not hasattr(plan, "app_name") for plan in plans):
        return _ensure_flatpak_images(
            context,
            built_plans,
            cache_only=cache_only,
            force=force,
        )
    built_by_name = {plan.app_name: plan for plan in built_plans}
    final_plans = tuple(built_by_name.get(plan.app_name, plan) for plan in plans)
    return _ensure_flatpak_images(
        context,
        final_plans,
        cache_only=cache_only,
        force=force,
    )


def resolve_manifest_flatpak_images(
    manifest_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = True,
) -> FlatpakImageResolution:
    context: ResolvedManifestContext | None = None
    dnf_workspace_dirs: list[Path] = []
    try:
        context = resolve_manifest_context(
            manifest_path,
            cards_dir=cards_dir,
            cache_dir=cache_dir,
            cache_version=cache_version,
            cache_only=cache_only,
            dnf_workspace_dirs=dnf_workspace_dirs,
        )
        if context.validation.missing_flatpaks:
            missing = ", ".join(context.validation.missing_flatpaks)
            raise ConfigError(
                f"{manifest_path}: missing flatpak definitions: {missing}"
            )
        plans = _manifest_flatpak_build_plans(context, cache_only=cache_only)
        return FlatpakImageResolution(
            output_images=tuple(plan.output_image for plan in plans),
            latest_images=tuple(plan.latest_image for plan in plans),
            build_images=tuple(plan.build_image for plan in plans),
            builder_images=tuple(plan.builder_image for plan in plans),
        )
    finally:
        if context is not None:
            _remove_tree(context.dnf_workspace_dir, podman=context.podman)
        else:
            for dnf_workspace_dir in dnf_workspace_dirs:
                _remove_tree(dnf_workspace_dir)


def plan_manifest_flatpaks_with_context(
    context: ResolvedManifestContext,
    *,
    manifest_path: Path,
    cache_only: bool = False,
) -> tuple[FlatpakBuildPlan, ...]:
    if context.validation.missing_flatpaks:
        missing = ", ".join(context.validation.missing_flatpaks)
        raise ConfigError(
            f"{manifest_path}: missing flatpak definitions: {missing}"
        )
    return _manifest_flatpak_build_plans(context, cache_only=cache_only)


def _manifest_flatpak_build_plans(
    context: ResolvedManifestContext,
    *,
    cache_only: bool,
) -> tuple[FlatpakBuildPlan, ...]:
    flatpak_refs = context.validation.manifest.flatpaks
    if not flatpak_refs:
        return tuple()
    return tuple(
        _prepare_flatpak_build_plan(
            context,
            _manifest_flatpak_path(flatpak_ref, context.root_dir),
            cache_only=cache_only,
        )
        for flatpak_ref in flatpak_refs
    )


def _build_flatpak_with_context(
    context: ResolvedManifestContext,
    flatpak_path: Path,
    *,
    cache_only: bool,
    force: bool,
) -> FlatpakBuildResult:
    plan = _prepare_flatpak_build_plan(context, flatpak_path, cache_only=cache_only)
    ci_registry = getattr(context, "ci_registry", "")
    if force or not _ensure_image(
        context.podman,
        plan.output_image,
        ci_registry,
    ):
        _ensure_flatpak_builders(context, (plan,), cache_only=cache_only)
        plan = _ensure_flatpak_rpm_builds(context, (plan,), cache_only=cache_only)[0]
    return _ensure_flatpak_images(
        context,
        (plan,),
        cache_only=cache_only,
        force=force,
    )[0]


def _prepare_flatpak_build_plan(
    context: ResolvedManifestContext,
    flatpak_path: Path,
    *,
    cache_only: bool,
) -> FlatpakBuildPlan:
    card_path = _flatpak_card_path(flatpak_path)
    card = FlatpakCard.from_file(card_path)
    flatpak_dir = card_path.parent
    app_name = _flatpak_name(flatpak_dir)
    block = f"flatpak-{app_name}"
    manifest_runtime = _require_manifest_runtime(context)
    branch = card.flatpak.version if card.flatpak.runtime else manifest_runtime.branch
    flatpak_arch = _flatpak_arch(context.arch)
    ref_kind = "runtime" if card.flatpak.runtime else "app"
    app_ref = f"{ref_kind}/{card.flatpak.app_id}/{flatpak_arch}/{branch}"
    log(f"Building flatpak {card.flatpak.app_id} for {context.distro}")
    substitution_env = _flatpak_build_env(context.manifest_env, card.env)
    build_env = dict(substitution_env)
    specs = _substitute_specs(card.specs, substitution_env)
    flatpak_cache_dir = (
        context.distro_cache_dir / "flatpaks" / _identifier(app_name)
    )
    spec_build_dir = flatpak_cache_dir / "spec-build"
    spec_scan_dir = flatpak_cache_dir / "spec-scan"
    artifact_cache_dir = (
        context.build_artifact_cache_dir / "flatpaks" / _identifier(app_name)
    )
    final_build_dir = (
        context.distro_cache_dir / "build" / "flatpaks" / _identifier(app_name)
    )
    flatpak_cache_dir.mkdir(parents=True, exist_ok=True)

    spec_hash, spec_revisions = _card_specs_hash(
        card_path,
        specs,
        substitution_env,
        card.prepare.rstrip(),
        context.spec_source_cache_dir,
        hash_expression="",
        cache_only=cache_only,
    )
    package_id_by_nevra: dict[str, tuple[str, str]] = {}
    orchestrator_dnf_base = _orchestrator_dnf_base(context)
    staged_specs = _stage_card_specs(
        card_source=card_path,
        specs=specs,
        card_env=substitution_env,
        workspace_dir=spec_scan_dir,
        arch=context.arch,
        spec_source_cache_dir=context.spec_source_cache_dir,
        cache_only=True,
        source_revisions=spec_revisions,
    )
    rpmbuild_defines = _flatpak_rpmbuild_defines()
    spec_builder_packages = _resolve_staged_spec_builder_packages(
        orchestrator_dnf_base,
        context.releasever,
        spec_scan_dir,
        staged_specs,
        context.arch,
        package_id_by_nevra,
        context.dnf_resolve_dir,
        context.repo_images,
        card_name=block,
        rpmbuild_defines=rpmbuild_defines,
    )
    builder_requests = _unique_packages((*card.build_deps, *spec_builder_packages))
    builder_packages = _resolve_packages(
        orchestrator_dnf_base,
        context.releasever,
        builder_requests,
        package_id_by_nevra,
        context.dnf_resolve_dir,
        context.repo_images,
    )
    builder_hash = _hash_lines(builder_packages)
    builder_image = _local_image(
        context.local_prefix,
        "builders",
        f"{context.distro}-flatpak-{app_name}-{builder_hash}",
    )
    build_image = _local_image(
        context.local_prefix,
        "builds",
        f"{context.distro}-flatpak-{app_name}-{spec_hash}",
    )
    metadata = _flatpak_metadata(
        card.flatpak,
        branch=branch,
        flatpak_arch=flatpak_arch,
        runtime_id=manifest_runtime.id,
    )
    final_hash = _flatpak_final_hash(
        app_name=app_name,
        app_ref=app_ref,
        branch=branch,
        flatpak_arch=flatpak_arch,
        card=card,
        flatpak_dir=flatpak_dir,
        substitution_env=substitution_env,
        build_image=build_image,
        metadata=metadata,
        flatpak_images=getattr(context, "flatpak_images", FlatpakImagesConfig()),
    )
    output_image = _local_image(
        context.local_prefix,
        "flatpaks",
        f"{context.distro}-{app_name}-{final_hash}",
    )
    latest_image = _local_image(context.local_prefix, "flatpaks", app_name)

    return FlatpakBuildPlan(
        card_path=card_path,
        card=card,
        flatpak_dir=flatpak_dir,
        app_name=app_name,
        block=block,
        branch=branch,
        flatpak_arch=flatpak_arch,
        app_ref=app_ref,
        output_image=output_image,
        latest_image=latest_image,
        substitution_env=substitution_env,
        build_env=build_env,
        specs=specs,
        prepare_script=card.prepare.rstrip(),
        spec_revisions=spec_revisions,
        spec_build_dir=spec_build_dir,
        artifact_cache_dir=artifact_cache_dir,
        final_build_dir=final_build_dir,
        rpmbuild_defines=rpmbuild_defines,
        builder_packages=builder_packages,
        builder_image=builder_image,
        build_image=build_image,
        metadata=metadata,
    )


def _ensure_flatpak_builders(
    context: ResolvedManifestContext,
    plans: tuple[FlatpakBuildPlan, ...],
    *,
    cache_only: bool,
) -> None:
    orchestrator_dnf_base = _orchestrator_dnf_base(context)
    ci_registry = getattr(context, "ci_registry", "")
    for plan in plans:
        if _ensure_image(context.podman, plan.builder_image, ci_registry):
            log(f"Reusing flatpak builder image: {plan.builder_image}")
            continue
        if cache_only:
            raise ConfigError(
                f"flatpak builder image is not cached: {plan.builder_image}"
            )

        builder_rpm_files = _download_block_packages(
            orchestrator_dnf_base,
            plan.builder_packages,
            package_dir=context.package_dir,
            resolve_dependencies=True,
        )
        log(f"Creating flatpak builder image: {plan.builder_image}")
        _create_builder_image(
            podman=context.podman,
            buildah=_require_buildah(context.buildah),
            orchestrator=context.orchestrator,
            root_dir=context.root_dir,
            repo_dir=context.repo_dir,
            dnf_cache_dir=context.dnf_cache_dir,
            dnf_persist_dir=context.dnf_persist_dir,
            dnf_log_dir=context.dnf_log_dir,
            image=plan.builder_image,
            package_dir=context.package_dir,
            rpm_files=builder_rpm_files,
            releasever=context.releasever,
        )


def _ensure_flatpak_rpm_builds(
    context: ResolvedManifestContext,
    plans: tuple[FlatpakBuildPlan, ...],
    *,
    cache_only: bool,
) -> tuple[FlatpakBuildPlan, ...]:
    results = []
    ci_registry = getattr(context, "ci_registry", "")
    for plan in plans:
        if _ensure_image(context.podman, plan.build_image, ci_registry):
            log(f"Reusing flatpak build output image: {plan.build_image}")
            rpm_files, _has_files = _output_metadata_in_image(
                context.podman,
                plan.build_image,
            )
        elif cache_only:
            raise ConfigError(
                f"flatpak build output image is not cached: {plan.build_image}"
            )
        else:
            log(
                f"Running flatpak RPM build: {plan.block} "
                f"(:{plan.build_image.rsplit(':', 1)[-1]})"
            )
            build_output = _build_specs_output_image(
                podman=context.podman,
                orchestrator=plan.builder_image,
                image=plan.build_image,
                build_dir=plan.spec_build_dir,
                artifact_cache_dir=plan.artifact_cache_dir,
                ccache_dir=context.ccache_dir,
                card_name=plan.block,
                card_source=plan.card_path,
                card_env=plan.substitution_env,
                specs=plan.specs,
                prepare_script=plan.prepare_script,
                arch=context.arch,
                spec_source_cache_dir=context.spec_source_cache_dir,
                source_revisions=plan.spec_revisions,
                rpmbuild_defines=plan.rpmbuild_defines,
                build_env=plan.build_env,
            )
            if not build_output.rpm_files:
                raise ConfigError(
                    f"{plan.card_path}: flatpak specs produced no RPMs"
                )
            rpm_files, _has_files = _output_metadata_in_image(
                context.podman,
                plan.build_image,
            )

        if not rpm_files:
            raise ConfigError(f"{plan.card_path}: flatpak build output has no RPMs")
        results.append(replace(plan, rpm_files=rpm_files))
    return tuple(results)


def _ensure_flatpak_images(
    context: ResolvedManifestContext,
    plans: tuple[FlatpakBuildPlan, ...],
    *,
    cache_only: bool,
    force: bool = False,
) -> tuple[FlatpakBuildResult, ...]:
    results = []
    ci_registry = getattr(context, "ci_registry", "")
    for plan in plans:
        if not force and _ensure_image(
            context.podman,
            plan.output_image,
            ci_registry,
        ):
            log(f"Reusing flatpak image: {plan.output_image}")
            _tag_image(context.podman, plan.output_image, plan.latest_image)
            results.append(_flatpak_build_result(context, plan))
            continue
        _write_flatpak_containerfile(
            final_build_dir=plan.final_build_dir,
            flatpak_dir=plan.flatpak_dir,
            card=plan.card,
            build_image=plan.build_image,
            orchestrator=context.orchestrator,
            metadata=plan.metadata,
            app_ref=plan.app_ref,
            branch=plan.branch,
            flatpak_arch=plan.flatpak_arch,
        )
        _run_flatpak_image_build(
            context.podman,
            _require_buildah(context.buildah),
            plan.final_build_dir,
            plan.output_image,
            plan.metadata,
            plan.card.flatpak.app_id,
            flatpak_images=getattr(
                context,
                "flatpak_images",
                FlatpakImagesConfig(),
            ),
        )
        _tag_image(context.podman, plan.output_image, plan.latest_image)
        results.append(_flatpak_build_result(context, plan))
    return tuple(results)


def _flatpak_build_result(
    context: ResolvedManifestContext,
    plan: FlatpakBuildPlan,
) -> FlatpakBuildResult:
    return FlatpakBuildResult(
        app_id=plan.card.flatpak.app_id,
        branch=plan.branch,
        ref=plan.app_ref,
        image=plan.output_image,
        latest_image=plan.latest_image,
        build_image=plan.build_image,
        builder_image=plan.builder_image,
        podman=context.podman,
        orchestrator=context.orchestrator,
    )


def _reject_unknown_keys(path: Path, data: dict[str, Any], allowed: set[str], prefix: str = "") -> None:
    for key in data:
        if key not in allowed:
            qualified = f"{prefix}.{key}" if prefix else key
            raise ConfigError(f"{path}: '{qualified}' is not supported")


def _flatpak_config(data: dict[str, Any], path: Path) -> FlatpakConfig:
    value = data.get("flatpak")
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: 'flatpak' must be a mapping")
    allowed = {
        "id",
        "command",
        "runtime",
        "version",
        "finish-args",
        "rename",
        "rename-author",
        "rename-icon",
        "rename-desktop-file",
        "rename-appdata-file",
        "add-extensions",
    }
    _reject_unknown_keys(path, value, allowed, "flatpak")
    app_id = _required_string(value, "id", path).strip()
    runtime = _optional_bool(value, "runtime", path, "flatpak")
    if runtime:
        command = _optional_string(value, "command", path).strip()
    else:
        command = _required_string(value, "command", path).strip()
    version = _optional_string(value, "version", path).strip() or "stable"
    finish_args = _optional_string(value, "finish-args", path)
    return FlatpakConfig(
        app_id=app_id,
        command=command,
        runtime=runtime,
        version=version,
        finish_args=finish_args,
        rename=_optional_string(value, "rename", path),
        rename_author=_optional_string(value, "rename-author", path),
        rename_icon=_optional_string(value, "rename-icon", path),
        rename_desktop_file=_optional_string(value, "rename-desktop-file", path),
        rename_appdata_file=_optional_string(value, "rename-appdata-file", path),
        add_extensions=_add_extensions(value, path),
    )


def _required_spec_builds_tuple(
    data: dict[str, Any], key: str, path: Path
) -> tuple[SpecBuild, ...]:
    specs = _spec_builds_tuple(data, key, path)
    if not specs:
        raise ConfigError(f"{path}: '{key}' must contain at least one item")
    return specs


def _add_extensions(data: dict[str, Any], path: Path) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    value = data.get("add-extensions")
    if value is None:
        return tuple()
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: 'flatpak.add-extensions' must be a mapping")
    extensions = []
    for name, config in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{path}: flatpak.add-extensions keys must be strings")
        if not isinstance(config, dict):
            raise ConfigError(f"{path}: 'flatpak.add-extensions.{name}' must be a mapping")
        items = []
        for key, raw in config.items():
            if not isinstance(key, str):
                raise ConfigError(f"{path}: flatpak.add-extensions.{name} keys must be strings")
            if not isinstance(raw, (str, int, bool)):
                raise ConfigError(
                    f"{path}: 'flatpak.add-extensions.{name}.{key}' must be a string, integer, or boolean"
                )
            if isinstance(raw, bool):
                value_text = "true" if raw else "false"
            else:
                value_text = str(raw)
            items.append((key, value_text))
        extensions.append((name, tuple(sorted(items))))
    return tuple(sorted(extensions))


def _flatpak_card_path(flatpak_path: Path) -> Path:
    path = flatpak_path.expanduser().resolve()
    if path.is_dir():
        yaml_path = path / "card.yaml"
        yml_path = path / "card.yml"
        if yaml_path.exists():
            return yaml_path
        if yml_path.exists():
            return yml_path
        raise ConfigError(f"{path}: missing card.yaml")
    return path


def _manifest_flatpak_path(flatpak_ref: str, root_dir: Path) -> Path:
    path = Path(flatpak_ref)
    if path.is_absolute():
        return path
    return root_dir / path


def _flatpak_name(flatpak_dir: Path) -> str:
    return flatpak_dir.resolve().name


def _substitute_specs(specs: tuple[SpecBuild, ...], env: dict[str, str]) -> tuple[SpecBuild, ...]:
    return tuple(
        replace(spec, spec=_substitute_variables(spec.spec, env))
        for spec in specs
    )


def _flatpak_build_env(
    manifest_env: dict[str, str],
    flatpak_env: dict[str, str | int],
) -> dict[str, str]:
    env = {
        key: manifest_env[key]
        for key in ("arch", "releasever")
        if key in manifest_env
    }
    variables = dict(manifest_env)
    variables.update({key: str(value) for key, value in env.items()})
    for key, value in flatpak_env.items():
        env[key] = _substitute_variables(str(value), variables)
        variables[key] = env[key]
    return env


def _orchestrator_dnf_base(context: ResolvedManifestContext) -> list[str]:
    return [
        context.podman,
        "run",
        "--rm",
        "--volume",
        f"{context.root_dir / 'repos'}:/workspace/repos:ro",
        "--volume",
        f"{context.repo_dir}:/ludos/dnf/repos:ro",
        "--volume",
        f"{context.dnf_cache_dir}:/ludos/dnf/cache",
        "--volume",
        f"{context.dnf_persist_dir}:/ludos/dnf/persist",
        "--volume",
        f"{context.dnf_log_dir}:/ludos/dnf/log",
        "--volume",
        f"{context.package_dir}:/ludos/packages",
        "--workdir",
        "/workspace/repos",
        context.orchestrator,
        "dnf5",
    ]


def _flatpak_arch(arch: str) -> str:
    try:
        return FLATPAK_ARCHES[arch]
    except KeyError as exc:
        raise ConfigError(f"flatpak builds do not support architecture: {arch}") from exc


def _require_manifest_runtime(context: ResolvedManifestContext) -> ManifestRuntime:
    runtime = context.validation.manifest.runtime
    if runtime is None:
        source = context.validation.manifest.source or context.root_dir
        raise ConfigError(f"{source}: 'runtime' is required to build flatpaks")
    return runtime


def _flatpak_rpmbuild_defines() -> tuple[str, ...]:
    # Ported from fedora flatpak macros
    return (
        "flatpak 1",
        "distcore .fc%{fedora}app",
        "dist %{!?distprefix0:%{?distprefix}}%{expand:%{lua:for i=0,9999 do print(\"%{?distprefix\" .. i ..\"}\") end}}%{distcore}%{?with_bootstrap:%{__bootstrap}}",
        "_prefix /app",
        "_sysconfdir %{_prefix}/etc",
        "_localstatedir %{_prefix}/var",
        "build_ldflags -Wl,-z,relro %{_ld_as_needed_flags} %{_ld_symbols_flags} %{_hardened_ldflags} %{_annotation_ldflags} %[ \"%{toolchain}\" == \"clang\" ? \"%{?_clang_extra_ldflags}\" : \"\" ] %{_build_id_flags} %{?_package_note_flags} -L%{_prefix}/lib64",
        "__brp_compress %{_usr}/lib/rpm/brp-compress /app",
        "__git %{_bindir}/git",
        "__perl %{_usr}/bin/perl",
        "_fontbasedir %{_datadir}/fonts",
        "java_home %{_prefix}/lib/jvm/jre-openjdk",
        "__font_provides %{_rpmconfigdir}/fontconfig-flatpak.prov",
        "__maven_path ^/usr/share/maven-metadata/.*",
        "jpb_env JAVACONFDIRS=%{_sysconfdir}/java",
        "__brp_check_rpaths %{nil}",
        "debug_package %{nil}",
        "java_remove_imports /usr/bin/jurand -i",
        "java_remove_annotations /usr/bin/jurand -i -a",
    )


def _flatpak_metadata(
    config: FlatpakConfig,
    *,
    branch: str,
    flatpak_arch: str,
    runtime_id: str,
    sdk_id: str = DEFAULT_FLATPAK_SDK,
) -> str:
    editor = _MetadataEditor()
    if config.runtime:
        editor.set("Runtime", "name", config.app_id)
        return editor.render()
    editor.set("Application", "name", config.app_id)
    editor.set("Application", "runtime", f"{runtime_id}/{flatpak_arch}/{branch}")
    editor.set("Application", "sdk", f"{sdk_id}/{flatpak_arch}/{branch}")
    finish_args = ["--command", config.command]
    if config.finish_args.strip():
        finish_args.extend(shlex.split(config.finish_args, comments=True))
    _apply_finish_args(editor, finish_args)
    for name, values in config.add_extensions:
        group = f"Extension {name}"
        for key, value in values:
            editor.set(group, key, value)
    return editor.render()


class _MetadataEditor:
    def __init__(self) -> None:
        self._groups: dict[str, dict[str, str]] = {}
        self._lists: dict[tuple[str, str], list[str]] = {}

    def set(self, group: str, key: str, value: str) -> None:
        self._groups.setdefault(group, {})[key] = value

    def add_list(self, group: str, key: str, value: str) -> None:
        values = self._lists.setdefault((group, key), [])
        if value not in values:
            values.append(value)
        self.set(group, key, "".join(f"{item};" for item in values))

    def render(self) -> str:
        lines = []
        for group, values in self._groups.items():
            lines.append(f"[{group}]")
            for key, value in values.items():
                lines.append(f"{key}={value}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _apply_finish_args(editor: _MetadataEditor, args: list[str]) -> None:
    index = 0
    while index < len(args):
        option, value, index = _finish_arg(args, index)
        if option == "command":
            editor.set("Application", "command", value)
        elif option == "share":
            editor.add_list("Context", "shared", value)
        elif option == "socket":
            editor.add_list("Context", "sockets", value)
        elif option == "device":
            editor.add_list("Context", "devices", value)
        elif option == "filesystem":
            editor.add_list("Context", "filesystems", value)
        elif option == "persist":
            editor.add_list("Context", "persistent", value)
        elif option == "allow":
            editor.add_list("Context", "features", value)
        elif option == "env":
            key, env_value = _split_assignment(value, option)
            editor.set("Environment", key, env_value)
        elif option == "unset-env":
            editor.add_list("Context", "unset-environment", value)
        elif option == "own-name":
            editor.set("Session Bus Policy", value, "own")
        elif option == "talk-name":
            editor.set("Session Bus Policy", value, "talk")
        elif option == "no-talk-name":
            editor.set("Session Bus Policy", value, "none")
        elif option == "system-own-name":
            editor.set("System Bus Policy", value, "own")
        elif option == "system-talk-name":
            editor.set("System Bus Policy", value, "talk")
        elif option == "system-no-talk-name":
            editor.set("System Bus Policy", value, "none")
        elif option == "a11y-own-name":
            editor.set("Accessibility Bus Policy", value, "own")
        elif option == "a11y-talk-name":
            editor.set("Accessibility Bus Policy", value, "talk")
        elif option == "metadata":
            group, key, metadata_value = _split_metadata(value)
            editor.set(group, key, metadata_value)
        elif option == "extension":
            name, key, extension_value = _split_metadata(value)
            editor.set(f"Extension {name}", key, extension_value)
        else:
            raise ConfigError(f"unsupported flatpak finish arg: --{option}")


def _finish_arg(args: list[str], index: int) -> tuple[str, str, int]:
    raw = args[index]
    if not raw.startswith("--"):
        raise ConfigError(f"unsupported flatpak finish arg: {raw}")
    option = raw[2:]
    if "=" in option:
        option, value = option.split("=", 1)
        return option, value, index + 1
    if index + 1 >= len(args):
        raise ConfigError(f"flatpak finish arg requires a value: {raw}")
    return option, args[index + 1], index + 2


def _split_assignment(value: str, option: str) -> tuple[str, str]:
    key, separator, rest = value.partition("=")
    if not separator or not key:
        raise ConfigError(f"flatpak --{option} must be KEY=VALUE")
    return key, rest


def _split_metadata(value: str) -> tuple[str, str, str]:
    first, separator, rest = value.partition("=")
    if not separator or not first:
        raise ConfigError("flatpak metadata values must be GROUP=KEY[=VALUE]")
    key, separator, metadata_value = rest.partition("=")
    if not key:
        raise ConfigError("flatpak metadata values must be GROUP=KEY[=VALUE]")
    if not separator:
        metadata_value = "true"
    return first, key, metadata_value


def _write_flatpak_containerfile(
    *,
    final_build_dir: Path,
    flatpak_dir: Path,
    card: FlatpakCard,
    build_image: str,
    orchestrator: str,
    metadata: str,
    app_ref: str,
    branch: str,
    flatpak_arch: str,
) -> None:
    _remove_tree(final_build_dir)
    final_build_dir.mkdir(parents=True, exist_ok=True)
    files_dir = final_build_dir / "files"
    staged_file_count = _stage_flatpak_files(card, flatpak_dir, files_dir)
    containerfile = final_build_dir / "Containerfile"
    timestamp = str(int(time.time()))
    lines = [
        f"FROM {build_image} AS rpms",
        f"FROM {orchestrator} AS build",
        "COPY --from=rpms /rpms /rpms",
        "COPY --from=rpms /files /ludos/build-files",
        "RUN <<'LUDOS_INSTALL_FLATPAK_RPMS'",
        "set -eux",
        "mkdir -p /flatpak",
        "rpm --root /flatpak --initdb",
        "rpm --root /flatpak -Uvh --allfiles --nodeps --noscripts --notriggers /rpms/*.rpm",
        "if [ -d /ludos/build-files ]; then cp -a /ludos/build-files/. /flatpak/; fi",
        "LUDOS_INSTALL_FLATPAK_RPMS",
    ]
    if staged_file_count:
        lines.append("COPY files/ /flatpak/")
    if card.postprocess.strip():
        lines.extend(
            [
                "WORKDIR /flatpak",
                "RUN <<'LUDOS_FLATPAK_POSTPROCESS'",
                "set -eux",
                card.postprocess.rstrip(),
                "LUDOS_FLATPAK_POSTPROCESS",
                "WORKDIR /",
            ]
        )
    prettify_lines = [
        "RUN <<'LUDOS_PRETTIFY_FLATPAK'",
        "set -eux",
        "if [ -d /flatpak/usr ]; then",
        "  usr_entries=\"$(find /flatpak/usr -mindepth 1 \\( -type f -o -type l \\) -print | sort || true)\"",
        "  if [ -n \"$usr_entries\" ]; then",
        "    echo 'warning: removing /usr entries from flatpak payload:' >&2",
        "    printf '%s\\n' \"$usr_entries\" >&2",
        "  fi",
        "  rm -rf /flatpak/usr",
        "fi",
        "if [ ! -d /flatpak/app ]; then echo 'flatpak payload did not create /app' >&2; exit 1; fi",
        "rm -rf /out",
        "mkdir -p /out/files /out/export",
        "cp -a /flatpak/app/. /out/files/",
        *_rename_lines(card.flatpak),
        *_rename_display_lines(card.flatpak),
        *_appdata_lines(card.flatpak),
        *(
            []
            if card.flatpak.runtime
            else _appstream_compose_lines(card.flatpak, app_ref)
        ),
        *([] if card.flatpak.runtime else _export_lines(card.flatpak)),
        "cat > /out/metadata <<'LUDOS_FLATPAK_METADATA'",
        metadata.rstrip(),
        "LUDOS_FLATPAK_METADATA",
        "LUDOS_PRETTIFY_FLATPAK",
    ]
    lines.extend(prettify_lines)
    containerfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_flatpak_label_file(
        final_build_dir,
        _flatpak_image_labels(
            card.flatpak,
            metadata=metadata,
            app_ref=app_ref,
            branch=branch,
            flatpak_arch=flatpak_arch,
            timestamp=timestamp,
        ),
    )
    log(f"Wrote flatpak Containerfile: {containerfile}")


def _flatpak_image_labels(
    config: FlatpakConfig,
    *,
    metadata: str,
    app_ref: str,
    branch: str,
    flatpak_arch: str,
    timestamp: str,
) -> tuple[tuple[str, str], ...]:
    return (
        ("org.flatpak.ref", app_ref),
        ("org.flatpak.metadata", metadata),
        ("org.flatpak.subject", f"Export {config.app_id}"),
        ("org.flatpak.body", _flatpak_body(config.app_id, flatpak_arch, branch)),
        ("org.flatpak.timestamp", timestamp),
        ("org.opencontainers.image.ref.name", app_ref),
        ("org.anatase.flatpak.branch", branch),
        ("org.anatase.flatpak.arch", flatpak_arch),
    )


def _write_flatpak_label_file(
    build_dir: Path,
    labels: tuple[tuple[str, str], ...],
) -> None:
    (build_dir / "labels.json").write_text(
        json.dumps(labels, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _read_flatpak_label_file(build_dir: Path) -> tuple[tuple[str, str], ...]:
    path = build_dir / "labels.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"invalid flatpak labels: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError("flatpak labels must be a list")
    labels = []
    for item in data:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise ConfigError("flatpak labels must contain string pairs")
        labels.append((item[0], item[1]))
    return tuple(labels)


def _flatpak_commit_metadata_labels(
    metadata: str,
    app_ref: str,
    *,
    download_size: int = 0,
    installed_size: int = 0,
) -> dict[str, str]:
    return {
        "org.flatpak.commit-metadata.xa.metadata": _gvariant_string_variant_b64(metadata),
        "org.flatpak.commit-metadata.xa.ref": _gvariant_string_variant_b64(app_ref),
        "org.flatpak.commit-metadata.ostree.ref-binding": _gvariant_strv_variant_b64((app_ref,)),
        "org.flatpak.commit-metadata.ostree.collection-binding": _gvariant_string_variant_b64(""),
        "org.flatpak.commit-metadata.xa.download-size": _gvariant_uint64_variant_b64(download_size),
        "org.flatpak.commit-metadata.xa.installed-size": _gvariant_uint64_variant_b64(installed_size),
        "org.flatpak.download-size": str(download_size),
        "org.flatpak.installed-size": str(installed_size),
    }


def _gvariant_string_variant_b64(value: str) -> str:
    return base64.b64encode(value.encode() + b"\0\0s").decode()


def _gvariant_uint64_variant_b64(value: int) -> str:
    return base64.b64encode(struct.pack(">Q", value) + b"\0t").decode()


def _gvariant_strv_variant_b64(values: tuple[str, ...]) -> str:
    body = bytearray()
    offsets = []
    for value in values:
        body.extend(value.encode())
        body.append(0)
        offsets.append(len(body))
    if any(offset > 255 for offset in offsets):
        raise ConfigError("flatpak commit metadata string array is too large")
    body.extend(offsets)
    body.extend(b"\0as")
    return base64.b64encode(bytes(body)).decode()


def _stage_flatpak_files(card: FlatpakCard, flatpak_dir: Path, files_dir: Path) -> int:
    if not card.files:
        return 0
    _remove_tree(files_dir)
    count = 0
    for entry in card.files:
        target, source = _parse_file_entry(entry)
        source_relpath = _relative_path(source, card.source or flatpak_dir, "files source")
        target_relpath = _relative_path(target, card.source or flatpak_dir, "files destination")
        source_path = (flatpak_dir / source_relpath).resolve()
        try:
            source_path.relative_to(flatpak_dir.resolve())
        except ValueError as exc:
            raise ConfigError(f"{card.source}: files entry '{entry}' escapes the flatpak directory") from exc
        target_path = files_dir / target_relpath
        if source_path.is_dir():
            shutil.copytree(source_path, target_path, dirs_exist_ok=True)
            count += sum(1 for path in source_path.rglob("*") if path.is_file())
        elif source_path.is_file():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            count += 1
        else:
            raise ConfigError(f"{card.source}: files entry '{entry}' is missing")
    log(f"Staged {count} flatpak files")
    return count


def _parse_file_entry(value: str) -> tuple[str, str]:
    if "::" not in value:
        return value.strip(), value.strip()
    target, source = (part.strip() for part in value.split("::", 1))
    if not target or not source:
        raise ConfigError(f"files entry '{value}' must be '<destination>::<source>'")
    return target, source


def _relative_path(value: str, source: Path, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ConfigError(f"{source}: {label} '{value}' must be a relative path without '..'")
    return path


def _rename_lines(config: FlatpakConfig) -> list[str]:
    lines = []
    if config.rename_desktop_file:
        source = shlex.quote(f"/out/files/share/applications/{config.rename_desktop_file}")
        target = shlex.quote(f"/out/files/share/applications/{config.app_id}.desktop")
        lines.append(f"[ ! -e {source} ] || mv -f {source} {target}")
    if config.rename_appdata_file:
        for directory in ("appdata", "metainfo"):
            source = shlex.quote(f"/out/files/share/{directory}/{config.rename_appdata_file}")
            suffix = ".metainfo.xml" if directory == "metainfo" else ".appdata.xml"
            target = shlex.quote(f"/out/files/share/{directory}/{config.app_id}{suffix}")
            lines.append(f"[ ! -e {source} ] || mv -f {source} {target}")
    if config.rename_icon:
        icon_glob = shlex.quote(f"{config.rename_icon}.*")
        new = shlex.quote(config.app_id)
        old_icon = shlex.quote(config.rename_icon)
        lines.extend(
            [
                "if [ -d /out/files/share/icons ]; then",
                f"  find /out/files/share/icons -type f -name {icon_glob} | while read -r icon; do",
                '    ext="${icon##*.}"',
                f"    mv -f \"$icon\" \"$(dirname \"$icon\")\"/{new}.\"$ext\"",
                "  done",
                "fi",
                "if [ -d /out/files/share/applications ]; then",
                f"  old_icon={old_icon}",
                f"  new_icon={new}",
                "  export old_icon new_icon",
                "  find /out/files/share/applications -type f -name '*.desktop' -exec sh -c '",
                "    for desktop do",
                "      sed -i \"s/^Icon=${old_icon}$/Icon=${new_icon}/\" \"$desktop\"",
                "    done",
                "  ' sh {} +",
                "fi",
            ]
        )
    return lines


def _rename_display_lines(config: FlatpakConfig) -> list[str]:
    if not config.rename:
        return []
    app_id = shlex.quote(config.app_id)
    display_name = shlex.quote(config.rename)
    return [
        f"app_id={app_id}",
        f"display_name={display_name}",
        "desktop_file=\"/out/files/share/applications/$app_id.desktop\"",
        "if [ -f \"$desktop_file\" ]; then",
        "  DISPLAY_NAME=\"$display_name\" DESKTOP_FILE=\"$desktop_file\" python3 - <<'LUDOS_RENAME_DESKTOP'",
        "import os",
        "",
        "path = os.environ['DESKTOP_FILE']",
        "display_name = os.environ['DISPLAY_NAME']",
        "lines = []",
        "with open(path, encoding='utf-8') as desktop:",
        "    for line in desktop:",
        "        if line.startswith('Name='):",
        "            line = f'Name={display_name}\\n'",
        "        lines.append(line)",
        "with open(path, 'w', encoding='utf-8') as desktop:",
        "    desktop.writelines(lines)",
        "LUDOS_RENAME_DESKTOP",
        "fi",
    ]


def _appdata_lines(config: FlatpakConfig) -> list[str]:
    app_id = shlex.quote(config.app_id)
    display_name = shlex.quote(config.rename)
    app_icon = shlex.quote(config.app_id if config.rename_icon else "")
    return [
        f"app_id={app_id}",
        f"display_name={display_name}",
        f"app_icon={app_icon}",
        "appdata_source=",
        "for candidate in \\",
        '  "/out/files/share/appdata/$app_id.appdata.xml" \\',
        '  "/out/files/share/appdata/$app_id.metainfo.xml" \\',
        '  "/out/files/share/metainfo/$app_id.appdata.xml" \\',
        '  "/out/files/share/metainfo/$app_id.metainfo.xml"; do',
        "  if [ -f \"$candidate\" ]; then",
        "    appdata_source=\"$candidate\"",
        "    break",
        "  fi",
        "done",
        "if [ -n \"$appdata_source\" ]; then",
        "  mkdir -p /out/files/share/appdata",
        '  appdata_target="/out/files/share/appdata/$app_id.appdata.xml"',
        '  if [ "$appdata_source" != "$appdata_target" ]; then',
        '    cp -a "$appdata_source" "$appdata_target"',
        "  fi",
        "  APPDATA_FILE=\"/out/files/share/appdata/$app_id.appdata.xml\" APP_ID=\"$app_id\" DISPLAY_NAME=\"$display_name\" APP_ICON=\"$app_icon\" python3 - <<'LUDOS_REWRITE_APPDATA'",
        "import os",
        "import xml.etree.ElementTree as ET",
        "",
        "path = os.environ['APPDATA_FILE']",
        "app_id = os.environ['APP_ID']",
        "display_name = os.environ['DISPLAY_NAME']",
        "app_icon = os.environ['APP_ICON']",
        "tree = ET.parse(path)",
        "root = tree.getroot()",
        "component_id = root.find('id')",
        "if component_id is not None and component_id.text and component_id.text != app_id:",
        "    old_id = component_id.text",
        "    component_id.text = app_id",
        "    provides = root.find('provides')",
        "    if provides is None:",
        "        provides = ET.SubElement(root, 'provides')",
        "    if not any(child.tag == 'id' and child.text == old_id for child in list(provides)):",
        "        ET.SubElement(provides, 'id').text = old_id",
        "if display_name:",
        "    name = root.find('name')",
        "    if name is None:",
        "        name = ET.SubElement(root, 'name')",
        "    name.text = display_name",
        "for launchable in root.findall('launchable'):",
        "    if launchable.get('type') == 'desktop-id':",
        "        launchable.text = f'{app_id}.desktop'",
        "if app_icon:",
        "    icons = list(root.findall('icon'))",
        "    if not icons:",
        "        icons = [ET.SubElement(root, 'icon', {'type': 'stock'})]",
        "    for icon in root.findall('icon'):",
        "        icon_type = icon.get('type')",
        "        if icon_type in (None, '', 'stock', 'cached', 'local') and (not icon.text or '://' not in icon.text):",
        "            icon.text = app_icon",
        "tree.write(path, encoding='UTF-8', xml_declaration=True)",
        "LUDOS_REWRITE_APPDATA",
        "  mkdir -p /out/files/share/metainfo",
        '  cp -a "/out/files/share/appdata/$app_id.appdata.xml" "/out/files/share/metainfo/$app_id.appdata.xml"',
        '  rm -rf "/out/files/share/metainfo/$app_id.metainfo.xml"',
        "fi",
    ]


def _appstream_compose_lines(config: FlatpakConfig, app_ref: str) -> list[str]:
    app_id = shlex.quote(config.app_id)
    ref = shlex.quote(app_ref)
    display_name = shlex.quote(config.rename)
    author_template = shlex.quote(config.rename_author)
    app_icon = shlex.quote(config.app_id if config.rename_icon else "")
    return [
        f"app_id={app_id}",
        f"app_ref={ref}",
        f"display_name={display_name}",
        f"author_template={author_template}",
        f"app_icon={app_icon}",
        "if [ -f \"/out/files/share/appdata/$app_id.appdata.xml\" ]; then",
        "  if ! command -v appstreamcli >/dev/null 2>&1; then",
        "    echo 'appstreamcli is required to build flatpak app-info metadata' >&2",
        "    exit 1",
        "  fi",
        "  appstreamcli compose --verbose --prefix /out/files --origin flatpak --components \"$app_id\" --data-dir /out/files/share/app-info/xmls --icons-dir /out/files/share/app-info/icons/flatpak /",
        "  appstream_xml=\"/out/files/share/app-info/xmls/$app_id.xml.gz\"",
        "  if [ ! -f \"$appstream_xml\" ]; then",
        "    composed_xml=\"$(find /out/files/share/app-info/xmls -type f -name '*.xml.gz' | sort | head -n 1 || true)\"",
        "    if [ -n \"$composed_xml\" ]; then",
        "      cp -a \"$composed_xml\" \"$appstream_xml\"",
        "    fi",
        "  fi",
        "  if [ -f \"$appstream_xml\" ] && command -v gzip >/dev/null 2>&1; then",
        "    appstream_tmp=\"/out/appstream-$app_id.xml\"",
        "    gzip -dc \"$appstream_xml\" > \"$appstream_tmp\"",
        "    APPSTREAM_FILE=\"$appstream_tmp\" APP_ID=\"$app_id\" APP_REF=\"$app_ref\" DISPLAY_NAME=\"$display_name\" AUTHOR_TEMPLATE=\"$author_template\" APP_ICON=\"$app_icon\" python3 - <<'LUDOS_REWRITE_APPSTREAM'",
        "import os",
        "import xml.etree.ElementTree as ET",
        "",
        "path = os.environ['APPSTREAM_FILE']",
        "app_id = os.environ['APP_ID']",
        "app_ref = os.environ['APP_REF']",
        "display_name = os.environ['DISPLAY_NAME']",
        "author_template = os.environ['AUTHOR_TEMPLATE']",
        "app_icon = os.environ['APP_ICON']",
        "tree = ET.parse(path)",
        "root = tree.getroot()",
        "def rewrite_author(component):",
        "    if not author_template:",
        "        return",
        "    author = ''",
        "    developer = component.find('developer')",
        "    if developer is not None:",
        "        name = developer.find('name')",
        "        if name is not None and name.text:",
        "            author = name.text.strip()",
        "    developer_names = component.findall('developer_name')",
        "    if not author:",
        "        for developer_name in developer_names:",
        "            if developer_name.text:",
        "                author = developer_name.text.strip()",
        "                break",
        "    if '%s' in author_template:",
        "        prefix, suffix = author_template.split('%s', 1)",
        "        while author.startswith(prefix) and author.endswith(suffix):",
        "            author = author[len(prefix):]",
        "            if suffix:",
        "                author = author[:-len(suffix)]",
        "            author = author.strip()",
        "        rewritten = author_template.replace('%s', author).strip()",
        "    else:",
        "        rewritten = author_template.strip()",
        "    if not rewritten:",
        "        return",
        "    if developer is None:",
        "        developer = ET.SubElement(component, 'developer')",
        "    name = developer.find('name')",
        "    if name is None:",
        "        name = ET.SubElement(developer, 'name')",
        "    name.text = rewritten",
        "    for developer_name in developer_names:",
        "        developer_name.text = rewritten",
        "for component in root.iter('component'):",
        "    component_id = component.find('id')",
        "    if component_id is not None and component_id.text != app_id:",
        "        continue",
        "    bundle = None",
        "    for candidate in component.findall('bundle'):",
        "        if candidate.get('type') == 'flatpak':",
        "            bundle = candidate",
        "            break",
        "    if bundle is None:",
        "        bundle = ET.SubElement(component, 'bundle', {'type': 'flatpak'})",
        "    bundle.text = app_ref",
        "    if display_name:",
        "        name = component.find('name')",
        "        if name is None:",
        "            name = ET.SubElement(component, 'name')",
        "        name.text = display_name",
        "    rewrite_author(component)",
        "    for launchable in component.findall('launchable'):",
        "        if launchable.get('type') == 'desktop-id':",
        "            launchable.text = f'{app_id}.desktop'",
        "    if app_icon:",
        "        icons = list(component.findall('icon'))",
        "        if not icons:",
        "            icons = [ET.SubElement(component, 'icon', {'type': 'stock'})]",
        "        for icon in component.findall('icon'):",
        "            icon_type = icon.get('type')",
        "            if icon_type in (None, '', 'stock', 'cached', 'local') and (not icon.text or '://' not in icon.text):",
        "                icon.text = app_icon",
        "tree.write(path, encoding='UTF-8', xml_declaration=True)",
        "LUDOS_REWRITE_APPSTREAM",
        "    gzip -n -c \"$appstream_tmp\" > \"$appstream_xml\"",
        "    rm -f \"$appstream_tmp\"",
        "  fi",
        "fi",
    ]


def _export_lines(config: FlatpakConfig) -> list[str]:
    app_id = shlex.quote(config.app_id)
    return [
        f"app_id={app_id}",
        "if [ -f \"/out/files/share/applications/$app_id.desktop\" ]; then",
        "  mkdir -p /out/export/share/applications",
        "  cp -a \"/out/files/share/applications/$app_id.desktop\" /out/export/share/applications/",
        "fi",
        "for dir in appdata metainfo; do",
        "  if [ -d \"/out/files/share/$dir\" ]; then",
        "    mkdir -p \"/out/export/share/$dir\"",
        "    find \"/out/files/share/$dir\" -maxdepth 1 -type f \\( -name \"$app_id.appdata.xml\" -o -name \"$app_id.metainfo.xml\" \\) -exec cp -a -t \"/out/export/share/$dir\" {} +",
        "  fi",
        "done",
        "if [ -d /out/files/share/icons ]; then",
        "  find /out/files/share/icons -type f -name \"$app_id.*\" | while read -r icon; do",
        "    target=\"/out/export/${icon#/out/files/}\"",
        "    mkdir -p \"$(dirname \"$target\")\"",
        "    cp -a \"$icon\" \"$target\"",
        "  done",
        "fi",
        "if [ -d /out/files/share/mime/packages ]; then",
        "  mkdir -p /out/export/share/mime/packages",
        "  find /out/files/share/mime/packages -maxdepth 1 -type f -name \"$app_id*.xml\" -exec cp -a -t /out/export/share/mime/packages {} +",
        "fi",
        "for dir in dbus-1 gnome-shell krunner; do",
        "  if [ -d \"/out/files/share/$dir\" ]; then",
        "    mkdir -p \"/out/export/share/$dir\"",
        "    cp -a \"/out/files/share/$dir/.\" \"/out/export/share/$dir/\"",
        "  fi",
        "done",
    ]


def _flatpak_body(app_id: str, flatpak_arch: str, branch: str) -> str:
    return f"Name: {app_id}\nArch: {flatpak_arch}\nBranch: {branch}\nBuilt with: Ludos\n"


def _run_flatpak_image_build(
    podman: str,
    buildah: str,
    build_dir: Path,
    image: str,
    metadata: str,
    app_id: str,
    *,
    flatpak_images: FlatpakImagesConfig = FlatpakImagesConfig(),
) -> None:
    containerfile = build_dir / "Containerfile"
    final_containerfile = build_dir / "Containerfile.final"
    build_iidfile = build_dir / "build-image.id"
    final_iidfile = build_dir / "final-image.id"
    build_stage_image = f"{image}-build-stage"
    build_image_id = ""
    final_image_id = ""
    try:
        build_iidfile.unlink(missing_ok=True)
        final_iidfile.unlink(missing_ok=True)
        command = _flatpak_build_stage_command(
            podman,
            containerfile,
            build_dir,
            build_stage_image,
            build_iidfile,
        )
        returncode, _output = _run_streamed_command(
            command,
            line_rewriter=_flatpak_build_stage_line,
        )
        if returncode != 0:
            raise ConfigError(
                f"flatpak appstream label build failed with exit status {returncode}"
            )
        build_image_id = build_iidfile.read_text(encoding="utf-8").strip()
        if not build_image_id:
            raise ConfigError("flatpak appstream label build did not write an image ID")
        appstream_labels = _flatpak_appstream_labels(
            podman,
            build_dir,
            build_image_id,
            app_id,
            files_root="/out/files",
        )
        appstream_labels = _flatpak_appstream_labels_with_remote_icon(
            appstream_labels,
            app_id,
            flatpak_images.uri,
        )
        base_labels = _read_flatpak_label_file(build_dir)
        labels_by_name = dict(base_labels)
        payload_size = _flatpak_payload_size(podman, build_image_id, "/out")
        commit_metadata_labels = _flatpak_commit_metadata_labels(
            metadata,
            labels_by_name["org.flatpak.ref"],
            download_size=payload_size,
            installed_size=payload_size,
        )
        labels = (
            *base_labels,
            *commit_metadata_labels.items(),
            *tuple(sorted(appstream_labels.items())),
        )
        _write_flatpak_final_containerfile(
            final_containerfile,
            build_image_id=build_image_id,
        )

        command = _flatpak_final_image_build_command(
            podman,
            final_containerfile,
            build_dir,
            final_iidfile,
        )
        returncode, _output = _run_streamed_command(command)
        if returncode != 0:
            raise ConfigError(f"flatpak image build failed with exit status {returncode}")
        final_image_id = final_iidfile.read_text(encoding="utf-8").strip()
        if not final_image_id:
            raise ConfigError("flatpak image build did not write an image ID")
        _label_flatpak_image(
            buildah,
            source_image=final_image_id,
            image=image,
            labels=labels,
        )
    finally:
        if build_image_id:
            images = [build_stage_image, build_image_id]
            if final_image_id:
                images.append(final_image_id)
            subprocess.run(
                [podman, "rmi", *images],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _flatpak_build_stage_command(
    podman: str,
    containerfile: Path,
    build_dir: Path,
    image: str,
    iidfile: Path,
) -> list[str]:
    return [
        podman,
        "build",
        "--pull=false",
        "--tag",
        image,
        "--iidfile",
        str(iidfile),
        "--file",
        str(containerfile),
        "--target",
        "build",
        str(build_dir),
    ]


def _flatpak_build_stage_line(line: str) -> str:
    stripped = line.strip()
    if _is_sha256_value(stripped) or (
        stripped.startswith("sha256:")
        and _is_sha256_value(stripped.removeprefix("sha256:"))
    ):
        return "Built flatpak build stage\n"
    return line


def _is_sha256_value(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _flatpak_final_image_build_command(
    podman: str,
    containerfile: Path,
    build_dir: Path,
    iidfile: Path,
) -> list[str]:
    return [
        podman,
        "build",
        "--pull=false",
        "--iidfile",
        str(iidfile),
        "--file",
        str(containerfile),
        str(build_dir),
    ]


def _label_flatpak_image(
    buildah: str,
    *,
    source_image: str,
    image: str,
    labels: tuple[tuple[str, str], ...],
) -> None:
    container = ""
    try:
        result = _run_buildah_flatpak_command(
            [buildah, "from", "--quiet", source_image],
            action="create label container",
            capture_stdout=True,
        )
        container = result.stdout.strip()
        if not container:
            raise ConfigError("flatpak image label container was not created")
        for key, value in labels:
            _run_buildah_flatpak_command(
                [buildah, "config", "--label", f"{key}={value}", container],
                action=f"set label {key}",
            )
        _run_buildah_flatpak_command(
            [
                buildah,
                "commit",
                "--squash",
                "--format",
                "oci",
                container,
                image,
            ],
            action="commit labeled image",
        )
    finally:
        if container:
            subprocess.run(
                [buildah, "rm", container],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _run_buildah_flatpak_command(
    command: list[str],
    *,
    action: str,
    capture_stdout: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise ConfigError(
            f"flatpak image buildah {action} failed with exit status "
            f"{result.returncode}"
        )
    return result


def _write_flatpak_final_containerfile(
    containerfile: Path,
    *,
    build_image_id: str,
) -> None:
    lines = [
        "FROM scratch",
        f"COPY --from={build_image_id} /out/ /",
    ]
    containerfile.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _flatpak_appstream_labels(
    podman: str,
    build_dir: Path,
    image: str,
    app_id: str,
    *,
    files_root: str = "/files",
) -> dict[str, str]:
    label_dir = build_dir / "appstream-labels"
    _remove_tree(label_dir)
    label_dir.mkdir(parents=True, exist_ok=True)
    labels = {}
    files_root = files_root.rstrip("/")

    container = subprocess.run(
        [podman, "create", image],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    try:
        appstream_gz = label_dir / f"{app_id}.xml.gz"
        if _podman_cp(podman, container, f"{files_root}/share/app-info/xmls/{app_id}.xml.gz", appstream_gz):
            appstream_xml = gzip.decompress(appstream_gz.read_bytes()).decode("utf-8")
            labels["org.freedesktop.appstream.appdata"] = _compact_xml(appstream_xml)

        for size in ("64", "128"):
            icon = label_dir / f"{app_id}-{size}.png"
            for source in (
                f"{files_root}/share/app-info/icons/flatpak/{size}x{size}/{app_id}.png",
                f"{files_root}/share/icons/hicolor/{size}x{size}/apps/{app_id}.png",
            ):
                if _podman_cp(podman, container, source, icon):
                    data = base64.b64encode(icon.read_bytes()).decode()
                    labels[f"org.freedesktop.appstream.icon-{size}"] = f"data:image/png;base64,{data}"
                    break
    finally:
        subprocess.run([podman, "rm", "-f", container], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return labels


def _flatpak_appstream_labels_with_remote_icon(
    labels: dict[str, str],
    app_id: str,
    uri: str,
) -> dict[str, str]:
    if not uri or "org.freedesktop.appstream.icon-128" not in labels:
        return labels
    appdata = labels.get("org.freedesktop.appstream.appdata")
    if not appdata:
        return labels
    try:
        root = ET.fromstring(appdata)
    except ET.ParseError as exc:
        raise ConfigError(
            f"invalid flatpak AppStream metadata for {app_id}: {exc}"
        ) from exc

    icon_url = _join_uri(uri, "128x128", f"{app_id}.png")
    changed = False
    for component in root.iter("component"):
        component_id = component.findtext("id")
        if component_id is not None and component_id != app_id:
            continue
        if any(
            icon.get("type") == "remote"
            and (icon.text or "").strip() == icon_url
            for icon in component.findall("icon")
        ):
            continue
        icon = ET.SubElement(
            component,
            "icon",
            {"type": "remote", "width": "128", "height": "128"},
        )
        icon.text = icon_url
        changed = True
    if not changed:
        return labels

    result = dict(labels)
    result["org.freedesktop.appstream.appdata"] = _compact_xml(
        ET.tostring(root, encoding="unicode")
    )
    return result


def _join_uri(base: str, *parts: str) -> str:
    return "/".join((base.rstrip("/"), *(part.strip("/") for part in parts)))


def _flatpak_payload_size(podman: str, image: str, path: str) -> int:
    result = subprocess.run(
        [
            podman,
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            image,
            "-c",
            f"du -sb {shlex.quote(path)} | cut -f1",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    value = result.stdout.strip()
    try:
        size = int(value)
    except ValueError as exc:
        raise ConfigError(f"invalid flatpak payload size: {value}") from exc
    if size <= 0:
        raise ConfigError(f"invalid flatpak payload size: {size}")
    return size


def _compact_xml(value: str) -> str:
    return " ".join(line.strip() for line in value.splitlines())


def _podman_cp(podman: str, container: str, source: str, target: Path) -> bool:
    result = subprocess.run(
        [podman, "cp", f"{container}:{source}", str(target)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _hash_lines(values: tuple[str, ...]) -> str:
    payload = "\n".join(sorted(values)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def _flatpak_final_hash(
    *,
    app_name: str,
    app_ref: str,
    branch: str,
    flatpak_arch: str,
    card: FlatpakCard,
    flatpak_dir: Path,
    substitution_env: dict[str, str],
    build_image: str,
    metadata: str,
    flatpak_images: FlatpakImagesConfig,
) -> str:
    flatpak_payload = {
        "app_id": card.flatpak.app_id,
        "command": card.flatpak.command,
        "finish_args": card.flatpak.finish_args,
        "rename": card.flatpak.rename,
        "rename_author": card.flatpak.rename_author,
        "rename_icon": card.flatpak.rename_icon,
        "rename_desktop_file": card.flatpak.rename_desktop_file,
        "rename_appdata_file": card.flatpak.rename_appdata_file,
        "add_extensions": card.flatpak.add_extensions,
    }
    if card.flatpak.runtime:
        flatpak_payload["runtime"] = card.flatpak.runtime
        flatpak_payload["version"] = card.flatpak.version
    payload = {
        "app_name": app_name,
        "app_ref": app_ref,
        "branch": branch,
        "flatpak_arch": flatpak_arch,
        "flatpak": flatpak_payload,
        "substitution_env": tuple(sorted(substitution_env.items())),
        "build_image": _image_tag(build_image),
        "metadata": metadata,
        "files": _flatpak_file_hash_inputs(card, flatpak_dir),
        "postprocess": card.postprocess,
        "flatpak_images": {
            "uri": flatpak_images.uri,
            "s3": flatpak_images.s3,
            "overlay": flatpak_images.overlay,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def _flatpak_file_hash_inputs(
    card: FlatpakCard,
    flatpak_dir: Path,
) -> tuple[tuple[str, str, str], ...]:
    entries = []
    for entry in card.files:
        target, source = _parse_file_entry(entry)
        source_relpath = _relative_path(source, card.source or flatpak_dir, "files source")
        source_path = (flatpak_dir / source_relpath).resolve()
        try:
            source_path.relative_to(flatpak_dir.resolve())
        except ValueError as exc:
            raise ConfigError(
                f"{card.source}: files entry '{entry}' escapes the flatpak directory"
            ) from exc
        if source_path.is_file():
            entries.append((target, source, _hash_path_contents(source_path)))
        elif source_path.is_dir():
            for file_path in sorted(path for path in source_path.rglob("*") if path.is_file()):
                relative = file_path.relative_to(source_path).as_posix()
                entries.append(
                    (
                        f"{target.rstrip('/')}/{relative}",
                        f"{source.rstrip('/')}/{relative}",
                        _hash_path_contents(file_path),
                    )
                )
        else:
            raise ConfigError(f"{card.source}: files entry '{entry}' is missing")
    return tuple(entries)


def _hash_path_contents(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _image_tag(image: str) -> str:
    return image.rsplit(":", 1)[-1]

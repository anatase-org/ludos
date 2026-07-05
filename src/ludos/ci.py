from __future__ import annotations

import base64
import lzma
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .build import (
    ResolvedBuildMetadata,
    _cleanup_dnf_workspace_paths,
    _ensure_image,
    _image_tag,
    _remove_tree,
    build_package_card_images,
    resolve_build_manifest_context,
    resolve_build_manifests_from_contexts,
)
from .common import ResolvedManifestContext
from .flatpaks import FlatpakBuildPlan, plan_manifest_flatpaks_with_context
from .logging import log
from .model import ConfigError


class _LiteralString(str):
    pass


class _CiBuildManifestDumper(yaml.SafeDumper):
    pass


def _represent_literal_string(
    dumper: yaml.SafeDumper,
    value: _LiteralString,
) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


_CiBuildManifestDumper.add_representer(
    _LiteralString,
    _represent_literal_string,
)


def prepare_ci(
    manifest_paths: tuple[Path, ...],
    *,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ccache: bool = True,
    full: bool = False,
) -> Path:
    if not manifest_paths:
        raise ConfigError("at least one manifest is required")

    cache_root = _resolve_cache_root(manifest_paths, cache_dir)
    dnf_workspace_dirs: list[Path] = []
    manifest_contexts: list[tuple[Path, ResolvedManifestContext]] = []
    try:
        for manifest_path in manifest_paths:
            context = resolve_build_manifest_context(
                manifest_path,
                cards_dir=cards_dir,
                cache_dir=cache_root,
                cache_version=cache_version,
                cache_only=cache_only,
                ccache=ccache,
                dnf_workspace_dirs=dnf_workspace_dirs,
            )
            manifest_contexts.append((manifest_path, context))

        metadata = resolve_build_manifests_from_contexts(
            tuple(manifest_contexts),
            cards_dir=cards_dir,
            cache_only=cache_only,
        )
        build_package_card_images(metadata, cache_only=cache_only)
        flatpaks = tuple(
            _flatpak_entry(manifest_path, context, plan)
            for manifest_path, context in manifest_contexts
            for plan in plan_manifest_flatpaks_with_context(
                context,
                manifest_path=manifest_path,
                cache_only=cache_only,
            )
            if full
            or not _ensure_image(
                context.podman,
                plan.output_image,
                getattr(context, "ci_registry", ""),
            )
        )
        output = cache_root / "ci" / "build.yml"
        _build_output, encoded_output = _write_ci_build_manifest(
            output,
            manifest_contexts=tuple(manifest_contexts),
            metadata=metadata,
            flatpaks=flatpaks,
            full=full,
        )
        log(f"Wrote CI build manifest: {output}")
        log(
            f"Wrote encoded CI build manifest: {encoded_output} "
            f"({_size_kib(encoded_output)} KiB)"
        )
        return output
    finally:
        context_paths = {
            context.dnf_workspace_dir
            for _path, context in manifest_contexts
        }
        _cleanup_contexts(tuple(manifest_contexts))
        _cleanup_dnf_workspace_paths(
            tuple(path for path in dnf_workspace_dirs if path not in context_paths)
        )


def _resolve_cache_root(
    manifest_paths: tuple[Path, ...],
    cache_dir: Path | None,
) -> Path:
    if cache_dir is not None:
        return cache_dir.expanduser().resolve()
    return (manifest_paths[0].resolve().parent / "cache").resolve()


def _write_ci_build_manifest(
    output: Path,
    *,
    manifest_contexts: tuple[tuple[Path, ResolvedManifestContext], ...],
    metadata: tuple[ResolvedBuildMetadata, ...],
    flatpaks: tuple[dict[str, Any], ...],
    full: bool = False,
) -> tuple[Path, Path]:
    payload = {
        "version": 1,
        "images": {
            _image_id(manifest_metadata.output_image): _image_entry(
                manifest_path,
                manifest_metadata,
            )
            for (manifest_path, _context), manifest_metadata in zip(
                manifest_contexts,
                metadata,
            )
            if full
            or not _ensure_image(
                manifest_metadata.podman,
                manifest_metadata.output_image,
                getattr(manifest_metadata, "ci_registry", ""),
            )
        },
        "flatpaks": {
            _image_id(entry["images"]["output"]): entry
            for entry in flatpaks
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.dump(payload, Dumper=_CiBuildManifestDumper, sort_keys=False).encode(
        "utf-8"
    )
    tmp = output.with_name(f"{output.name}.tmp")
    tmp.write_bytes(body)
    tmp.replace(output)
    encoded = base64.b64encode(lzma.compress(body, format=lzma.FORMAT_XZ))
    encoded_output = output.with_suffix(f"{output.suffix}.encoded")
    encoded_tmp = encoded_output.with_name(f"{encoded_output.name}.tmp")
    encoded_tmp.write_bytes(encoded)
    encoded_tmp.replace(encoded_output)
    return output, encoded_output


def _size_kib(path: Path) -> int:
    return (path.stat().st_size + 1023) // 1024


def _image_entry(
    manifest_path: Path,
    metadata: ResolvedBuildMetadata,
) -> dict[str, Any]:
    return {
        "path": str(manifest_path),
        "build": _build_entry(metadata),
    }


def _build_entry(metadata: ResolvedBuildMetadata) -> dict[str, Any]:
    build = _to_plain(
        metadata,
        omit_fields=frozenset(
            {
                "requested_packages",
                "resolved_packages",
            }
        ),
    )
    build["package_images"] = _image_plan_mapping(metadata.package_images)
    build["build_images"] = _image_plan_mapping(metadata.build_images)
    build["oci_images"] = _oci_image_plan_mapping(metadata.oci_images)
    return build


def _image_plan_mapping(plans: tuple[Any, ...]) -> dict[str, Any]:
    return {
        _image_id(plan.image): _to_plain(plan)
        for plan in plans
    }


def _oci_image_plan_mapping(plans: tuple[Any, ...]) -> dict[str, Any]:
    return {
        _oci_image_id(plan): _pinned_oci_image_plan(plan)
        for plan in plans
    }


def _oci_image_id(plan: Any) -> str:
    return f"{plan.name}-{_image_id(plan.image)}"


def _pinned_oci_image_plan(plan: Any) -> dict[str, Any]:
    entry = _to_plain(plan)
    entry["tagged_image"] = plan.image
    entry["image"] = _pinned_image(plan.image, plan.digest)
    return entry


def _pinned_image(image: str, digest: str) -> str:
    if not digest:
        return image
    if "@" in image:
        return image
    return f"{image}@{digest}"


def _image_id(image: str) -> str:
    return _image_tag(image)


def _flatpak_entry(
    manifest_path: Path,
    context: ResolvedManifestContext,
    plan: FlatpakBuildPlan,
) -> dict[str, Any]:
    return {
        "manifest": str(manifest_path),
        "source": _display_path(plan.card_path, context.root_dir),
        "app": plan.app_name,
        "block": plan.block,
        "ref": plan.app_ref,
        "branch": plan.branch,
        "arch": plan.flatpak_arch,
        "images": {
            "output": plan.output_image,
            "latest": plan.latest_image,
            "build": plan.build_image,
            "builder": plan.builder_image,
        },
        "paths": {
            "flatpak_dir": str(plan.flatpak_dir),
            "spec_build_dir": str(plan.spec_build_dir),
            "artifact_cache_dir": str(plan.artifact_cache_dir),
            "final_build_dir": str(plan.final_build_dir),
        },
        "build_env": _to_plain(plan.build_env),
        "substitution_env": _to_plain(plan.substitution_env),
        "specs": _to_plain(plan.specs),
        "spec_revisions": _to_plain(plan.spec_revisions),
        "prepare_script": plan.prepare_script,
        "builder_packages": list(plan.builder_packages),
        "rpmbuild_defines": list(plan.rpmbuild_defines),
        "metadata": _to_plain(plan.metadata),
    }


def _display_path(path: Path, root_dir: Path) -> str:
    try:
        return path.resolve().relative_to(root_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _to_plain(value: Any, *, omit_fields: frozenset[str] = frozenset()) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _to_plain(getattr(value, field.name))
            for field in fields(value)
            if field.name not in omit_fields
        }
    if isinstance(value, dict):
        return {
            key: _to_plain(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (tuple, list)):
        return [_to_plain(item) for item in value]
    if isinstance(value, str) and "\n" in value:
        return _LiteralString(value)
    return value


def _cleanup_contexts(
    manifest_contexts: tuple[tuple[Path, ResolvedManifestContext], ...],
) -> None:
    for _manifest_path, context in manifest_contexts:
        _remove_tree(context.dnf_workspace_dir, podman=context.podman)

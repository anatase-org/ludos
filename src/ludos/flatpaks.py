from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from .build import (
    _card_specs_hash,
    _create_build_output_image,
    _create_builder_image,
    _download_block_packages,
    _identifier,
    _image_exists,
    _local_image,
    _output_metadata_in_image,
    _remove_tree,
    _require_buildah,
    _resolve_packages,
    _resolve_staged_spec_builder_packages,
    _run_specs_build,
    _stage_card_specs,
    _substitute_variables,
    _unique_packages,
)
from .common import ResolvedManifestContext, resolve_manifest_context
from .logging import log
from .model import ConfigError, SpecBuild, _spec_builds_tuple


FLATPAK_BUILDER_DEPS = (
    "rpm-build",
    "rpmdevtools",
    "redhat-rpm-config",
    "flatpak-rpm-macros",
)

FLATPAK_ARCHES = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
}


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
class FlatpakConfig:
    app_id: str
    command: str
    finish_args: str = ""
    rename_icon: str = ""
    rename_desktop_file: str = ""
    rename_appdata_file: str = ""
    add_extensions: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = tuple()


@dataclass(frozen=True)
class FlatpakCard:
    version: int
    flatpak: FlatpakConfig
    specs: tuple[SpecBuild, ...]
    files: tuple[str, ...] = tuple()
    postprocess: str = ""
    source: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> "FlatpakCard":
        data = _load_mapping(path)
        allowed = {"version", "flatpak", "specs", "files", "postprocess"}
        _reject_unknown_keys(path, data, allowed)
        version = data.get("version")
        if version != 1:
            raise ConfigError(f"{path}: 'version' must be 1")
        flatpak = _flatpak_config(data, path)
        specs = _spec_builds_tuple(data, "specs", path)
        if not specs:
            raise ConfigError(f"{path}: 'specs' must contain at least one item")
        files = _string_tuple(data, "files", path)
        postprocess = _optional_string(data, "postprocess", path)
        return cls(
            version=version,
            flatpak=flatpak,
            specs=specs,
            files=files,
            postprocess=postprocess,
            source=path,
        )


def build_flatpak(
    manifest_path: Path,
    flatpak_path: Path,
    cards_dir: Path | None = None,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ccache: bool = True,
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
        return _build_flatpak_with_context(context, flatpak_path, cache_only=cache_only)
    finally:
        if context is not None:
            _remove_tree(context.dnf_workspace_dir, podman=context.podman)


def _build_flatpak_with_context(
    context: ResolvedManifestContext,
    flatpak_path: Path,
    *,
    cache_only: bool,
) -> FlatpakBuildResult:
    card_path = _flatpak_card_path(flatpak_path)
    card = FlatpakCard.from_file(card_path)
    flatpak_dir = card_path.parent
    app_name = _flatpak_name(flatpak_dir)
    block = f"flatpak-{app_name}"
    branch = _substitute_variables("f$releasever", context.manifest_env)
    flatpak_arch = _flatpak_arch(context.arch)
    app_ref = f"app/{card.flatpak.app_id}/{flatpak_arch}/{branch}"
    output_image = f"localhost/{card.flatpak.app_id}:{branch}"
    latest_image = f"localhost/{card.flatpak.app_id}:latest"

    log(f"Building flatpak {card.flatpak.app_id} for {context.distro}")
    card_env = dict(context.manifest_env)
    specs = _substitute_specs(card.specs, card_env)
    flatpak_cache_dir = context.distro_cache_dir / "flatpaks" / _identifier(app_name)
    spec_build_dir = flatpak_cache_dir / "spec-build"
    spec_scan_dir = flatpak_cache_dir / "spec-scan"
    artifact_cache_dir = context.build_artifact_cache_dir / "flatpaks" / _identifier(app_name)
    final_build_dir = context.distro_cache_dir / "build" / "flatpaks" / _identifier(app_name)
    flatpak_cache_dir.mkdir(parents=True, exist_ok=True)

    spec_hash, spec_revisions = _card_specs_hash(
        card_path,
        specs,
        card_env,
        "",
        context.spec_source_cache_dir,
        hash_expression="",
        cache_only=cache_only,
    )

    package_id_by_nevra: dict[str, tuple[str, str]] = {}
    orchestrator_dnf_base = _orchestrator_dnf_base(context)
    staged_specs = _stage_card_specs(
        card_source=card_path,
        specs=specs,
        card_env=card_env,
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
    builder_requests = _unique_packages((*FLATPAK_BUILDER_DEPS, *spec_builder_packages))
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
    if _image_exists(context.podman, builder_image):
        log(f"Reusing flatpak builder image: {builder_image}")
    elif cache_only:
        raise ConfigError(f"flatpak builder image is not cached: {builder_image}")
    else:
        builder_rpm_files = _download_block_packages(
            orchestrator_dnf_base,
            builder_packages,
            package_dir=context.package_dir,
            resolve_dependencies=True,
        )
        log(f"Creating flatpak builder image: {builder_image}")
        _create_builder_image(
            podman=context.podman,
            buildah=_require_buildah(context.buildah),
            orchestrator=context.orchestrator,
            root_dir=context.root_dir,
            repo_dir=context.repo_dir,
            dnf_cache_dir=context.dnf_cache_dir,
            dnf_persist_dir=context.dnf_persist_dir,
            dnf_log_dir=context.dnf_log_dir,
            image=builder_image,
            package_dir=context.package_dir,
            rpm_files=builder_rpm_files,
            releasever=context.releasever,
        )

    build_image = _local_image(
        context.local_prefix,
        "builds",
        f"{context.distro}-flatpak-{app_name}-{spec_hash}",
    )
    if _image_exists(context.podman, build_image):
        log(f"Reusing flatpak build output image: {build_image}")
        rpm_files, _has_files = _output_metadata_in_image(context.podman, build_image)
    elif cache_only:
        raise ConfigError(f"flatpak build output image is not cached: {build_image}")
    else:
        log(f"Running flatpak RPM build: {block} (:{build_image.rsplit(':', 1)[-1]})")
        build_output = _run_specs_build(
            podman=context.podman,
            orchestrator=builder_image,
            build_dir=spec_build_dir,
            artifact_cache_dir=artifact_cache_dir,
            ccache_dir=context.ccache_dir,
            card_name=block,
            card_source=card_path,
            card_env=card_env,
            specs=specs,
            prepare_script="",
            arch=context.arch,
            spec_source_cache_dir=context.spec_source_cache_dir,
            source_revisions=spec_revisions,
            rpmbuild_defines=rpmbuild_defines,
        )
        if not build_output.rpm_files:
            raise ConfigError(f"{card_path}: flatpak specs produced no RPMs")
        log(f"Creating flatpak build output image: {build_image}")
        _create_build_output_image(
            buildah=_require_buildah(context.buildah),
            image=build_image,
            rpm_dir=build_output.rpm_dir,
            files_dir=build_output.files_dir,
        )
        rpm_files, _has_files = _output_metadata_in_image(context.podman, build_image)

    if not rpm_files:
        raise ConfigError(f"{card_path}: flatpak build output has no RPMs")

    metadata = _flatpak_metadata(
        card.flatpak,
        branch=branch,
        flatpak_arch=flatpak_arch,
    )
    _write_flatpak_containerfile(
        final_build_dir=final_build_dir,
        flatpak_dir=flatpak_dir,
        card=card,
        build_image=build_image,
        orchestrator=context.orchestrator,
        metadata=metadata,
        app_ref=app_ref,
        branch=branch,
        flatpak_arch=flatpak_arch,
    )
    if cache_only and _image_exists(context.podman, output_image):
        log(f"Reusing flatpak image: {output_image}")
    elif cache_only:
        raise ConfigError(f"flatpak image is not cached: {output_image}")
    else:
        _run_flatpak_image_build(
            context.podman,
            final_build_dir,
            output_image,
            metadata,
        )
    subprocess.run([context.podman, "tag", output_image, latest_image], check=True)
    return FlatpakBuildResult(
        app_id=card.flatpak.app_id,
        branch=branch,
        ref=app_ref,
        image=output_image,
        latest_image=latest_image,
        build_image=build_image,
        builder_image=builder_image,
        podman=context.podman,
        orchestrator=context.orchestrator,
    )


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: file does not exist") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a YAML mapping")
    return data


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
        "finish-args",
        "rename-icon",
        "rename-desktop-file",
        "rename-appdata-file",
        "add-extensions",
    }
    _reject_unknown_keys(path, value, allowed, "flatpak")
    app_id = _required_string(value, "id", path, "flatpak")
    command = _required_string(value, "command", path, "flatpak")
    finish_args = _optional_string(value, "finish-args", path, "flatpak")
    return FlatpakConfig(
        app_id=app_id,
        command=command,
        finish_args=finish_args,
        rename_icon=_optional_string(value, "rename-icon", path, "flatpak"),
        rename_desktop_file=_optional_string(value, "rename-desktop-file", path, "flatpak"),
        rename_appdata_file=_optional_string(value, "rename-appdata-file", path, "flatpak"),
        add_extensions=_add_extensions(value, path),
    )


def _required_string(data: dict[str, Any], key: str, path: Path, prefix: str = "") -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        qualified = f"{prefix}.{key}" if prefix else key
        raise ConfigError(f"{path}: '{qualified}' must be a non-empty string")
    return value.strip()


def _optional_string(data: dict[str, Any], key: str, path: Path, prefix: str = "") -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        qualified = f"{prefix}.{key}" if prefix else key
        raise ConfigError(f"{path}: '{qualified}' must be a string")
    return value


def _string_tuple(data: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return tuple()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: '{key}' must be a list of strings")
    return tuple(value)


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


def _flatpak_name(flatpak_dir: Path) -> str:
    return flatpak_dir.resolve().name


def _substitute_specs(specs: tuple[SpecBuild, ...], env: dict[str, str]) -> tuple[SpecBuild, ...]:
    return tuple(
        replace(spec, spec=_substitute_variables(spec.spec, env))
        for spec in specs
    )


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


def _flatpak_rpmbuild_defines() -> tuple[str, ...]:
    return (
        "flatpak 1",
        "_prefix /app",
        "_exec_prefix /app",
        "_bindir /app/bin",
        "_sbindir /app/sbin",
        "_libexecdir /app/libexec",
        "_datadir /app/share",
        "_sysconfdir /app/etc",
        "_libdir /app/lib64",
        "_includedir /app/include",
        "_mandir /app/share/man",
        "_infodir /app/share/info",
    )


def _flatpak_metadata(
    config: FlatpakConfig,
    *,
    branch: str,
    flatpak_arch: str,
) -> str:
    editor = _MetadataEditor()
    editor.set("Application", "name", config.app_id)
    editor.set("Application", "runtime", f"org.anatase.Platform/{flatpak_arch}/{branch}")
    editor.set("Application", "sdk", f"org.anatase.Sdk/{flatpak_arch}/{branch}")
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
    lines = [
        f"FROM {build_image} AS rpms",
        f"FROM {orchestrator} AS build",
        "COPY --from=rpms /rpms /rpms",
        "COPY --from=rpms /files /ludos/build-files",
        "RUN <<'LUDOS_INSTALL_FLATPAK_RPMS'",
        "set -eux",
        "mkdir -p /flatpak",
        "rpm --root /flatpak --initdb",
        "rpm --root /flatpak --define '_install_langs *' -Uvh --nodeps --noscripts --notriggers /rpms/*.rpm",
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
    lines.extend(
        [
            "RUN <<'LUDOS_PRETTIFY_FLATPAK'",
            "set -eux",
            "if [ -d /flatpak/usr ]; then",
            "  usr_entries=\"$(find /flatpak/usr -mindepth 1 \\( -type f -o -type l \\) -print | sort || true)\"",
            "  if [ -n \"$usr_entries\" ]; then",
            "    echo 'warning: removing /usr entries from app flatpak payload:' >&2",
            "    printf '%s\\n' \"$usr_entries\" >&2",
            "  fi",
            "  rm -rf /flatpak/usr",
            "fi",
            "if [ ! -d /flatpak/app ]; then echo 'flatpak payload did not create /app' >&2; exit 1; fi",
            "rm -rf /out",
            "mkdir -p /out/files /out/export",
            "cp -a /flatpak/app/. /out/files/",
            *_rename_lines(card.flatpak),
            *_export_lines(card.flatpak),
            "cat > /out/metadata <<'LUDOS_FLATPAK_METADATA'",
            metadata.rstrip(),
            "LUDOS_FLATPAK_METADATA",
            "LUDOS_PRETTIFY_FLATPAK",
            "FROM scratch",
            "ARG LUDOS_FLATPAK_METADATA",
            "COPY --from=build /out/ /",
            f"LABEL org.flatpak.ref={json.dumps(app_ref)}",
            'LABEL org.flatpak.metadata="$LUDOS_FLATPAK_METADATA"',
            f"LABEL org.opencontainers.image.ref.name={json.dumps(app_ref)}",
            f"LABEL org.anatase.flatpak.branch={json.dumps(branch)}",
            f"LABEL org.anatase.flatpak.arch={json.dumps(flatpak_arch)}",
        ]
    )
    containerfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote flatpak Containerfile: {containerfile}")


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
        old_icon_name = Path(config.rename_icon).stem
        old_icon = shlex.quote(old_icon_name)
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
        "for dir in mime dbus-1 gnome-shell krunner; do",
        "  if [ -d \"/out/files/share/$dir\" ]; then",
        "    mkdir -p \"/out/export/share/$dir\"",
        "    cp -a \"/out/files/share/$dir/.\" \"/out/export/share/$dir/\"",
        "  fi",
        "done",
    ]


def _run_flatpak_image_build(
    podman: str,
    build_dir: Path,
    image: str,
    metadata: str,
) -> None:
    containerfile = build_dir / "Containerfile"
    command = [
        podman,
        "build",
        "--pull=false",
        "--build-arg",
        f"LUDOS_FLATPAK_METADATA={metadata}",
        "--tag",
        image,
        "--file",
        str(containerfile),
        str(build_dir),
    ]
    returncode = subprocess.run(command, check=False).returncode
    if returncode != 0:
        raise ConfigError(f"flatpak image build failed with exit status {returncode}")


def _hash_lines(values: tuple[str, ...]) -> str:
    import hashlib

    payload = "\n".join(sorted(values)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]

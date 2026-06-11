from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from .model import ConfigError, ManifestValidation, RepoRef, validate_manifest


@dataclass(frozen=True)
class BuildResult:
    distro: str
    requested_packages: tuple[str, ...]
    resolved_packages: tuple[str, ...]
    package_dir: Path
    package_list: Path
    repo_dir: Path
    dnf: str


def build_manifest(manifest_path: Path, cards_dir: Path | None = None) -> BuildResult:
    validation = validate_manifest(manifest_path, cards_dir)
    _raise_validation_errors(manifest_path, validation)

    root_dir = manifest_path.resolve().parent
    distro = _distro_cache_name(validation)
    cache_dir = root_dir / "cache"
    package_dir = cache_dir / "packages" / distro
    dnf_dir = cache_dir / "dnf" / distro
    repo_dir = dnf_dir / "repos"
    dnf_cache_dir = dnf_dir / "cache"
    dnf_persist_dir = dnf_dir / "persist"
    dnf_log_dir = dnf_dir / "log"
    package_list = dnf_dir / "packages.json"
    package_urls = dnf_dir / "package-urls.json"

    package_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)
    dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_persist_dir.mkdir(parents=True, exist_ok=True)
    dnf_log_dir.mkdir(parents=True, exist_ok=True)

    _render_repos(validation, repo_dir)

    requested_packages = _package_set(validation)
    if not requested_packages:
        raise ConfigError(f"{manifest_path}: no packages requested by cards")

    dnf = _find_dnf()
    dnf_base_command = _dnf_base_command(
        dnf=dnf,
        repo_dir=repo_dir,
        dnf_cache_dir=dnf_cache_dir,
        dnf_persist_dir=dnf_persist_dir,
        dnf_log_dir=dnf_log_dir,
    )
    package_urls_resolved = _resolve_package_urls(
        dnf_base_command=dnf_base_command,
        validation=validation,
        root_dir=root_dir,
        packages=requested_packages,
    )
    resolved_packages = tuple(_package_spec_from_url(url) for url in package_urls_resolved)
    _write_json_array(package_list, resolved_packages)
    _write_json_array(package_urls, package_urls_resolved)

    command = [
        *dnf_base_command,
        "download",
        *_download_arch_args(validation),
        "--destdir=" + str(package_dir),
        *resolved_packages,
    ]
    subprocess.run(command, cwd=root_dir / "repos", check=True)

    return BuildResult(
        distro=distro,
        requested_packages=requested_packages,
        resolved_packages=resolved_packages,
        package_dir=package_dir,
        package_list=package_list,
        repo_dir=repo_dir,
        dnf=dnf,
    )


def _dnf_base_command(
    dnf: str,
    repo_dir: Path,
    dnf_cache_dir: Path,
    dnf_persist_dir: Path,
    dnf_log_dir: Path,
) -> list[str]:
    return [
        dnf,
        "--setopt=reposdir=" + str(repo_dir),
        "--setopt=cachedir=" + str(dnf_cache_dir),
        "--setopt=persistdir=" + str(dnf_persist_dir),
        "--setopt=logdir=" + str(dnf_log_dir),
        "--disable-repo=*",
        "--enable-repo=*",
    ]


def _resolve_package_urls(
    dnf_base_command: list[str],
    validation: ManifestValidation,
    root_dir: Path,
    packages: tuple[str, ...],
) -> tuple[str, ...]:
    command = [
        *dnf_base_command,
        "--refresh",
        "download",
        "--resolve",
        "--alldeps",
        "--url",
        *_download_arch_args(validation),
        *packages,
    ]
    result = subprocess.run(
        command,
        cwd=root_dir / "repos",
        check=True,
        text=True,
        capture_output=True,
    )
    urls = tuple(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().endswith(".rpm")
    )
    if not urls:
        raise ConfigError("dnf did not resolve any package URLs")
    return urls


def _raise_validation_errors(
    manifest_path: Path, validation: ManifestValidation
) -> None:
    if validation.missing_repos:
        missing = ", ".join(validation.missing_repos)
        raise ConfigError(f"{manifest_path}: missing repository definitions: {missing}")
    if validation.missing_cards:
        missing = ", ".join(validation.missing_cards)
        raise ConfigError(f"{manifest_path}: missing card definitions: {missing}")


def _render_repos(validation: ManifestValidation, repo_dir: Path) -> None:
    for existing in repo_dir.glob("*.repo"):
        existing.unlink()

    for repo in validation.repos:
        content = repo.source.read_text(encoding="utf-8")
        variables = _repo_variables(repo.ref, validation.manifest.env)
        rendered = _substitute_variables(content, variables)
        rendered = _append_priority(rendered, repo.ref.priority)
        (repo_dir / repo.source.name).write_text(rendered, encoding="utf-8")


def _repo_variables(repo: RepoRef, env: dict[str, str | int]) -> dict[str, str]:
    variables = {key: str(value) for key, value in env.items()}
    for key, value in repo.vars.items():
        variables[key] = _substitute_variables(value, variables)
    return variables


def _distro_cache_name(validation: ManifestValidation) -> str:
    distro = _substitute_variables(
        validation.manifest.distro,
        {key: str(value) for key, value in validation.manifest.env.items()},
    )
    if "/" in distro or distro in ("", ".", ".."):
        raise ConfigError(f"invalid distro cache name '{distro}'")
    return distro


def _substitute_variables(value: str, variables: dict[str, str]) -> str:
    for key, replacement in variables.items():
        value = value.replace(f"${key}", replacement)
    return value


def _append_priority(repo_content: str, priority: int) -> str:
    lines = repo_content.rstrip().splitlines()
    lines.append(f"priority={priority}")
    return "\n".join(lines) + "\n"


def _package_set(validation: ManifestValidation) -> tuple[str, ...]:
    packages = []
    seen = set()
    for card in validation.cards:
        for package in card.packages:
            if package in seen:
                continue
            seen.add(package)
            packages.append(package)
    return tuple(packages)


def _download_arch_args(validation: ManifestValidation) -> tuple[str, ...]:
    arch = validation.manifest.env["arch"]
    return (f"--arch={arch}", "--arch=noarch")


def _package_spec_from_url(url: str) -> str:
    filename = Path(unquote(urlparse(url).path)).name
    if not filename.endswith(".rpm"):
        raise ConfigError(f"resolved package URL does not end with .rpm: {url}")
    return filename[:-4]


def _write_json_array(path: Path, values: tuple[str, ...]) -> None:
    path.write_text(json.dumps(list(values), indent=2) + "\n", encoding="utf-8")


def _find_dnf() -> str:
    for command in ("dnf5", "dnf"):
        path = shutil.which(command)
        if path:
            return path
    raise ConfigError("dnf5 or dnf must be installed to build")

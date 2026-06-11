from __future__ import annotations

import json
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
    package_dir: Path
    package_list: Path
    repo_dir: Path
    podman: str


def build_manifest(manifest_path: Path, cards_dir: Path | None = None) -> BuildResult:
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
    distro = _cache_name(
        _substitute_variables(validation.manifest.distro, manifest_env),
        "distro",
    )
    bootstrap = _substitute_variables(validation.manifest.bootstrap, manifest_env)
    output_image = f"localhost/ludos/{image}:{distro}"

    cache_dir = root_dir / "cache"
    package_dir = cache_dir / "packages" / distro
    dnf_dir = cache_dir / "dnf" / distro
    build_dir = cache_dir / "build" / distro / image
    repo_dir = dnf_dir / "repos"
    dnf_cache_dir = dnf_dir / "cache"
    dnf_persist_dir = dnf_dir / "persist"
    dnf_log_dir = dnf_dir / "log"
    package_list = dnf_dir / f"{image}-packages.json"

    package_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)
    dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_persist_dir.mkdir(parents=True, exist_ok=True)
    dnf_log_dir.mkdir(parents=True, exist_ok=True)

    for existing in repo_dir.glob("*.repo"):
        existing.unlink()

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
        (repo_dir / repo.source.name).write_text(
            "\n".join(repo_lines) + "\n",
            encoding="utf-8",
        )

    requested_packages = []
    seen_packages = set()
    for card in validation.cards:
        for package in card.packages:
            if package in seen_packages:
                continue
            seen_packages.add(package)
            requested_packages.append(package)
    requested_packages = tuple(requested_packages)
    if not requested_packages:
        raise ConfigError(f"{manifest_path}: no packages requested by cards")

    podman = shutil.which("podman")
    if not podman:
        raise ConfigError("podman must be installed to build")

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
        "--workdir",
        "/workspace/repos",
        bootstrap,
        "dnf5",
    ]

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
            "--refresh",
            "--installroot=/ludos/resolve-root",
            f"--releasever={validation.manifest.env['releasever']}",
            "install",
            *requested_packages,
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    resolved_package_list = []
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
        resolved_package_list.append(f"{package}-{version}.{arch}")
    resolved_packages = tuple(resolved_package_list)
    if not resolved_packages:
        output = transaction_preview.stdout + transaction_preview.stderr
        detail = "\n".join(output.splitlines()[-20:])
        raise ConfigError(f"dnf did not resolve any packages:\n{detail}")

    package_list.write_text(
        json.dumps(list(resolved_packages), indent=2) + "\n",
        encoding="utf-8",
    )

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
            *resolved_packages,
        ],
        check=True,
        text=True,
    )

    label_lines = "".join(
        f"LABEL {json.dumps(key)}={json.dumps(value)}\n"
        for key, value in validation.manifest.labels.items()
    )
    package_lines = "".join(
        f"      {shlex.quote(package)} \\\n" for package in resolved_packages
    )
    containerfile = build_dir / "Containerfile"
    containerfile.write_text(
        f"""FROM {bootstrap} AS install
WORKDIR /workspace/repos
RUN mkdir -p /target && \\
    dnf5 -y \\
      --installroot=/target \\
      --releasever={validation.manifest.env["releasever"]} \\
      --setopt=reposdir=/ludos/dnf/repos \\
      --setopt=cachedir=/ludos/dnf/cache \\
      --setopt=persistdir=/ludos/dnf/persist \\
      --setopt=logdir=/ludos/dnf/log \\
      --setopt=install_weak_deps=False \\
      --disable-repo='*' \\
      --enable-repo='*' \\
      install \\
{package_lines}    && \\
    dnf5 -y --installroot=/target clean all && \\
    rm -rf /target/var/cache/dnf /target/var/log/dnf* \\
      /target/etc/machine-id /target/var/lib/dbus/machine-id

FROM scratch
COPY --from=install /target /
{label_lines}""",
        encoding="utf-8",
    )

    subprocess.run(
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
        check=True,
    )

    return BuildResult(
        image=image,
        distro=distro,
        bootstrap=bootstrap,
        output_image=output_image,
        requested_packages=requested_packages,
        resolved_packages=resolved_packages,
        package_dir=package_dir,
        package_list=package_list,
        repo_dir=repo_dir,
        podman=str(podman),
    )


def _cache_name(value: str, description: str) -> str:
    if "/" in value or value in ("", ".", ".."):
        raise ConfigError(f"invalid {description} cache name '{value}'")
    return value


def _substitute_variables(value: str, variables: dict[str, str]) -> str:
    for key, replacement in variables.items():
        value = value.replace(f"${key}", replacement)
    return value

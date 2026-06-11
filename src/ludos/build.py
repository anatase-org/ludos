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
    package_blocks: tuple[tuple[str, tuple[str, ...]], ...]
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

    card_entries = []
    used_card_names = set()
    for insertion_order, card in enumerate(validation.cards):
        card_name = card.source.stem if card.source else "card"
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
    postprocess_blocks = []
    for _priority, _insertion_order, card_name, card in card_entries:
        card_names.append(card_name)
        card_packages = []
        for package in card.packages:
            card_packages.append(package)
            requested_packages.append(package)
        card_requests.append(tuple(card_packages))
        if card.postprocess.strip():
            postprocess_blocks.append((card_name, card.postprocess.rstrip()))
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
                "--refresh",
                "--installroot=/ludos/resolve-root",
                f"--releasever={validation.manifest.env['releasever']}",
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

    resolved_package_list = list(common_packages)
    for card_name, card_resolution in zip(card_names, card_resolutions):
        card_packages = tuple(
            package for package in card_resolution if package not in common_package_set
        )
        if not card_packages:
            continue
        package_blocks.append((card_name, card_packages))
        resolved_package_list.extend(card_packages)
    package_blocks = tuple(package_blocks)
    resolved_packages = tuple(resolved_package_list)
    if not resolved_packages:
        raise ConfigError("dnf did not resolve any packages")

    package_list.write_text(
        json.dumps(
            {
                "packages": list(resolved_packages),
                "blocks": {
                    block_name: list(block_packages)
                    for block_name, block_packages in package_blocks
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    for block_name, block_packages in package_blocks:
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

    label_lines = "".join(
        f"LABEL {json.dumps(key)}={json.dumps(value)}\n"
        for key, value in validation.manifest.labels.items()
    )
    install_steps = []
    for block_name, block_packages in package_blocks:
        package_lines = "".join(
            f"      {shlex.quote(package)} \\\n" for package in block_packages
        )
        install_steps.append(
            f"""# Install {block_name} packages.
RUN dnf5 -y \\
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
    rm -rf /target/var/cache/dnf /target/var/log/dnf*
"""
        )
    install_step_lines = "\n".join(install_steps)
    postprocess_steps = []
    for block_name, postprocess in postprocess_blocks:
        postprocess_steps.append(
            f"""# Postprocess: {block_name}
RUN /bin/sh <<'LUDOS_POSTPROCESS_{block_name}'
set -e
{postprocess}
LUDOS_POSTPROCESS_{block_name}
"""
        )
    postprocess_step_lines = "\n".join(postprocess_steps)
    containerfile = build_dir / "Containerfile"
    containerfile.write_text(
        f"""FROM {bootstrap} AS install
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
        package_blocks=package_blocks,
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

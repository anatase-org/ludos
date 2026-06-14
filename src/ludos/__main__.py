from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from .build import build_manifest
from .cleanup import cleanup_local_images
from .logging import LOGO_STR, configure_tracebacks, error, log
from .model import ConfigError, validate_manifest
from .update import update_targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ludos",
        description="Build bootc OS images from a Ludos YAML manifest.",
    )
    subcommands = parser.add_subparsers(dest="command")

    build = subcommands.add_parser("build", help="Build a Ludos manifest.")
    build.add_argument("manifest", type=Path, help="Path to a Ludos YAML file.")
    build.add_argument(
        "--cards-dir",
        type=Path,
        default=None,
        help="Directory containing card YAML files. Defaults to ./cards next to the manifest.",
    )
    build.add_argument(
        "--cache",
        action="store_true",
        help="Only use cached repository and card images. Fail if any are missing.",
    )
    build.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for build, dnf, and package caches. Defaults to ./cache next to the manifest.",
    )
    build.add_argument(
        "--version",
        default=None,
        help="Repository/package cache version to load. Defaults to the current YYYYMMDD and creates missing cache images.",
    )
    build.set_defaults(func=build_command)

    validate = subcommands.add_parser("validate", help="Validate Ludos config files.")
    validate.add_argument("manifest", type=Path, help="Path to a Ludos YAML file.")
    validate.add_argument(
        "--cards-dir",
        type=Path,
        default=None,
        help="Directory containing card YAML files. Defaults to ./cards next to the manifest.",
    )
    validate.set_defaults(func=validate_command)

    update = subcommands.add_parser(
        "update",
        help="Update upstream-backed card sources.",
    )
    update.add_argument(
        "targets",
        nargs="+",
        type=Path,
        help="Manifest or card YAML files to update.",
    )
    update.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for update caches. Defaults to ./cache.",
    )
    update.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and merge in the cache without copying files back or updating locks.",
    )
    update.set_defaults(func=update_command)

    cleanup = subcommands.add_parser(
        "cleanup",
        help="Remove stale local Ludos cache images.",
    )
    cleanup.add_argument(
        "--version",
        default=None,
        help="Cache version to keep. Defaults to the current YYYYMMDD.",
    )
    cleanup.add_argument(
        "--local-prefix",
        default="",
        help="Local image prefix to clean. Defaults to the unprefixed local cache.",
    )
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="Show stale images without removing them.",
    )
    cleanup.add_argument(
        "manifests",
        nargs="*",
        type=Path,
        help="Optional manifests whose final image tags should also be cleaned.",
    )
    cleanup.set_defaults(func=cleanup_command)

    return parser


def show_logo(_args: argparse.Namespace) -> int:
    log(LOGO_STR)
    log("Starting Ludos...")
    return 0


def validate_command(args: argparse.Namespace) -> int:
    result = validate_manifest(args.manifest, args.cards_dir)
    if result.missing_bootstrap:
        raise ConfigError(
            f"{args.manifest}: missing bootstrap card: {result.missing_bootstrap}"
        )
    if result.missing_repos:
        missing = ", ".join(result.missing_repos)
        raise ConfigError(f"{args.manifest}: missing repository definitions: {missing}")
    if result.missing_cards:
        missing = ", ".join(result.missing_cards)
        raise ConfigError(f"{args.manifest}: missing card definitions: {missing}")

    log(
        f"Manifest is valid: bootstrap, {len(result.repos)} repos, "
        f"{len(result.cards)} cards"
    )
    return 0


def build_command(args: argparse.Namespace) -> int:
    show_logo(args)

    result = build_manifest(
        args.manifest,
        args.cards_dir,
        args.cache_dir,
        args.version,
        args.cache,
    )
    log(
        f"Built {result.output_image} for {result.image} on {result.distro} "
        f"with {Path(result.podman).name} using {result.orchestrator}"
    )
    blocks = ", ".join(
        _package_block_summary(block_name, block_packages, result.build_blocks)
        for block_name, block_packages in result.package_blocks
    )
    log(f"Package blocks: {blocks}")
    return 0


def _package_block_summary(
    block_name: str,
    block_packages: tuple[str, ...],
    build_blocks: tuple[str, ...],
) -> str:
    package_count = len(block_packages)
    if block_name in build_blocks:
        if package_count:
            return f"{block_name}: {package_count} + build"
        return f"{block_name}: build"
    return f"{block_name}: {package_count}"


def cleanup_command(args: argparse.Namespace) -> int:
    return cleanup_local_images(
        version=args.version,
        local_prefix=args.local_prefix,
        manifests=tuple(args.manifests),
        dry_run=args.dry_run,
    )


def update_command(args: argparse.Namespace) -> int:
    return update_targets(tuple(args.targets), args.cache_dir, args.dry_run)


def main() -> int:
    configure_tracebacks()
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        error("User requested to exit...")
        return 130
    except ConfigError as exc:
        error(exc)
        return 1
    except subprocess.CalledProcessError as exc:
        error(f"command failed with exit status {exc.returncode}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from .bootc import ostree_import
from .build import build_manifest
from .cleanup import cleanup_local_images
from .logging import LOGO_STR, configure_tracebacks, error, log
from .model import ConfigError, validate_manifest
from .contrib.package import package_target
from .contrib.patchwork import patch_target
from .contrib.update import update_targets


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
    build.add_argument(
        "--ci",
        action="store_true",
        help="Build the final image with combined package and postprocess layers.",
    )
    build.add_argument(
        "--no-ccache",
        action="store_true",
        help="Do not mount or enable shared ccache/sccache directories for builder runs.",
    )
    build.add_argument(
        "--card",
        default=None,
        help="Build only the selected card output, using the same card path format as the manifest.",
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
        "--patchwork-dir",
        type=Path,
        default=None,
        help="Directory for update patchwork checkouts. Defaults to ./patchwork.",
    )
    update.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and merge in the cache without copying files back or updating locks.",
    )
    update.set_defaults(func=update_command)

    patch = subcommands.add_parser(
        "patch",
        help="Work with git-backed patchwork branches.",
    )
    patch.add_argument(
        "--patchwork-dir",
        type=Path,
        default=None,
        help="Directory for patchwork checkouts. Defaults to ./patchwork.",
    )
    patch_subcommands = patch.add_subparsers(dest="patch_action", required=True)
    patch_checkout = patch_subcommands.add_parser(
        "checkout",
        help="Recreate the ludos patchwork branch from a saved patch file.",
    )
    patch_checkout.add_argument("target", help="Patch target as <card>:<spec>.")
    patch_checkout.set_defaults(func=patch_command)
    patch_apply = patch_subcommands.add_parser(
        "apply",
        help="Update the saved patch file from the ludos patchwork branch.",
    )
    patch_apply.add_argument("target", help="Patch target as <card>:<spec>.")
    patch_apply.set_defaults(func=patch_command)
    patch_init = patch_subcommands.add_parser(
        "init",
        help="Initialize git patchwork for a spec.",
    )
    patch_init.add_argument("target", help="Patch target as <card>:<spec>.")
    patch_init.add_argument("url", help="Upstream git URL for patchwork.")
    patch_init.add_argument(
        "--file",
        default="overrides.patch",
        help="Patch file name to create. Defaults to overrides.patch.",
    )
    patch_init.add_argument(
        "--ref",
        default="${spec:Version}",
        help="Git ref for the patch base. Defaults to ${spec:Version}.",
    )
    patch_init.add_argument(
        "--name",
        default="",
        help="Patchwork repo name. Defaults to the derived card/spec source name.",
    )
    patch_init.set_defaults(func=patch_command)

    package = subcommands.add_parser(
        "package",
        help="Work with dist-git package repos.",
    )
    package_subcommands = package.add_subparsers(
        dest="package_action",
        required=True,
    )
    package_fork = package_subcommands.add_parser(
        "fork",
        help="Fork a dist-git package repo into a card source location.",
    )
    package_fork.add_argument("git_url", help="Package dist-git URL to clone.")
    package_fork.add_argument(
        "location",
        type=Path,
        help="Destination directory for copied package files.",
    )
    package_fork.add_argument(
        "--card",
        type=Path,
        default=None,
        help="Card YAML file to append. Defaults to <location>/card.yml.",
    )
    package_fork.add_argument(
        "--subdir",
        default="",
        help="Repository subdirectory to copy and track.",
    )
    package_fork.set_defaults(func=package_command)

    bootc = subcommands.add_parser(
        "bootc",
        help="Work with bootc image artifacts.",
    )
    bootc_subcommands = bootc.add_subparsers(dest="bootc_action", required=True)
    ostree_import_parser = bootc_subcommands.add_parser(
        "ostree-import",
        help="Import a local container image root into an OSTree repo.",
    )
    ostree_import_parser.add_argument(
        "ref",
        help="Local container image reference to import.",
    )
    ostree_import_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory containing the OSTree repo. Defaults to ./cache.",
    )
    ostree_import_parser.add_argument(
        "--orchestrator",
        default=None,
        help="Local orchestrator image to run. Defaults to the imported ref.",
    )
    ostree_import_parser.add_argument(
        "--ostree-ref",
        default="master",
        help="OSTree ref to write in the cache repo. Defaults to master.",
    )
    ostree_import_parser.add_argument(
        "--no-process",
        action="store_true",
        help="Import the container root as-is without OSTree rootfs postprocessing.",
    )
    ostree_import_parser.set_defaults(func=bootc_command)

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
        cards_dir=args.cards_dir,
        cache_dir=args.cache_dir,
        cache_version=args.version,
        cache_only=args.cache,
        ci=args.ci,
        ccache=not args.no_ccache,
        card=args.card,
    )
    if args.card:
        card_name = result.build_blocks[0] if result.build_blocks else str(args.card)
        if result.build_images:
            log(f"Built card {card_name}: {result.build_images[0]}")
        else:
            log(f"Built card {card_name}: no build output image")
        return 0
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
    return update_targets(
        tuple(args.targets),
        cache_dir=args.cache_dir,
        patchwork_dir=args.patchwork_dir,
        dry_run=args.dry_run,
    )


def patch_command(args: argparse.Namespace) -> int:
    return patch_target(
        args.patch_action,
        args.target,
        patchwork_dir=args.patchwork_dir,
        url=getattr(args, "url", ""),
        file=getattr(args, "file", "overrides.patch"),
        ref=getattr(args, "ref", "${spec:Version}"),
        name=getattr(args, "name", ""),
    )


def package_command(args: argparse.Namespace) -> int:
    return package_target(
        args.package_action,
        args.git_url,
        args.location,
        card=args.card,
        subdir=args.subdir,
    )


def bootc_command(args: argparse.Namespace) -> int:
    if args.bootc_action == "ostree-import":
        return ostree_import(
            args.ref,
            cache_dir=args.cache_dir,
            orchestrator=args.orchestrator,
            ostree_ref=args.ostree_ref,
            process=not args.no_process,
        )
    raise ConfigError(f"unknown bootc action: {args.bootc_action}")


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

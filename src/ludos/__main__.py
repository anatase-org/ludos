from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .bootc import DEFAULT_OCI_WRITERS, bootc_create, ostree_import
from .build import build_manifest
from .cleanup import cleanup_local_images
from .flatpaks import build_flatpak, build_flatpaks
from .installer import bootc_installer
from .logging import LOGO_STR, configure_logging, configure_tracebacks, error, log
from .model import ConfigError, Project, validate_manifest
from .contrib.package import package_target
from .contrib.patchwork import patch_target
from .contrib.update import update_targets
from .upload.file import delete_file, upload_file
from .upload.flatpaks import tree_shake_flatpaks, upload_flatpaks
from .upload.registry import (
    delete_oci_tags,
    list_oci_tags,
    prune_oci_tags,
    registry_init,
    tree_shake_oci,
    upload_oci,
)


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
    target = build.add_mutually_exclusive_group()
    target.add_argument(
        "--card",
        default=None,
        help="Build only the selected card output, using the same card path format as the manifest.",
    )
    target.add_argument(
        "--flatpak",
        type=Path,
        default=None,
        help="Build a flatpak app from the selected flatpak directory or card YAML.",
    )
    target.add_argument(
        "--flatpaks",
        action="store_true",
        help="Build every flatpak app declared by the manifest's flatpaks list.",
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
        "--card",
        default=None,
        help="Update only the selected card from a manifest.",
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
    update.add_argument(
        "--assume-yes",
        action="store_true",
        help="Apply discovered updates without prompting.",
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

    registry = subcommands.add_parser(
        "registry",
        help="Work with static registry artifacts.",
    )
    registry_subcommands = registry.add_subparsers(
        dest="registry_action",
        required=True,
    )
    registry_init_parser = registry_subcommands.add_parser(
        "init",
        help="Initialize S3 objects required for a static OCI registry.",
    )
    registry_init_parser.set_defaults(func=registry_command)

    registry_file = registry_subcommands.add_parser(
        "file",
        help="Work with registry-hosted files.",
    )
    registry_file_subcommands = registry_file.add_subparsers(
        dest="registry_file_action",
        required=True,
    )
    registry_file_upload = registry_file_subcommands.add_parser(
        "upload",
        help="Upload a file to S3 and update SHA256SUMS.",
    )
    registry_file_upload.add_argument(
        "path",
        type=Path,
        help="Path to the local file to upload.",
    )
    registry_file_upload.add_argument(
        "output_path",
        help="S3 object path to write.",
    )
    registry_file_upload.add_argument(
        "download_name",
        nargs="?",
        help="Filename to publish in SHA256SUMS and Content-Disposition.",
    )
    registry_file_upload.set_defaults(func=registry_command)

    registry_file_delete = registry_file_subcommands.add_parser(
        "delete",
        help="Delete a file from S3.",
    )
    registry_file_delete.add_argument(
        "output_path",
        help="S3 object path to delete.",
    )
    registry_file_delete.set_defaults(func=registry_command)

    registry_flatpak = registry_subcommands.add_parser(
        "flatpak",
        help="Work with registry-hosted flatpaks.",
    )
    registry_flatpak_subcommands = registry_flatpak.add_subparsers(
        dest="registry_flatpak_action",
        required=True,
    )
    registry_flatpak_upload = registry_flatpak_subcommands.add_parser(
        "upload",
        help="Export and upload flatpak OCI images to S3.",
    )
    registry_flatpak_upload.add_argument(
        "manifest",
        type=Path,
        help="Path to a Ludos YAML manifest.",
    )
    registry_flatpak_upload.add_argument(
        "--flatpak",
        action="append",
        type=Path,
        default=None,
        dest="flatpaks",
        help="Flatpak directory or card YAML to upload. May be specified more than once.",
    )
    registry_flatpak_upload.add_argument(
        "--build",
        action="store_true",
        help="Build selected flatpaks before uploading.",
    )
    registry_flatpak_upload.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for flatpak export caches. Defaults to ./cache next to the manifest.",
    )
    registry_flatpak_upload.set_defaults(func=registry_command)
    registry_flatpak_tree_shake = registry_flatpak_subcommands.add_parser(
        "tree-shake",
        help="Delete OCI blobs not referenced by flatpak repository manifests.",
    )
    registry_flatpak_tree_shake.add_argument(
        "manifest",
        type=Path,
        help="Path to a Ludos YAML manifest.",
    )
    registry_flatpak_tree_shake.add_argument(
        "--flatpak",
        action="append",
        type=Path,
        default=None,
        dest="flatpaks",
        help="Flatpak directory or card YAML to tree-shake. May be specified more than once.",
    )
    registry_flatpak_tree_shake.add_argument(
        "--dry-run",
        action="store_true",
        help="Print blobs that would be deleted without deleting them.",
    )
    registry_flatpak_tree_shake.set_defaults(func=registry_command)

    registry_oci = registry_subcommands.add_parser(
        "oci",
        help="Work with static OCI repositories.",
    )
    registry_oci_subcommands = registry_oci.add_subparsers(
        dest="registry_oci_action",
        required=True,
    )
    registry_oci_upload = registry_oci_subcommands.add_parser(
        "upload",
        help="Upload a local OCI layout to S3 as a static OCI repository.",
    )
    registry_oci_upload.add_argument(
        "local_oci_path",
        type=Path,
        help="Path to a local OCI layout directory.",
    )
    registry_oci_upload.add_argument(
        "ref",
        help="OCI repository path within the registry, without the registry host.",
    )
    registry_oci_upload.add_argument(
        "--tag",
        action="append",
        required=True,
        dest="tags",
        help="Tag to publish. May be specified more than once.",
    )
    registry_oci_upload.set_defaults(func=registry_command)
    registry_oci_list = registry_oci_subcommands.add_parser(
        "list",
        help="List OCI tags in S3.",
    )
    registry_oci_list.add_argument(
        "ref",
        help="OCI repository path within the registry, without the registry host.",
    )
    registry_oci_list.set_defaults(func=registry_command)
    registry_oci_delete = registry_oci_subcommands.add_parser(
        "delete",
        help="Delete OCI tag manifests from S3.",
    )
    registry_oci_delete.add_argument(
        "ref",
        help="OCI repository path within the registry, without the registry host.",
    )
    registry_oci_delete.add_argument(
        "--tag",
        action="append",
        required=True,
        dest="tags",
        help="Tag to delete. May be specified more than once.",
    )
    registry_oci_delete.add_argument(
        "--dry-run",
        action="store_true",
        help="Print tag manifests that would be deleted without deleting them.",
    )
    registry_oci_delete.set_defaults(func=registry_command)
    registry_oci_prune = registry_oci_subcommands.add_parser(
        "prune",
        help="Prune OCI tag manifests from S3.",
    )
    registry_oci_prune.add_argument(
        "ref",
        help="OCI repository path within the registry, without the registry host.",
    )
    registry_oci_prune.add_argument(
        "--pattern",
        required=True,
        help="Glob pattern for tag names to prune.",
    )
    registry_oci_prune.add_argument(
        "--rule",
        choices=("descending",),
        default="descending",
        help="Ordering rule for tags before keeping --number entries.",
    )
    registry_oci_prune.add_argument(
        "--number",
        type=int,
        default=3,
        help="Number of matching tags to keep.",
    )
    registry_oci_prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Print tag manifests that would be deleted without deleting them.",
    )
    registry_oci_prune.set_defaults(func=registry_command)
    registry_oci_tree_shake = registry_oci_subcommands.add_parser(
        "tree-shake",
        help="Delete OCI blobs not referenced by repository manifests.",
    )
    registry_oci_tree_shake.add_argument(
        "ref",
        help="OCI repository path within the registry, without the registry host.",
    )
    registry_oci_tree_shake.add_argument(
        "--dry-run",
        action="store_true",
        help="Print blobs that would be deleted without deleting them.",
    )
    registry_oci_tree_shake.set_defaults(func=registry_command)

    bootc = subcommands.add_parser(
        "bootc",
        help="Work with bootc image artifacts.",
    )
    bootc_subcommands = bootc.add_subparsers(dest="bootc_action", required=True)
    create_parser = bootc_subcommands.add_parser(
        "create",
        help="Build manifests and export rechunked bootc OCI images.",
    )
    create_parser.add_argument(
        "manifests",
        nargs="+",
        type=Path,
        help="Paths to Ludos YAML manifests.",
    )
    create_parser.add_argument(
        "--chunks",
        type=Path,
        default=None,
        help="Path to chunks YAML. Defaults to chunks.yml next to the first manifest.",
    )
    create_parser.add_argument(
        "--cards-dir",
        type=Path,
        default=None,
        help="Directory containing card YAML files. Defaults to ./cards next to the manifest.",
    )
    create_parser.add_argument(
        "--cache",
        action="store_true",
        help="Only use cached repository and card images. Fail if any are missing.",
    )
    create_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for build, dnf, package, OSTree, and OCI caches. Defaults to ./cache next to the first manifest.",
    )
    create_parser.add_argument(
        "--version",
        default=None,
        help="Repository/package cache version to load. Defaults to the current YYYYMMDD and creates missing cache images.",
    )
    create_parser.add_argument(
        "--ci",
        action="store_true",
        help="Build the final image with combined package and postprocess layers.",
    )
    create_parser.add_argument(
        "--writers",
        type=int,
        default=DEFAULT_OCI_WRITERS,
        help="Number of parallel OCI layer writers for bootc encapsulate. Defaults to 4.",
    )
    create_parser.add_argument(
        "--no-ccache",
        action="store_true",
        help="Do not mount or enable shared ccache/sccache directories for builder runs.",
    )
    create_parser.set_defaults(func=bootc_command)

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

    installer_parser = bootc_subcommands.add_parser(
        "installer",
        help="Create a bootc installer ISO from an ostree-container image ref.",
    )
    installer_parser.add_argument(
        "manifest",
        type=Path,
        help="Path to a Ludos YAML manifest.",
    )
    installer_parser.add_argument(
        "ref",
        help="OSTree container image ref to import into the installer ISO.",
    )
    installer_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Artifact directory to create. Defaults to ./cache/iso/<safe-ref-name>.",
    )
    installer_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory used only for the default output location.",
    )
    installer_parser.add_argument(
        "--orchestrator",
        default=None,
        help="Container image used to run installer tooling. Defaults to the image ref.",
    )
    installer_parser.add_argument(
        "--scratch",
        action="store_true",
        help="Use a faster scratch EROFS profile for installer root creation.",
    )
    installer_parser.set_defaults(func=bootc_command)

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
    project = getattr(_args, "project", None)
    if project is not None:
        log(f"Using project: {project.name} at {project.root}")
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
    if result.missing_flatpaks:
        missing = ", ".join(result.missing_flatpaks)
        raise ConfigError(f"{args.manifest}: missing flatpak definitions: {missing}")

    log(
        f"Manifest is valid: bootstrap, {len(result.repos)} repos, "
        f"{len(result.cards)} cards, {len(result.manifest.flatpaks)} flatpaks"
    )
    return 0


def build_command(args: argparse.Namespace) -> int:
    show_logo(args)

    if args.flatpak is not None:
        result = build_flatpak(
            args.manifest,
            args.flatpak,
            cards_dir=args.cards_dir,
            cache_dir=args.cache_dir,
            cache_version=args.version,
            cache_only=args.cache,
            ccache=not args.no_ccache,
        )
        _log_flatpak_result(result)
        return 0

    if args.flatpaks:
        results = build_flatpaks(
            args.manifest,
            cards_dir=args.cards_dir,
            cache_dir=args.cache_dir,
            cache_version=args.version,
            cache_only=args.cache,
            ccache=not args.no_ccache,
        )
        for result in results:
            _log_flatpak_result(result)
        return 0

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


def _log_flatpak_result(result: object) -> None:
    latest_image = getattr(result, "latest_image", "")
    suffix = (
        f" (latest: {latest_image})"
        if latest_image and latest_image != result.image
        else ""
    )
    log(f"Built flatpak {result.ref}: {result.image}{suffix}")


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
        assume_yes=args.assume_yes,
        card=args.card,
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


def registry_command(args: argparse.Namespace) -> int:
    if args.registry_action == "init":
        return registry_init()
    if args.registry_action == "file":
        if args.registry_file_action == "upload":
            return upload_file(args.path, args.output_path, args.download_name)
        if args.registry_file_action == "delete":
            return delete_file(args.output_path)
        raise ConfigError(f"unknown registry file action: {args.registry_file_action}")
    if args.registry_action == "flatpak":
        if args.registry_flatpak_action == "upload":
            return upload_flatpaks(
                args.manifest,
                tuple(args.flatpaks or ()),
                build=args.build,
                cache_dir=args.cache_dir,
            )
        if args.registry_flatpak_action == "tree-shake":
            return tree_shake_flatpaks(
                args.manifest,
                tuple(args.flatpaks or ()),
                dry_run=args.dry_run,
            )
        raise ConfigError(
            f"unknown registry flatpak action: {args.registry_flatpak_action}"
        )
    if args.registry_action == "oci":
        if args.registry_oci_action == "upload":
            return upload_oci(args.local_oci_path, args.ref, tuple(args.tags))
        if args.registry_oci_action == "list":
            return list_oci_tags(args.ref)
        if args.registry_oci_action == "delete":
            return delete_oci_tags(args.ref, tuple(args.tags), dry_run=args.dry_run)
        if args.registry_oci_action == "prune":
            return prune_oci_tags(
                args.ref,
                args.pattern,
                rule=args.rule,
                number=args.number,
                dry_run=args.dry_run,
            )
        if args.registry_oci_action == "tree-shake":
            return tree_shake_oci(args.ref, dry_run=args.dry_run)
        raise ConfigError(f"unknown registry oci action: {args.registry_oci_action}")
    raise ConfigError(f"unknown registry action: {args.registry_action}")


def bootc_command(args: argparse.Namespace) -> int:
    if args.bootc_action == "create":
        return bootc_create(
            tuple(args.manifests),
            chunks=args.chunks,
            cards_dir=args.cards_dir,
            cache_dir=args.cache_dir,
            cache_version=args.version,
            cache_only=args.cache,
            ci=args.ci,
            ccache=not args.no_ccache,
            writers=args.writers,
        )
    if args.bootc_action == "ostree-import":
        return ostree_import(
            args.ref,
            cache_dir=args.cache_dir,
            orchestrator=args.orchestrator,
            ostree_ref=args.ostree_ref,
            process=not args.no_process,
        )
    if args.bootc_action == "installer":
        return bootc_installer(
            args.manifest,
            args.ref,
            output=args.output,
            cache_dir=args.cache_dir,
            orchestrator=args.orchestrator,
            scratch=args.scratch,
        )
    raise ConfigError(f"unknown bootc action: {args.bootc_action}")


def main() -> int:
    configure_tracebacks()
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        return 0
    original_cwd = Path.cwd()
    project_config = _discover_project_config(original_cwd)
    if project_config is not None:
        os.chdir(project_config.parent)
    configure_logging()
    try:
        args.project = Project.from_file(project_config) if project_config else None
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
    finally:
        if project_config is not None:
            os.chdir(original_cwd)


def _discover_project_config(start: Path) -> Path | None:
    root = start.resolve()
    for directory in (root, *root.parents):
        config = directory / "ludos.yml"
        if config.exists():
            return config
    return None


if __name__ == "__main__":
    sys.exit(main())

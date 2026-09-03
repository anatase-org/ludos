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
from .ci import (
    DEFAULT_PREPARE_WORKERS,
    DEFAULT_VERSION_LABEL,
    SeedDiskSpaceError,
    build_ci,
    init_ci,
    prepare_ci,
    promote_ci,
    remove_ci,
    seed_ci,
    upload_ci,
    write_ci_env,
)
from .flatpaks import build_flatpak, build_flatpaks
from .installer import bootc_installer
from .logging import LOGO_STR, configure_logging, configure_tracebacks, error, log
from .model import ConfigError, Project, validate_manifest
from .contrib.package import package_target
from .contrib.patchwork import patch_target
from .contrib.update import update_targets
from .upload.file import delete_file, upload_file
from .upload.flatpaks import (
    tree_shake_flatpaks,
    update_flatpak_index,
    upload_dummy_runtime,
    upload_flatpaks,
)
from .upload.gpg import sign_detached, sign_file
from .upload.registry import (
    create_oci_index,
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

    build = subcommands.add_parser("build", help="Build Ludos manifests.")
    build.add_argument(
        "manifests",
        nargs="+",
        type=Path,
        help="Paths to Ludos YAML manifests.",
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
        help="Repository/package cache version to load. Defaults to the current UTC week's Monday as YYYYMMDD and creates missing cache images.",
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
        "--force",
        action="store_true",
        help="Rebuild final images even when the hash-addressed image already exists.",
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
    target.add_argument(
        "--all",
        action="store_true",
        help="Build manifests first, then build their declared flatpaks.",
    )
    build.set_defaults(func=build_command)

    validate = subcommands.add_parser("validate", help="Validate Ludos config files.")
    validate.add_argument("manifest", type=Path, help="Path to a Ludos YAML file.")
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
    update_target = update.add_mutually_exclusive_group()
    update_target.add_argument(
        "--card",
        default=None,
        help="Update only the selected card from a manifest.",
    )
    update_target.add_argument(
        "--flatpak",
        default=None,
        help="Update only the selected flatpak from a manifest.",
    )
    update_target.add_argument(
        "--flatpaks",
        action="store_true",
        help="Update every flatpak declared by the manifest's flatpaks list.",
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
    registry_file_upload.add_argument(
        "--sign",
        action="store_true",
        help="Upload detached OpenPGP .sig files next to the uploaded file.",
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

    registry_gpg = registry_subcommands.add_parser(
        "gpg",
        help="Create OpenPGP signatures using registry signing keys.",
    )
    registry_gpg_subcommands = registry_gpg.add_subparsers(
        dest="registry_gpg_action",
        required=True,
    )
    registry_gpg_sign = registry_gpg_subcommands.add_parser(
        "sign",
        help="Create a binary attached OpenPGP signature.",
    )
    registry_gpg_sign.add_argument(
        "input_path",
        type=Path,
        help="Path to the input file.",
    )
    registry_gpg_sign.add_argument(
        "output_path",
        type=Path,
        help="Path to write the signed output.",
    )
    registry_gpg_sign.add_argument(
        "--verify",
        action="store_true",
        help="Verify the written signature with gpg.",
    )
    registry_gpg_sign.set_defaults(func=registry_command)

    registry_gpg_sign_detached = registry_gpg_subcommands.add_parser(
        "sign-detached",
        help="Create a binary detached OpenPGP signature next to the input file.",
    )
    registry_gpg_sign_detached.add_argument(
        "input_path",
        type=Path,
        help="Path to the input file.",
    )
    registry_gpg_sign_detached.add_argument(
        "--verify",
        action="store_true",
        help="Verify the written signature with gpg.",
    )
    registry_gpg_sign_detached.set_defaults(func=registry_command)

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
    registry_flatpak_upload.add_argument(
        "--cache",
        action="store_true",
        help="Only use cached repository and orchestrator images while resolving flatpak images.",
    )
    registry_flatpak_upload.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the static flatpak index after uploading.",
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
    registry_flatpak_refresh = registry_flatpak_subcommands.add_parser(
        "refresh",
        help="Refresh the static flatpak index for a manifest distro.",
    )
    registry_flatpak_refresh.add_argument(
        "manifest",
        type=Path,
        help="Path to a Ludos YAML manifest.",
    )
    registry_flatpak_refresh.set_defaults(func=registry_command)
    registry_flatpak_dummy_runtime = registry_flatpak_subcommands.add_parser(
        "init-dummy-runtime",
        help="Initialize a dummy flatpak runtime OCI image and refresh the static index.",
    )
    registry_flatpak_dummy_runtime.add_argument(
        "manifest",
        type=Path,
        help="Path to a Ludos YAML manifest.",
    )
    registry_flatpak_dummy_runtime.add_argument(
        "--prefix",
        default="",
        help="Prefix for the published flatpak tag. Defaults to no prefix.",
    )
    registry_flatpak_dummy_runtime.set_defaults(func=registry_command)

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
    registry_oci_index = registry_oci_subcommands.add_parser(
        "index",
        help="Create a multi-platform OCI image index from existing tags.",
    )
    registry_oci_index.add_argument(
        "ref",
        help="OCI repository path within the registry, without the registry host.",
    )
    registry_oci_index.add_argument(
        "--tag",
        action="append",
        required=True,
        dest="tags",
        help=(
            "Target tag to publish for the image index. "
            "May be specified more than once."
        ),
    )
    registry_oci_index.add_argument(
        "--source-tag",
        action="append",
        required=True,
        dest="source_tags",
        help=(
            "Existing platform-specific tag to include. "
            "May be specified more than once."
        ),
    )
    registry_oci_index.set_defaults(func=registry_command)
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
        "--previous-manifest",
        default=None,
        metavar="URI",
        help="Remote OCI image whose layer plan should seed rechunking.",
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
        help="Repository/package cache version to load. Defaults to the current UTC week's Monday as YYYYMMDD and creates missing cache images.",
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
    create_parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild final images even when the hash-addressed image already exists.",
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
    installer_parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild the installer image even when the hash-addressed image already exists.",
    )
    installer_parser.set_defaults(func=bootc_command)

    ci = subcommands.add_parser(
        "ci",
        help="Create and consume CI fan-out manifests.",
    )
    ci_subcommands = ci.add_subparsers(dest="ci_action", required=True)
    env_parser = ci_subcommands.add_parser(
        "env",
        help="Write CI environment values derived from an existing image.",
    )
    env_parser.add_argument(
        "manifest",
        type=Path,
        help="Path to a Ludos YAML manifest.",
    )
    env_parser.add_argument(
        "ref",
        help="Remote OCI image reference to inspect.",
    )
    env_parser.add_argument(
        "--label",
        default=DEFAULT_VERSION_LABEL,
        help=f"OCI version label to compare. Defaults to {DEFAULT_VERSION_LABEL}.",
    )
    env_parser.add_argument(
        "--arch",
        default=None,
        help="OCI architecture to inspect. Defaults to the current architecture.",
    )
    env_parser.set_defaults(func=ci_command)
    init_parser = ci_subcommands.add_parser(
        "init",
        help="Create and upload CI repository and orchestrator images.",
    )
    init_parser.add_argument(
        "manifests",
        nargs="+",
        type=Path,
        help="Paths to Ludos YAML manifests.",
    )
    init_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for build, dnf, and package caches. Defaults to ./cache next to the first manifest.",
    )
    init_parser.add_argument(
        "--version",
        default=None,
        help="Repository/package cache version to load. Defaults to the current UTC week's Monday as YYYYMMDD.",
    )
    init_parser.add_argument(
        "--recreate",
        action="store_true",
        help="Recreate and upload orchestrator and repository images.",
    )
    init_parser.set_defaults(func=ci_command)
    prepare_parser = ci_subcommands.add_parser(
        "prepare",
        help="Prepare shared CI images and write build metadata.",
    )
    prepare_parser.add_argument(
        "manifests",
        nargs="+",
        type=Path,
        help="Paths to Ludos YAML manifests.",
    )
    prepare_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for build, dnf, and package caches. Defaults to ./cache next to the first manifest.",
    )
    prepare_parser.add_argument(
        "--version",
        default=None,
        help="Repository/package cache version to load. Defaults to the current UTC week's Monday as YYYYMMDD and creates missing cache images.",
    )
    prepare_parser.add_argument(
        "--ci",
        action="store_true",
        help="Prepare final images with combined package and postprocess layers.",
    )
    prepare_parser.add_argument(
        "--full",
        action="store_true",
        help="Include already-built final images and flatpaks in the CI metadata.",
    )
    prepare_parser.add_argument(
        "--no-flatpaks",
        action="store_true",
        help="Skip Flatpak preparation and omit Flatpaks from the CI metadata.",
    )
    prepare_parser.add_argument(
        "--prefix",
        default="",
        help="Prefix for the published flatpak tag. Defaults to no prefix.",
    )
    prepare_parser.add_argument(
        "--tag",
        default="latest",
        help="Published final image tag to inspect. Defaults to latest.",
    )
    prepare_parser.add_argument(
        "--registry",
        default="",
        help=(
            "Published OCI registry to inspect instead of checking final outputs "
            "in the CI cache registry."
        ),
    )
    prepare_parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_PREPARE_WORKERS,
        help=(
            "Number of parallel builder-package resolvers. Defaults to the smaller "
            "of 4 and the available CPU count."
        ),
    )
    prepare_parser.set_defaults(func=ci_command)
    seed_parser = ci_subcommands.add_parser(
        "seed",
        help="Create and upload missing CI card and builder images.",
    )
    seed_parser.add_argument(
        "build_manifest",
        nargs="?",
        type=Path,
        default=None,
        help="Path to a CI build.yml generated by 'ludos ci prepare'.",
    )
    seed_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory containing the default CI build manifest. Defaults to ./cache.",
    )
    seed_parser.add_argument(
        "--autoremove",
        action="store_true",
        help="Remove each local seed image after its build or upload finishes.",
    )
    seed_parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_PREPARE_WORKERS,
        help=(
            "Number of parallel image builders. Defaults to the smaller of 4 "
            "and the available CPU count."
        ),
    )
    seed_parser.set_defaults(func=ci_command)
    ci_build_parser = ci_subcommands.add_parser(
        "build",
        help="Build outputs from prepared CI metadata.",
    )
    ci_build_parser.add_argument(
        "build_ids",
        nargs="*",
        metavar="BUILD_ID",
        help="Prepared builds, images, or flatpaks IDs. The ID 0 is a no-op.",
    )
    ci_build_parser.add_argument(
        "--builds",
        action="store_true",
        help="Build every outstanding package build image.",
    )
    ci_build_parser.add_argument(
        "--images",
        action="store_true",
        help="Build every outstanding final manifest image.",
    )
    ci_build_parser.add_argument(
        "--flatpaks",
        action="store_true",
        help="Build every outstanding final flatpak image.",
    )
    ci_build_parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload each output to the CI registry after it is built.",
    )
    ci_build_parser.add_argument(
        "--ci",
        action="store_true",
        help="Build final images with combined package and postprocess layers.",
    )
    ci_build_parser.add_argument(
        "--cache",
        action="store_true",
        help="Read and write image build intermediate layers in <ci.registry>/cache.",
    )
    ci_build_parser.add_argument(
        "--autoremove",
        action="store_true",
        help=(
            "Remove restored cards: and builds: dependencies after successful "
            "builds and, with --upload, remove uploaded outputs."
        ),
    )
    ci_build_parser.add_argument(
        "--ccache",
        action="store_true",
        help="Mount and enable shared ccache/sccache directories for builder runs.",
    )
    ci_build_parser.set_defaults(func=ci_command)
    ci_upload_parser = ci_subcommands.add_parser(
        "upload",
        help="Upload built final images and flatpaks to the S3 registry.",
    )
    ci_upload_parser.add_argument(
        "upload_ids",
        nargs="*",
        metavar="OUTPUT_ID",
        help="Prepared image or flatpak IDs. The ID 0 is a no-op.",
    )
    ci_upload_parser.add_argument(
        "--images",
        action="store_true",
        help="Upload every prepared final manifest image.",
    )
    ci_upload_parser.add_argument(
        "--flatpaks",
        action="store_true",
        help="Upload every prepared final flatpak image.",
    )
    ci_upload_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh the static flatpak index after successful uploads.",
    )
    ci_upload_parser.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        metavar="TAG",
        help="Image tag to publish. May be specified more than once.",
    )
    ci_upload_parser.add_argument(
        "--previous-manifest",
        default=None,
        metavar="URI",
        help="Remote OCI image whose layer plan should seed rechunking.",
    )
    ci_upload_parser.add_argument(
        "--prefix",
        default="",
        help="Prefix for uploaded flatpak tags and the refreshed static index.",
    )
    ci_upload_parser.set_defaults(func=ci_command)
    ci_remove_parser = ci_subcommands.add_parser(
        "remove",
        help="Remove built final images and flatpaks from the CI registry.",
    )
    ci_remove_parser.add_argument(
        "remove_ids",
        nargs="*",
        metavar="OUTPUT_ID",
        help="Prepared image or flatpak IDs. The ID 0 is a no-op.",
    )
    ci_remove_parser.add_argument(
        "--images",
        action="store_true",
        help="Remove every prepared final manifest image.",
    )
    ci_remove_parser.add_argument(
        "--flatpaks",
        action="store_true",
        help="Remove every prepared final flatpak image.",
    )
    ci_remove_parser.set_defaults(func=ci_command)
    ci_promote_parser = ci_subcommands.add_parser(
        "promote",
        help="Promote image and flatpak tags within the S3 registry.",
    )
    ci_promote_parser.add_argument(
        "manifests",
        nargs="+",
        type=Path,
        metavar="MANIFEST",
        help="Paths to Ludos YAML manifests whose outputs should be promoted.",
    )
    ci_promote_parser.add_argument(
        "--images",
        action="store_true",
        help="Promote final manifest image tags.",
    )
    ci_promote_parser.add_argument(
        "--flatpaks",
        action="store_true",
        help="Promote flatpaks declared by the manifests.",
    )
    ci_promote_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh destination flatpak indexes after promotion.",
    )
    ci_promote_parser.add_argument(
        "--arch",
        action="append",
        default=[],
        dest="arches",
        metavar="ARCH",
        help=(
            "Architecture whose flatpak distro tags should be promoted. "
            "May be specified more than once. Defaults to the host architecture."
        ),
    )
    ci_promote_parser.add_argument(
        "--prefix",
        required=True,
        help="Source prefix for flatpak distro tags.",
    )
    ci_promote_parser.add_argument(
        "--from",
        required=True,
        dest="from_tag",
        metavar="TAG",
        help="Source image tag.",
    )
    ci_promote_parser.add_argument(
        "--to",
        required=True,
        dest="to_tag",
        metavar="TAG",
        help="Destination image tag.",
    )
    ci_promote_parser.set_defaults(func=ci_command)

    cleanup = subcommands.add_parser(
        "cleanup",
        help="Remove stale local Ludos cache images.",
    )
    cleanup.add_argument(
        "--version",
        default=None,
        help="Cache version to keep. Defaults to the current UTC week's Monday as YYYYMMDD.",
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
        "--cache",
        action="store_true",
        help="Only use cached repository and orchestrator images while resolving manifests.",
    )
    cleanup.add_argument(
        "--purge",
        action="store_true",
        help=(
            "Remove all local Ludos images in the selected local prefix, "
            "including images resolved from manifests."
        ),
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
    result = validate_manifest(args.manifest)
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
    manifests = tuple(args.manifests)
    targeted = args.card is not None or args.flatpak is not None or args.flatpaks
    if targeted and len(manifests) != 1:
        raise ConfigError("targeted builds require exactly one manifest")
    manifest = manifests[0]

    if args.flatpak is not None:
        result = build_flatpak(
            manifest,
            args.flatpak,
            cache_dir=args.cache_dir,
            cache_version=args.version,
            cache_only=args.cache,
            ccache=not args.no_ccache,
            force=args.force,
        )
        _log_flatpak_result(result)
        return 0

    if args.flatpaks:
        results = build_flatpaks(
            manifest,
            cache_dir=args.cache_dir,
            cache_version=args.version,
            cache_only=args.cache,
            ccache=not args.no_ccache,
            force=args.force,
        )
        for result in results:
            _log_flatpak_result(result)
        return 0

    for manifest in manifests:
        result = build_manifest(
            manifest,
            cache_dir=args.cache_dir,
            cache_version=args.version,
            cache_only=args.cache,
            ci=args.ci,
            ccache=not args.no_ccache,
            card=args.card,
            force=args.force,
        )
        _log_build_result(result, args.card)
    if args.all:
        for manifest in manifests:
            results = build_flatpaks(
                manifest,
                cache_dir=args.cache_dir,
                cache_version=args.version,
                cache_only=args.cache,
                ccache=not args.no_ccache,
                force=args.force,
            )
            for result in results:
                _log_flatpak_result(result)
    return 0


def _log_build_result(result: object, card: str | None = None) -> None:
    if card:
        card_name = result.build_blocks[0] if result.build_blocks else str(card)
        if result.build_images:
            log(f"Built card {card_name}: {result.build_images[0]}")
        else:
            log(f"Built card {card_name}: no build output image")
        return
    log(
        f"Built {result.output_image} for {result.image} on {result.distro} "
        f"with {Path(result.podman).name} using {result.orchestrator}"
    )
    blocks = ", ".join(
        _package_block_summary(block_name, block_packages, result.build_blocks)
        for block_name, block_packages in result.package_blocks
    )
    log(f"Package blocks: {blocks}")


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
        purge=args.purge,
        cache_only=args.cache,
    )


def update_command(args: argparse.Namespace) -> int:
    return update_targets(
        tuple(args.targets),
        cache_dir=args.cache_dir,
        patchwork_dir=args.patchwork_dir,
        dry_run=args.dry_run,
        assume_yes=args.assume_yes,
        card=args.card,
        flatpak=args.flatpak,
        flatpaks=args.flatpaks,
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
            return upload_file(
                args.path,
                args.output_path,
                args.download_name,
                sign=args.sign,
            )
        if args.registry_file_action == "delete":
            return delete_file(args.output_path)
        raise ConfigError(f"unknown registry file action: {args.registry_file_action}")
    if args.registry_action == "gpg":
        project_root = _project_root(args)
        if args.registry_gpg_action == "sign":
            return sign_file(
                args.input_path,
                args.output_path,
                verify=args.verify,
                project_root=project_root,
            )
        if args.registry_gpg_action == "sign-detached":
            return sign_detached(
                args.input_path,
                verify=args.verify,
                project_root=project_root,
            )
        raise ConfigError(f"unknown registry gpg action: {args.registry_gpg_action}")
    if args.registry_action == "flatpak":
        if args.registry_flatpak_action == "upload":
            result = upload_flatpaks(
                args.manifest,
                tuple(args.flatpaks or ()),
                build=args.build,
                cache_dir=args.cache_dir,
                cache_only=args.cache,
            )
            if result != 0:
                return result
            if args.refresh:
                return update_flatpak_index(args.manifest)
            return result
        if args.registry_flatpak_action == "tree-shake":
            return tree_shake_flatpaks(
                args.manifest,
                tuple(args.flatpaks or ()),
                dry_run=args.dry_run,
            )
        if args.registry_flatpak_action == "refresh":
            return update_flatpak_index(args.manifest)
        if args.registry_flatpak_action == "init-dummy-runtime":
            return upload_dummy_runtime(args.manifest, prefix=args.prefix)
        raise ConfigError(
            f"unknown registry flatpak action: {args.registry_flatpak_action}"
        )
    if args.registry_action == "oci":
        if args.registry_oci_action == "upload":
            return upload_oci(
                args.local_oci_path,
                args.ref,
                tuple(args.tags),
                project_root=_project_root(args),
            )
        if args.registry_oci_action == "index":
            return create_oci_index(
                args.ref,
                tuple(args.source_tags),
                tuple(args.tags),
                project_root=_project_root(args),
            )
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
            return tree_shake_oci(
                args.ref,
                dry_run=args.dry_run,
                project_root=_project_root(args),
            )
        raise ConfigError(f"unknown registry oci action: {args.registry_oci_action}")
    raise ConfigError(f"unknown registry action: {args.registry_action}")


def bootc_command(args: argparse.Namespace) -> int:
    if args.bootc_action == "create":
        return bootc_create(
            tuple(args.manifests),
            chunks=args.chunks,
            previous_manifest=args.previous_manifest,
            cache_dir=args.cache_dir,
            cache_version=args.version,
            cache_only=args.cache,
            ci=args.ci,
            ccache=not args.no_ccache,
            writers=args.writers,
            force=args.force,
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
            force=args.force,
        )
    raise ConfigError(f"unknown bootc action: {args.bootc_action}")


def ci_command(args: argparse.Namespace) -> int:
    if args.ci_action == "env":
        write_ci_env(args.manifest, args.ref, label=args.label, arch=args.arch)
        return 0
    if args.ci_action == "init":
        init_ci(
            tuple(args.manifests),
            cache_dir=args.cache_dir,
            cache_version=args.version,
            recreate=args.recreate,
        )
        return 0
    if args.ci_action == "prepare":
        prepare_ci(
            tuple(args.manifests),
            cache_dir=args.cache_dir,
            cache_version=args.version,
            ci=args.ci,
            full=args.full,
            no_flatpaks=args.no_flatpaks,
            prefix=args.prefix,
            tag=args.tag,
            registry=args.registry,
            workers=args.workers,
        )
        return 0
    if args.ci_action == "seed":
        seed_ci(
            args.build_manifest,
            cache_dir=args.cache_dir,
            autoremove=args.autoremove,
            workers=args.workers,
        )
        return 0
    if args.ci_action == "build":
        build_ci(
            tuple(args.build_ids),
            builds=args.builds,
            images=args.images,
            flatpaks=args.flatpaks,
            upload=args.upload,
            ci=args.ci,
            cache=args.cache,
            autoremove=args.autoremove,
            ccache=args.ccache,
        )
        return 0
    if args.ci_action == "upload":
        return upload_ci(
            tuple(args.upload_ids),
            images=args.images,
            flatpaks=args.flatpaks,
            refresh=args.refresh,
            tags=tuple(args.tags),
            previous_manifest=args.previous_manifest,
            prefix=args.prefix,
        )
    if args.ci_action == "remove":
        return remove_ci(
            tuple(args.remove_ids),
            images=args.images,
            flatpaks=args.flatpaks,
        )
    if args.ci_action == "promote":
        return promote_ci(
            tuple(args.manifests),
            prefix=args.prefix,
            from_tag=args.from_tag,
            to_tag=args.to_tag,
            arches=tuple(args.arches),
            images=args.images,
            flatpaks=args.flatpaks,
            refresh=args.refresh,
        )
    raise ConfigError(f"unknown ci action: {args.ci_action}")


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
    except SeedDiskSpaceError as exc:
        error(exc)
        return 7
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


def _project_root(args: argparse.Namespace) -> Path:
    project = getattr(args, "project", None)
    if project is not None:
        return project.root
    return Path.cwd()


if __name__ == "__main__":
    sys.exit(main())

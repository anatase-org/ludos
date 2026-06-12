from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from .build import build_manifest
from .logging import LOGO_STR, configure_tracebacks, error, log
from .model import ConfigError, validate_manifest


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
        type=Path,
        default=None,
        help="Directory for build, dnf, and package caches. Defaults to ./cache next to the manifest.",
    )
    build.add_argument(
        "--version",
        default=None,
        help="Repository/package cache version to load. Defaults to the current ISO YYYY-WW and creates missing cache images.",
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

    return parser


def show_logo(_args: argparse.Namespace) -> int:
    log(LOGO_STR)
    log("Starting Ludos...")
    return 0


def validate_command(args: argparse.Namespace) -> int:
    result = validate_manifest(args.manifest, args.cards_dir)
    if result.missing_repos:
        missing = ", ".join(result.missing_repos)
        raise ConfigError(f"{args.manifest}: missing repository definitions: {missing}")
    if result.missing_cards:
        missing = ", ".join(result.missing_cards)
        raise ConfigError(f"{args.manifest}: missing card definitions: {missing}")

    log(f"Manifest is valid: {len(result.repos)} repos, {len(result.cards)} cards")
    return 0


def build_command(args: argparse.Namespace) -> int:
    result = build_manifest(args.manifest, args.cards_dir, args.cache, args.version)
    log(
        f"Built {result.output_image} for {result.image} on {result.distro} "
        f"with {Path(result.podman).name} using {result.bootstrap}"
    )
    blocks = ", ".join(
        f"{block_name}: {len(block_packages)}"
        for block_name, block_packages in result.package_blocks
    )
    log(f"Package blocks: {blocks}")
    return 0


def main() -> int:
    configure_tracebacks()
    parser = build_parser()
    args = parser.parse_args()
    show_logo(args)
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
        command = " ".join(shlex.quote(str(part)) for part in exc.cmd)
        error(f"command failed with exit status {exc.returncode}: {command}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

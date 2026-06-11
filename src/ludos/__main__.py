from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .build import build_manifest
from .logging import LOGO_STR
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

    parser.set_defaults(func=show_logo)
    return parser


def show_logo(_args: argparse.Namespace) -> int:
    print(LOGO_STR)
    return 0


def validate_command(args: argparse.Namespace) -> int:
    result = validate_manifest(args.manifest, args.cards_dir)
    if result.missing_repos:
        missing = ", ".join(result.missing_repos)
        raise ConfigError(f"{args.manifest}: missing repository definitions: {missing}")
    if result.missing_cards:
        missing = ", ".join(result.missing_cards)
        raise ConfigError(f"{args.manifest}: missing card definitions: {missing}")

    print(f"Manifest is valid: {len(result.repos)} repos, {len(result.cards)} cards")
    return 0


def build_command(args: argparse.Namespace) -> int:
    result = build_manifest(args.manifest, args.cards_dir)
    print(
        f"Downloaded {len(result.resolved_packages)} resolved packages "
        f"for {result.distro} with {Path(result.dnf).name} into {result.package_dir}"
    )
    print(f"Package list: {result.package_list}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ConfigError as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())

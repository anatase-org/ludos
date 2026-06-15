from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..logging import log
from ..model import ConfigError
from .patch import display_path, run


DEFAULT_BRANCH = "rawhide"
VCS_DIR_NAMES = {".git", ".hg", ".svn"}


class IndentedSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def package_target(
    action: str,
    git_url: str,
    location: Path,
    *,
    card: Path | None = None,
) -> int:
    if action == "fork":
        return fork_package(git_url, location, card=card)
    raise ConfigError(f"unsupported package action: {action}")


def fork_package(git_url: str, location: Path, *, card: Path | None = None) -> int:
    destination = location.expanduser().resolve()
    _guard_empty_destination(destination)
    card_path = _resolve_card_path(destination, card)

    with tempfile.TemporaryDirectory(prefix="ludos-package-") as temp_dir:
        repo_dir = Path(temp_dir) / "repo"
        run(["git", "clone", "--depth", "1", git_url, str(repo_dir)])
        spec_paths = _discover_specs(repo_dir)
        if not spec_paths:
            raise ConfigError(f"{git_url}: package repo has no spec files")

        card_data = _load_card(card_path)
        specs = _card_specs(card_data, card_path)
        entries = _spec_entries(
            repo_dir=repo_dir,
            destination=destination,
            card_path=card_path,
            git_url=git_url,
            spec_paths=spec_paths,
        )
        _guard_duplicate_specs(specs, entries, card_path)

        _copy_repo_contents(repo_dir, destination)
        specs.extend(entries)
        _write_card(card_path, card_data)

    log(
        f"Forked package into {display_path(destination)}; "
        f"added {len(spec_paths)} specs to {display_path(card_path)}"
    )
    return 0


def _guard_empty_destination(destination: Path) -> None:
    if destination.exists() and not destination.is_dir():
        raise ConfigError(f"{destination}: package location exists but is not a directory")
    if destination.exists() and any(destination.iterdir()):
        raise ConfigError(f"{destination}: package location exists but is not empty")


def _resolve_card_path(destination: Path, card: Path | None) -> Path:
    if card is not None:
        return card.expanduser().resolve()
    return destination / "card.yml"


def _discover_specs(repo_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                path
                for path in repo_dir.rglob("*.spec")
                if path.is_file() and not _has_vcs_part(path.relative_to(repo_dir))
            ),
            key=lambda path: path.relative_to(repo_dir).as_posix(),
        )
    )


def _load_card(card_path: Path) -> dict[str, Any]:
    if not card_path.exists():
        return {"version": 1}
    try:
        data = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{card_path}: invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{card_path}: expected a YAML mapping")
    version = data.setdefault("version", 1)
    if version != 1:
        raise ConfigError(f"{card_path}: 'version' must be 1")
    return data


def _card_specs(card_data: dict[str, Any], card_path: Path) -> list[dict[str, Any]]:
    specs = card_data.setdefault("specs", [])
    if not isinstance(specs, list):
        raise ConfigError(f"{card_path}: 'specs' must be a list of spec mappings")
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ConfigError(f"{card_path}: 'specs[{index}]' must be a mapping")
    return specs


def _spec_entries(
    *,
    repo_dir: Path,
    destination: Path,
    card_path: Path,
    git_url: str,
    spec_paths: tuple[Path, ...],
) -> list[dict[str, Any]]:
    entries = []
    for index, spec_path in enumerate(spec_paths):
        destination_spec = destination / spec_path.relative_to(repo_dir)
        entry: dict[str, Any] = {
            "spec": _relative_path(card_path.parent, destination_spec),
            "files": _spec_files(repo_dir, spec_path),
            "packages": [_spec_name(spec_path)],
        }
        if index == 0:
            entry["upstream"] = {
                "type": "dist-git",
                "url": git_url,
                "branch": DEFAULT_BRANCH,
            }
        entries.append(entry)
    return entries


def _spec_files(repo_dir: Path, spec_path: Path) -> list[str]:
    spec_dir = spec_path.parent
    files = [
        path.relative_to(spec_dir).as_posix()
        for path in spec_dir.rglob("*")
        if path.is_file()
        and not _has_vcs_part(path.relative_to(repo_dir))
        and not _has_vcs_part(path.relative_to(spec_dir))
    ]
    return sorted(files)


def _spec_name(spec_path: Path) -> str:
    pattern = re.compile(r"^Name:\s*(\S+)", re.IGNORECASE)
    for line in spec_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    raise ConfigError(f"{spec_path}: missing RPM Name header")


def _guard_duplicate_specs(
    specs: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    card_path: Path,
) -> None:
    existing = {
        spec["spec"]
        for spec in specs
        if isinstance(spec.get("spec"), str)
    }
    duplicates = [entry["spec"] for entry in entries if entry["spec"] in existing]
    if duplicates:
        duplicate_list = ", ".join(duplicates)
        raise ConfigError(f"{card_path}: duplicate spec entries: {duplicate_list}")


def _copy_repo_contents(repo_dir: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in repo_dir.iterdir():
        if child.name in VCS_DIR_NAMES:
            continue
        target = destination / child.name
        if child.is_symlink():
            os.symlink(os.readlink(child), target)
        elif child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)


def _write_card(card_path: Path, card_data: dict[str, Any]) -> None:
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(
        yaml.dump(card_data, Dumper=IndentedSafeDumper, sort_keys=False),
        encoding="utf-8",
    )


def _relative_path(base_dir: Path, path: Path) -> str:
    return Path(os.path.relpath(path, base_dir)).as_posix()


def _has_vcs_part(path: Path) -> bool:
    return any(part in VCS_DIR_NAMES for part in path.parts)

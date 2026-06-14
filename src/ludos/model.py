from __future__ import annotations

import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class UpstreamRef:
    type: str
    url: str
    branch: str = ""
    ref: str = ""


@dataclass(frozen=True)
class SpecBuild:
    spec: str
    packages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    replace: dict[str, str] = field(default_factory=dict)
    files: tuple[str, ...] = tuple()
    upstream: UpstreamRef | None = None


@dataclass(frozen=True)
class Card:
    version: int
    priority: int = 1
    env: dict[str, str | int] = field(default_factory=dict)
    packages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    build_deps: tuple[str, ...] = tuple()
    specs: tuple[SpecBuild, ...] = tuple()
    repos: tuple["RepoRef", ...] = tuple()
    files: tuple[str, ...] = tuple()
    hash: str = ""
    prepare: str = ""
    build: str = ""
    postprocess: str = ""
    source: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> "Card":
        data = _load_mapping(path)
        return cls(
            version=_required_version(data, path),
            priority=_optional_int(data, "priority", path, default=1),
            env=_env_dict(data, path, include_default=False),
            packages=_packages_dict(data, "packages", path),
            build_deps=_string_tuple(data, "build-deps", path),
            specs=_spec_builds_tuple(data, "specs", path),
            repos=_repo_refs_tuple(data, "repos", path),
            files=_string_tuple(data, "files", path),
            hash=_optional_string(data, "hash", path),
            prepare=_optional_string(data, "prepare", path),
            build=_optional_string(data, "build", path),
            postprocess=_optional_string(data, "postprocess", path),
            source=path,
        )


@dataclass(frozen=True)
class RepoRef:
    repo: str
    priority: int
    vars: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedRepo:
    ref: RepoRef
    source: Path


@dataclass(frozen=True)
class Manifest:
    version: int
    env: dict[str, str | int]
    releasever: str
    distro: str
    orchestrator: str
    bootstrap: str
    repos: tuple[RepoRef, ...]
    cards: tuple[str, ...]
    orchestrator_deps: tuple[str, ...] = tuple()
    local_prefix: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    source: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> "Manifest":
        data = _load_mapping(path)
        return cls(
            version=_required_version(data, path),
            env=_env_dict(data, path),
            releasever=_required_string(data, "releasever", path),
            distro=_required_string(data, "distro", path),
            orchestrator=_required_string(data, "orchestrator", path),
            orchestrator_deps=_string_tuple(data, "orchestrator-deps", path),
            bootstrap=_required_string(data, "bootstrap", path),
            repos=_repo_refs_tuple(data, "repos", path),
            cards=_required_string_tuple(data, "cards", path),
            local_prefix=_optional_string(data, "local_prefix", path),
            labels=_string_dict(data, "labels", path),
            source=path,
        )


@dataclass(frozen=True)
class ManifestValidation:
    manifest: Manifest
    bootstrap: Card | None
    repos: tuple[ResolvedRepo, ...]
    cards: tuple[Card, ...]
    missing_bootstrap: str
    missing_repos: tuple[str, ...]
    missing_cards: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return (
            not self.missing_bootstrap
            and not self.missing_repos
            and not self.missing_cards
        )


def validate_manifest(
    manifest_path: Path, cards_dir: Path | None = None
) -> ManifestValidation:
    manifest_path = manifest_path.resolve()
    manifest = Manifest.from_file(manifest_path)
    root_dir = manifest_path.parent
    cards_dir = cards_dir.resolve() if cards_dir else None

    bootstrap_path = _resolve_card_path(manifest.bootstrap, root_dir, cards_dir)
    bootstrap = None
    missing_bootstrap = ""
    if bootstrap_path.exists():
        bootstrap = Card.from_file(bootstrap_path)
    else:
        missing_bootstrap = manifest.bootstrap

    repos = []
    missing_repos = []
    manifest_env = dict(manifest.env)
    manifest_env.setdefault("releasever", manifest.releasever)
    for repo_ref in manifest.repos:
        _validate_repo_vars(repo_ref, manifest_env)
        repo_path = _resolve_repo_path(repo_ref.repo, root_dir)
        if not repo_path.exists():
            missing_repos.append(repo_ref.repo)
            continue
        repos.append(ResolvedRepo(ref=repo_ref, source=repo_path))

    cards = []
    missing_cards = []
    for card_ref in manifest.cards:
        card_path = _resolve_card_path(card_ref, root_dir, cards_dir)
        if not card_path.exists():
            missing_cards.append(card_ref)
            continue
        card = Card.from_file(card_path)
        cards.append(card)

    return ManifestValidation(
        manifest=manifest,
        bootstrap=bootstrap,
        repos=tuple(repos),
        cards=tuple(cards),
        missing_bootstrap=missing_bootstrap,
        missing_repos=tuple(missing_repos),
        missing_cards=tuple(missing_cards),
    )


def _resolve_card_path(
    card_ref: str, root_dir: Path, cards_dir: Path | None = None
) -> Path:
    path = Path(card_ref)
    if cards_dir and len(path.parts) == 1:
        path = cards_dir / path
    elif not path.is_absolute():
        path = root_dir / path

    if path.suffix in (".yml", ".yaml"):
        return path

    if path.is_dir():
        for card_path in (
            path / "card.yml",
            path / "card.yaml",
            path / f"{path.name}.yml",
            path / f"{path.name}.yaml",
        ):
            if card_path.exists():
                return card_path
        return path / "card.yml"

    return path.with_suffix(".yml")


def _resolve_repo_path(repo_ref: str, root_dir: Path) -> Path:
    path = Path(repo_ref)
    if path.is_absolute() or len(path.parts) != 1:
        raise ConfigError(
            f"repository reference '{repo_ref}' must be a name from the top-level repos directory"
        )

    path = root_dir / "repos" / path

    if path.suffix == ".repo":
        return path

    return path.with_suffix(".repo")


def _validate_repo_vars(repo_ref: RepoRef, env: dict[str, str | int]) -> None:
    for var_name, var_value in repo_ref.vars.items():
        for env_name in _referenced_env_vars(var_value):
            if env_name not in env:
                raise ConfigError(
                    f"repository '{repo_ref.repo}' variable '{var_name}' references undefined env '{env_name}'"
                )


def _referenced_env_vars(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", value))


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: file does not exist") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a YAML mapping")
    return data


def _required_version(data: dict[str, Any], path: Path) -> int:
    value = data.get("version")
    if value != 1:
        raise ConfigError(f"{path}: 'version' must be 1")
    return value


def _required_string(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: '{key}' must be a non-empty string")
    return value


def _optional_string(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{path}: '{key}' must be a string")
    return value


def _optional_int(data: dict[str, Any], key: str, path: Path, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int):
        raise ConfigError(f"{path}: '{key}' must be an integer")
    return value


def _default_env() -> dict[str, str | int]:
    return {"arch": platform.machine()}


def _env_dict(
    data: dict[str, Any], path: Path, *, include_default: bool = True
) -> dict[str, str | int]:
    env = _default_env() if include_default else {}
    value = data.get("env", {})
    if value is None:
        return env
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, (str, int)) for k, v in value.items()
    ):
        raise ConfigError(f"{path}: 'env' must be a mapping of strings or integers")

    env.update(value)
    return env


def _string_tuple(data: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return tuple()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: '{key}' must be a list of strings")
    return tuple(value)


def _required_string_tuple(
    data: dict[str, Any], key: str, path: Path
) -> tuple[str, ...]:
    value = _string_tuple(data, key, path)
    if not value:
        raise ConfigError(f"{path}: '{key}' must contain at least one item")
    return value


def _repo_refs_tuple(data: dict[str, Any], key: str, path: Path) -> tuple[RepoRef, ...]:
    value = data.get(key)
    if value is None:
        return tuple()
    if not isinstance(value, list):
        raise ConfigError(f"{path}: '{key}' must be a list of repository mappings")

    repos = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(
                f"{path}: '{key}[{index}]' must be a mapping with repo and priority"
            )

        repo = item.get("repo")
        priority = item.get("priority")
        if not isinstance(repo, str) or not repo.strip():
            raise ConfigError(f"{path}: '{key}[{index}].repo' must be a string")
        if not isinstance(priority, int):
            raise ConfigError(f"{path}: '{key}[{index}].priority' must be an integer")
        repo_vars = {}
        for var_key, var_value in item.items():
            if var_key in ("repo", "priority"):
                continue
            if not isinstance(var_key, str) or not isinstance(var_value, str):
                raise ConfigError(
                    f"{path}: '{key}[{index}]' repository variables must be strings"
                )
            repo_vars[var_key] = var_value

        repos.append(RepoRef(repo=repo, priority=priority, vars=repo_vars))

    return tuple(repos)


def _spec_builds_tuple(
    data: dict[str, Any], key: str, path: Path
) -> tuple[SpecBuild, ...]:
    value = data.get(key)
    if value is None:
        return tuple()
    if not isinstance(value, list):
        raise ConfigError(f"{path}: '{key}' must be a list of spec mappings")

    specs = []
    for index, item in enumerate(value):
        label = f"{key}[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: '{label}' must be a mapping")

        spec = item.get("spec")
        if not isinstance(spec, str) or not spec.strip():
            raise ConfigError(f"{path}: '{label}.spec' must be a non-empty string")

        packages = _packages_dict(item, "packages", path, label)
        replace = _spec_replace_dict(item, "replace", path, label)
        files = _string_tuple(item, "files", path)
        upstream = _upstream_ref(item, "upstream", path, label)
        specs.append(
            SpecBuild(
                spec=spec,
                packages=packages,
                replace=replace,
                files=files,
                upstream=upstream,
            )
        )

    return tuple(specs)


def _packages_dict(
    data: dict[str, Any], key: str, path: Path, label: str = ""
) -> dict[str, tuple[str, ...]]:
    qualified_key = f"{label}.{key}" if label else key
    value = data.get(key, {})
    if value is None:
        return {}
    if isinstance(value, list):
        if not all(isinstance(package, str) for package in value):
            raise ConfigError(f"{path}: '{qualified_key}' must be a list of strings")
        return {"*": tuple(value)}
    if not isinstance(value, dict):
        raise ConfigError(
            f"{path}: '{qualified_key}' must be a list of strings or an arch mapping"
        )

    result = {}
    for arch, packages in value.items():
        if not isinstance(arch, str) or not arch.strip():
            raise ConfigError(f"{path}: '{qualified_key}' arch keys must be strings")
        if not isinstance(packages, list) or not all(
            isinstance(package, str) for package in packages
        ):
            raise ConfigError(
                f"{path}: '{qualified_key}.{arch}' must be a list of strings"
            )
        result[arch] = tuple(packages)
    return result


def _spec_replace_dict(
    data: dict[str, Any], key: str, path: Path, label: str
) -> dict[str, str]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ConfigError(f"{path}: '{label}.{key}' must be a mapping of strings")
    return dict(value)


def _upstream_ref(
    data: dict[str, Any], key: str, path: Path, label: str
) -> UpstreamRef | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: '{label}.{key}' must be a mapping")

    upstream_type = value.get("type")
    url = value.get("url")
    branch = value.get("branch", "")
    ref = value.get("ref", "")
    if not isinstance(upstream_type, str) or not upstream_type.strip():
        raise ConfigError(f"{path}: '{label}.{key}.type' must be a non-empty string")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"{path}: '{label}.{key}.url' must be a non-empty string")
    if not isinstance(branch, str) or not isinstance(ref, str):
        raise ConfigError(
            f"{path}: '{label}.{key}.branch' and '{label}.{key}.ref' must be strings"
        )
    return UpstreamRef(type=upstream_type, url=url, branch=branch, ref=ref)


def _string_dict(data: dict[str, Any], key: str, path: Path) -> dict[str, str]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ConfigError(f"{path}: '{key}' must be a mapping of strings")
    return dict(value)

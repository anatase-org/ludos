from __future__ import annotations

import base64
import datetime as _datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .logging import log, stream
from .model import (
    ConfigError,
    FlatpakGpgConfig,
    FlatpakImagesConfig,
    ManifestValidation,
    OciCosignConfig,
    Project,
    validate_manifest,
)

_OCI_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
_REGISTRY_TIMEOUT = 2.0
_REGISTRY_RETRY_ATTEMPTS = 4
_REGISTRY_RETRY_BASE_DELAY = 0.25
_REGISTRY_RETRY_MAX_DELAY = 2.0
_REGISTRY_TRANSIENT_STATUSES = frozenset({0, 408, 425, 429})
_registry_bearer_tokens: dict[tuple[str, str], str] = {}
_registry_bearer_token_locks: dict[tuple[str, str], threading.Lock] = {}
_registry_bearer_tokens_lock = threading.Lock()


@dataclass(frozen=True)
class ResolvedManifestContext:
    validation: ManifestValidation
    image: str
    distro: str
    releasever: str
    arch: str
    root_dir: Path
    local_prefix: str
    orchestrator: str
    output_image: str
    manifest_env: dict[str, str]
    cache_version: str
    cache_dir: Path
    distro_cache_dir: Path
    package_dir: Path
    dnf_dir: Path
    build_dir: Path
    card_build_dir: Path
    dnf_workspace_dir: Path
    repo_dir: Path
    dnf_cache_dir: Path
    dnf_persist_dir: Path
    dnf_log_dir: Path
    dnf_resolve_dir: Path
    build_artifact_cache_dir: Path
    spec_source_cache_dir: Path
    ccache_dir: Path | None
    podman: str
    buildah: str | None
    repo_images: tuple[str, ...]
    ci_registry: str
    flatpak_images: FlatpakImagesConfig
    flatpak_gpg: FlatpakGpgConfig
    oci_cosign: OciCosignConfig


def resolve_manifest_context(
    manifest_path: Path,
    cache_dir: Path | None = None,
    cache_version: str | None = None,
    cache_only: bool = False,
    ccache: bool = True,
    dnf_workspace_dirs: list[Path] | None = None,
    dnf_workspace_dir: Path | None = None,
    image_exists=None,
    create_orchestrator_image=None,
    create_repo_image=None,
    extract_image_paths=None,
    apply_repo_priority=None,
    require_buildah=None,
) -> ResolvedManifestContext:
    image_exists = image_exists or _ensure_image
    create_orchestrator_image = create_orchestrator_image or _create_orchestrator_image
    create_repo_image = create_repo_image or _create_repo_image
    extract_image_paths = extract_image_paths or _extract_image_paths
    apply_repo_priority = apply_repo_priority or _apply_repo_priority
    require_buildah = require_buildah or _require_buildah

    log(f"Validating manifest: {manifest_path}")
    validation = validate_manifest(manifest_path)
    if validation.missing_bootstrap:
        raise ConfigError(
            f"{manifest_path}: missing bootstrap card: {validation.missing_bootstrap}"
        )
    if validation.missing_repos:
        missing = ", ".join(validation.missing_repos)
        raise ConfigError(f"{manifest_path}: missing repository definitions: {missing}")
    if validation.missing_cards:
        missing = ", ".join(validation.missing_cards)
        raise ConfigError(f"{manifest_path}: missing card definitions: {missing}")

    root_dir = manifest_path.resolve().parent
    project_config = _project_upload_config(root_dir)
    image = _cache_name(manifest_path.resolve().stem, "image")
    manifest_env = {key: str(value) for key, value in validation.manifest.env.items()}
    local_values = _load_dotenv(root_dir / ".env")
    local_prefix = local_values.pop("local_prefix", validation.manifest.local_prefix)
    local_prefix = _local_prefix(local_prefix)
    manifest_env.update(local_values)
    if cache_version is None:
        cache_version = _default_cache_version()
    else:
        cache_version = _cache_name(cache_version, "version")
    manifest_env["version"] = cache_version
    releasever = _cache_name(
        _substitute_variables(validation.manifest.releasever, manifest_env),
        "releasever",
    )
    manifest_env["releasever"] = releasever
    arch = _cache_name(
        _substitute_variables(str(manifest_env.get("arch", "")), manifest_env),
        "arch",
    )
    manifest_env["arch"] = arch
    manifest_env = {
        key: _substitute_variables(value, manifest_env)
        for key, value in manifest_env.items()
    }
    distro = _cache_name(
        _substitute_variables(validation.manifest.distro, manifest_env),
        "distro",
    )
    manifest_env["distro"] = distro
    orchestrator_source = _substitute_variables(
        validation.manifest.orchestrator, manifest_env
    )
    output_image = f"{local_prefix}{image}:{distro}"
    if cache_only:
        log("Using cache-only mode")

    if cache_dir is None:
        cache_dir = root_dir / "cache"
    else:
        cache_dir = cache_dir.expanduser().resolve()
    log(f"Preparing cache directories under {cache_dir}")
    distro_cache_dir = cache_dir / distro
    package_dir = distro_cache_dir / "packages"
    dnf_dir = distro_cache_dir / "dnf"
    build_dir = distro_cache_dir / "build" / image
    card_build_dir = distro_cache_dir / "cards"
    dnf_resolve_dir = dnf_dir / "resolves"
    build_artifact_cache_dir = distro_cache_dir / "build-artifacts"
    spec_source_cache_dir = cache_dir / "spec-sources" / "git"
    ccache_dir = cache_dir / "ccache" if ccache else None

    distro_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_dir.mkdir(parents=True, exist_ok=True)
    if dnf_workspace_dir is None:
        dnf_workspace_dir = Path(tempfile.mkdtemp(prefix="run-", dir=dnf_dir))
    else:
        dnf_workspace_dir = dnf_workspace_dir.expanduser().resolve()
        dnf_workspace_dir.mkdir(parents=True, exist_ok=True)
    if dnf_workspace_dirs is not None:
        dnf_workspace_dirs.append(dnf_workspace_dir)
    repo_dir = dnf_workspace_dir / "repos"
    dnf_cache_dir = dnf_workspace_dir / "cache"
    dnf_persist_dir = dnf_workspace_dir / "persist"
    dnf_log_dir = dnf_workspace_dir / "log"
    package_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    card_build_dir.mkdir(parents=True, exist_ok=True)
    build_artifact_cache_dir.mkdir(parents=True, exist_ok=True)
    spec_source_cache_dir.mkdir(parents=True, exist_ok=True)
    if ccache_dir is not None:
        ccache_dir.mkdir(parents=True, exist_ok=True)
    repo_dir.mkdir(parents=True, exist_ok=True)
    dnf_cache_dir.mkdir(parents=True, exist_ok=True)
    dnf_persist_dir.mkdir(parents=True, exist_ok=True)
    dnf_log_dir.mkdir(parents=True, exist_ok=True)
    dnf_resolve_dir.mkdir(parents=True, exist_ok=True)

    podman = shutil.which("podman")
    if not podman:
        raise ConfigError("podman must be installed to build")
    log(f"Using Podman: {podman}")
    buildah = shutil.which("buildah")
    if buildah:
        log(f"Using Buildah: {buildah}")

    orchestrator_deps = tuple(
        _substitute_variables(package, manifest_env)
        for package in validation.manifest.orchestrator_deps
    )
    if orchestrator_deps:
        orchestrator_tag = f"{distro}-{_package_hash(orchestrator_deps)}-{cache_version}"
    else:
        orchestrator_tag = f"{distro}-base-{cache_version}"

    orchestrator_image = _local_image(local_prefix, "orchestrator", orchestrator_tag)
    ci_registry = project_config.ci.registry
    if _call_image_exists(image_exists, podman, orchestrator_image, ci_registry):
        log(f"Reusing orchestrator image: {orchestrator_image}")
    elif cache_only:
        raise ConfigError(f"orchestrator image is not cached: {orchestrator_image}")
    else:
        log(f"Creating orchestrator image: {orchestrator_image}")
        create_orchestrator_image(
            podman=podman,
            buildah=buildah,
            source=orchestrator_source,
            image=orchestrator_image,
            packages=_build_deps(orchestrator_deps),
        )
    orchestrator = orchestrator_image

    log(f"Using DNF metadata workspace: {dnf_workspace_dir}")
    repo_images = []
    for repo in validation.repos:
        log(f"Rendering repository metadata: {repo.ref.repo}")
        repo_variables = dict(manifest_env)
        for key, value in repo.ref.vars.items():
            repo_variables[key] = _substitute_variables(value, repo_variables)

        rendered_repo = _substitute_variables(
            repo.source.read_text(encoding="utf-8"),
            repo_variables,
        )
        repo_lines = rendered_repo.rstrip().splitlines()
        repo_lines.append("metadata_expire=never")
        rendered_repo = "\n".join(repo_lines) + "\n"
        repo_id = _repo_id(rendered_repo, repo.source)
        repo_image = _local_image(
            local_prefix,
            "repos",
            f"{distro}-{repo.ref.repo}-{cache_version}",
        )
        repo_images.append(repo_image)
        if _call_image_exists(image_exists, podman, repo_image, ci_registry):
            log(f"Reusing repository metadata image: {repo_image}")
            extract_image_paths(
                podman,
                repo_image,
                {
                    "repos": repo_dir,
                    "cache": dnf_cache_dir,
                    "persist": dnf_persist_dir,
                },
            )
            apply_repo_priority(repo_dir / repo.source.name, repo.ref.priority)
            continue
        if cache_only:
            raise ConfigError(
                f"repository metadata image is not cached: {repo_image}"
            )

        log(f"Creating repository metadata image: {repo_image}")
        create_repo_image(
            podman=podman,
            buildah=require_buildah(buildah),
            orchestrator=orchestrator,
            root_dir=root_dir,
            image=repo_image,
            repo_name=repo.source.name,
            repo_id=repo_id,
            rendered_repo=rendered_repo,
        )
        log(f"Extracting repository metadata: {repo.ref.repo}")
        extract_image_paths(
            podman,
            repo_image,
            {
                "repos": repo_dir,
                "cache": dnf_cache_dir,
                "persist": dnf_persist_dir,
            },
        )
        apply_repo_priority(repo_dir / repo.source.name, repo.ref.priority)

    return ResolvedManifestContext(
        validation=validation,
        image=image,
        distro=distro,
        releasever=releasever,
        arch=arch,
        root_dir=root_dir,
        local_prefix=local_prefix,
        orchestrator=orchestrator,
        output_image=output_image,
        manifest_env=manifest_env,
        cache_version=cache_version,
        cache_dir=cache_dir,
        distro_cache_dir=distro_cache_dir,
        package_dir=package_dir,
        dnf_dir=dnf_dir,
        build_dir=build_dir,
        card_build_dir=card_build_dir,
        dnf_workspace_dir=dnf_workspace_dir,
        repo_dir=repo_dir,
        dnf_cache_dir=dnf_cache_dir,
        dnf_persist_dir=dnf_persist_dir,
        dnf_log_dir=dnf_log_dir,
        dnf_resolve_dir=dnf_resolve_dir,
        build_artifact_cache_dir=build_artifact_cache_dir,
        spec_source_cache_dir=spec_source_cache_dir,
        ccache_dir=ccache_dir,
        podman=str(podman),
        buildah=buildah,
        repo_images=tuple(repo_images),
        ci_registry=ci_registry,
        flatpak_images=project_config.flatpak_images,
        flatpak_gpg=project_config.flatpak_gpg,
        oci_cosign=project_config.oci_cosign,
    )


def _project_upload_config(root_dir: Path) -> Project:
    project_config = root_dir / "ludos.yml"
    if not project_config.exists():
        return Project(
            name=root_dir.name,
            root=root_dir,
            flatpak_images=FlatpakImagesConfig(),
            flatpak_gpg=FlatpakGpgConfig(),
            oci_cosign=OciCosignConfig(),
        )
    return Project.from_file(project_config)


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ConfigError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{path}:{line_number}: invalid environment key '{key}'")
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _local_prefix(value: str) -> str:
    if "/" in value or ":" in value:
        raise ConfigError(f"invalid local_prefix '{value}'")
    return value


def _local_image(local_prefix: str, repository: str, tag: str) -> str:
    return f"{local_prefix}{repository}:{tag}"


def _latest_image(image: str) -> str:
    repository, _tag = image.rsplit(":", 1)
    return f"{repository}:latest"


def _image_exists(podman: str, image: str) -> bool:
    try:
        return subprocess.run([podman, "image", "exists", image], check=False).returncode == 0
    except FileNotFoundError:
        return False


def _call_image_exists(
    image_exists: Callable[..., bool],
    podman: str,
    image: str,
    ci_registry: str,
) -> bool:
    try:
        return image_exists(podman, image, ci_registry)
    except TypeError:
        return image_exists(podman, image)


def _ensure_image(podman: str, image: str, ci_registry: str = "") -> bool:
    if _image_exists(podman, image):
        return True

    remote_image = _remote_cache_image(ci_registry, image)
    if remote_image is None:
        return False

    if not _remote_cache_image_exists(remote_image):
        return False

    log(f"Checking CI for image: {remote_image}")
    try:
        returncode, _ = _run_streamed_command([podman, "pull", remote_image])
    except FileNotFoundError:
        return False
    if returncode != 0:
        return False

    subprocess.run([podman, "tag", remote_image, image], check=True)
    return True


def _remote_cache_image(ci_registry: str, image: str) -> str | None:
    registry = ci_registry.strip().rstrip("/")
    if not registry:
        return None
    if "@" in image or ":" not in image:
        return None
    repository, tag = image.rsplit(":", 1)
    if not repository or not tag or "/" in repository:
        return None
    return f"{registry}/{repository}:{tag}"


def _remote_cache_image_exists(remote: str) -> bool:
    registry, repository, reference = _split_remote_image(remote)
    url = (
        f"https://{registry}/v2/"
        f"{urllib.parse.quote(repository, safe='/')}/manifests/"
        f"{urllib.parse.quote(reference, safe='')}"
    )
    headers = {"Accept": _OCI_MANIFEST_ACCEPT}
    basic_auth = _registry_basic_auth(registry)
    if basic_auth:
        headers["Authorization"] = f"Basic {basic_auth}"

    status, response_headers = _registry_head_with_backoff(url, headers)
    if status == 401:
        token = _cached_registry_bearer_token(
            response_headers.get("www-authenticate", ""),
            basic_auth,
        )
        if token:
            status, _response_headers = _registry_head_with_backoff(
                url,
                {
                    "Accept": _OCI_MANIFEST_ACCEPT,
                    "Authorization": f"Bearer {token}",
                },
            )

    if _registry_status_is_transient(status):
        raise ConfigError(f"registry unavailable while checking image: {remote}")
    return status == 200


def _registry_head_with_backoff(
    url: str,
    headers: dict[str, str],
) -> tuple[int, dict[str, str]]:
    for attempt in range(_REGISTRY_RETRY_ATTEMPTS):
        status, response_headers = _registry_head(url, headers)
        if not _registry_status_is_transient(status):
            return status, response_headers
        if attempt + 1 < _REGISTRY_RETRY_ATTEMPTS:
            _registry_backoff(attempt, response_headers)
    return status, response_headers


def _registry_status_is_transient(status: int) -> bool:
    return status in _REGISTRY_TRANSIENT_STATUSES or 500 <= status <= 599


def _registry_backoff(attempt: int, headers: dict[str, str]) -> None:
    delay = min(
        _REGISTRY_RETRY_BASE_DELAY * (2**attempt),
        _REGISTRY_RETRY_MAX_DELAY,
    )
    retry_after = headers.get("retry-after", "").strip()
    if retry_after:
        try:
            delay = min(max(float(retry_after), 0.0), _REGISTRY_RETRY_MAX_DELAY)
        except ValueError:
            pass
    time.sleep(delay)


def _registry_head(
    url: str,
    headers: dict[str, str],
) -> tuple[int, dict[str, str]]:
    request = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=_REGISTRY_TIMEOUT) as response:
            return response.status, _lower_headers(response.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, _lower_headers(exc.headers.items())
    except OSError:
        return 0, {}


def _lower_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers}


def _registry_bearer_token(challenge: str, basic_auth: str | None) -> str | None:
    if not challenge.lower().startswith("bearer "):
        return None
    params = urllib.request.parse_keqv_list(
        urllib.request.parse_http_list(challenge[7:])
    )
    realm = params.get("realm")
    if not realm:
        return None

    query = {
        key: value
        for key in ("service", "scope")
        if (value := params.get(key))
    }
    token_url = realm
    if query:
        token_url = f"{realm}?{urllib.parse.urlencode(query)}"
    headers = {"Accept": "application/json"}
    if basic_auth:
        headers["Authorization"] = f"Basic {basic_auth}"

    status = 0
    response_headers: dict[str, str] = {}
    payload: object = None
    for attempt in range(_REGISTRY_RETRY_ATTEMPTS):
        status, response_headers, payload = _registry_token_request(
            token_url,
            headers,
        )
        if not _registry_status_is_transient(status):
            break
        if attempt + 1 < _REGISTRY_RETRY_ATTEMPTS:
            _registry_backoff(attempt, response_headers)
    if _registry_status_is_transient(status):
        raise ConfigError("registry token service unavailable")
    if status != 200 or not isinstance(payload, dict):
        return None
    token = payload.get("token") or payload.get("access_token")
    return token if isinstance(token, str) and token else None


def _registry_token_request(
    url: str,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], object]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_REGISTRY_TIMEOUT) as response:
            try:
                payload = json.loads(response.read().decode("utf-8"))
            except ValueError:
                payload = None
            return response.status, _lower_headers(response.headers.items()), payload
    except urllib.error.HTTPError as exc:
        response_headers = (
            _lower_headers(exc.headers.items())
            if exc.headers is not None
            else {}
        )
        return exc.code, response_headers, None
    except OSError:
        return 0, {}, None


def _cached_registry_bearer_token(
    challenge: str,
    basic_auth: str | None,
) -> str | None:
    key = (challenge, basic_auth or "")
    with _registry_bearer_tokens_lock:
        if token := _registry_bearer_tokens.get(key):
            return token
        token_lock = _registry_bearer_token_locks.setdefault(key, threading.Lock())

    with token_lock:
        with _registry_bearer_tokens_lock:
            if token := _registry_bearer_tokens.get(key):
                return token
        token = _registry_bearer_token(challenge, basic_auth)
        if token:
            with _registry_bearer_tokens_lock:
                _registry_bearer_tokens[key] = token
        return token


def _split_remote_image(remote: str) -> tuple[str, str, str]:
    registry, path = remote.split("/", 1)
    repository, reference = path.rsplit(":", 1)
    return registry, repository, reference


def _registry_basic_auth(registry: str) -> str | None:
    for auth_file in _registry_auth_files():
        try:
            data = json.loads(auth_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        auths = data.get("auths")
        if not isinstance(auths, dict):
            continue
        for key, entry in auths.items():
            if (
                not isinstance(key, str)
                or not _auth_key_matches_registry(key, registry)
            ):
                continue
            if not isinstance(entry, dict):
                continue
            auth = entry.get("auth")
            if isinstance(auth, str) and auth:
                return auth
            username = entry.get("username")
            password = entry.get("password")
            if isinstance(username, str) and isinstance(password, str):
                return base64.b64encode(f"{username}:{password}".encode()).decode()
    return None


def _registry_auth_files() -> tuple[Path, ...]:
    paths = []
    if auth_file := os.environ.get("REGISTRY_AUTH_FILE"):
        paths.append(Path(auth_file).expanduser())
    if runtime_dir := os.environ.get("XDG_RUNTIME_DIR"):
        paths.append(Path(runtime_dir) / "containers" / "auth.json")
    paths.append(Path.home() / ".config" / "containers" / "auth.json")
    if docker_config := os.environ.get("DOCKER_CONFIG"):
        paths.append(Path(docker_config).expanduser() / "config.json")
    paths.append(Path.home() / ".docker" / "config.json")
    return tuple(dict.fromkeys(paths))


def _auth_key_matches_registry(key: str, registry: str) -> bool:
    parsed = urllib.parse.urlsplit(key)
    if parsed.netloc:
        key = parsed.netloc + parsed.path
    key = key.strip().rstrip("/")
    if key.endswith("/v1"):
        key = key[:-3].rstrip("/")
    return key == registry


def _require_buildah(buildah: str | None) -> str:
    if buildah is None:
        raise ConfigError("buildah must be installed to create card/build output images")
    return buildah


def _create_orchestrator_image(
    *,
    podman: str,
    buildah: str | None,
    source: str,
    image: str,
    packages: tuple[str, ...],
) -> None:
    returncode, _output = _run_streamed_command([podman, "pull", source])
    if returncode != 0:
        raise ConfigError(f"failed to pull orchestrator image: {source}")

    if not packages:
        subprocess.run([podman, "tag", source, image], check=True)
        subprocess.run([podman, "tag", image, _latest_image(image)], check=True)
        return

    package_args = " ".join(shlex.quote(package) for package in packages)
    buildah = _require_buildah(buildah)
    buildah_command = shlex.quote(buildah)
    script = "\n".join(
        [
            "set -eu",
            "container=",
            "mounted=0",
            'cleanup() {',
            '  if [ "$mounted" = 1 ]; then '
            f"{buildah_command} unmount \"$container\" >/dev/null 2>&1 || true; fi",
            '  if [ -n "$container" ]; then '
            f"{buildah_command} rm \"$container\" >/dev/null 2>&1 || true; fi",
            "}",
            "trap cleanup EXIT INT TERM",
            f"container=$({buildah_command} from --quiet {shlex.quote(source)})",
            f"mount_path=$({buildah_command} mount \"$container\")",
            "mounted=1",
            _shell_command(
                [
                    podman,
                    "run",
                    "--rm",
                    "--volume",
                    "$mount_path:/target",
                    source,
                    "dnf5",
                    "-y",
                    "--installroot=/target",
                    "--setopt=install_weak_deps=False",
                    "install",
                    "--allowerasing",
                ],
                raw_suffix=f" {package_args}",
            ),
            'rm -rf "$mount_path/var/cache/dnf" "$mount_path/var/cache/libdnf5"',
            'find "$mount_path/var/log" -maxdepth 1 -name "dnf*" '
            "-exec rm -rf {} + 2>/dev/null || true",
            f"{buildah_command} unmount \"$container\" >/dev/null",
            "mounted=0",
            f"{buildah_command} commit --rm --quiet --format oci \"$container\" {shlex.quote(image)} >/dev/null",
            "container=",
        ]
    )
    returncode, _output = _run_streamed_command(
        [buildah, "unshare", "/bin/sh", "-s"],
        input_text=script + "\n",
    )
    if returncode != 0:
        raise ConfigError(
            f"orchestrator image build failed with exit status {returncode}"
        )
    subprocess.run([podman, "tag", image, _latest_image(image)], check=True)


def _repo_id(rendered_repo: str, source: Path) -> str:
    for line in rendered_repo.splitlines():
        match = re.fullmatch(r"\[([^]]+)]", line.strip())
        if match:
            return match.group(1)
    raise ConfigError(f"{source}: repository definition does not contain a repo id")


def _apply_repo_priority(repo_file: Path, priority: int) -> None:
    lines = [
        line
        for line in repo_file.read_text(encoding="utf-8").rstrip().splitlines()
        if not line.strip().startswith("priority=")
    ]
    lines.append(f"priority={priority}")
    repo_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_deps(card_build_deps: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(card_build_deps))


def _create_repo_image(
    *,
    podman: str,
    buildah: str,
    orchestrator: str,
    root_dir: Path,
    image: str,
    repo_name: str,
    repo_id: str,
    rendered_repo: str,
) -> None:
    body = [
        'mkdir -p "$mount_path/repos" "$mount_path/cache" "$mount_path/persist"',
        f"printf %s {shlex.quote(rendered_repo)} > \"$mount_path/repos/{shlex.quote(repo_name)}\"",
        "log_dir=$(mktemp -d)",
        'cleanup_dirs="$log_dir"',
        _shell_command(
            [
                podman,
                "run",
                "--rm",
                "--volume",
                f"{root_dir / 'repos'}:/workspace/repos:ro",
                "--volume",
                "$mount_path/repos:/ludos/dnf/repos:ro",
                "--volume",
                "$mount_path/cache:/ludos/dnf/cache",
                "--volume",
                "$mount_path/persist:/ludos/dnf/persist",
                "--volume",
                "$log_dir:/ludos/dnf/log",
                "--workdir",
                "/workspace/repos",
                orchestrator,
                "dnf5",
                "--setopt=reposdir=/ludos/dnf/repos",
                "--setopt=cachedir=/ludos/dnf/cache",
                "--setopt=system_cachedir=/ludos/dnf/cache",
                "--setopt=persistdir=/ludos/dnf/persist",
                "--setopt=logdir=/ludos/dnf/log",
                "--disable-repo=*",
                f"--enable-repo={repo_id}",
                "makecache",
                "--refresh",
            ],
        ),
    ]
    _create_scratch_image(buildah=buildah, image=image, body=body)


def _extract_image_paths(
    podman: str, image: str, paths: dict[str, Path]
) -> None:
    container = subprocess.run(
        [podman, "create", image, "true"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    try:
        for source_name, destination in paths.items():
            destination.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    podman,
                    "cp",
                    f"{container}:/{source_name}/.",
                    str(destination),
                ],
                check=True,
            )
    finally:
        subprocess.run([podman, "rm", container], check=True, stdout=subprocess.DEVNULL)


def _shell_command(command: list[str], raw_suffix: str = "") -> str:
    return " ".join(_shell_arg(str(part)) for part in command) + raw_suffix


def _shell_arg(value: str) -> str:
    if value.startswith("$") or value.startswith('"$'):
        return value
    return shlex.quote(value)


def _create_scratch_image(*, buildah: str, image: str, body: list[str]) -> None:
    buildah_command = shlex.quote(buildah)
    script = "\n".join(
        [
            "set -eu",
            "container=",
            "mounted=0",
            "cleanup_dirs=",
            "cleanup() {",
            '  if [ "$mounted" = 1 ]; then '
            f"{buildah_command} unmount \"$container\" >/dev/null 2>&1 || true; fi",
            '  if [ -n "$container" ]; then '
            f"{buildah_command} rm \"$container\" >/dev/null 2>&1 || true; fi",
            '  if [ -n "$cleanup_dirs" ]; then rm -rf $cleanup_dirs; fi',
            "}",
            "trap cleanup EXIT INT TERM",
            f"container=$({buildah_command} from --quiet scratch)",
            f"mount_path=$({buildah_command} mount \"$container\")",
            "mounted=1",
            *body,
            f"{buildah_command} unmount \"$container\" >/dev/null",
            "mounted=0",
            f"{buildah_command} commit --rm --quiet --format oci \"$container\" {shlex.quote(image)} >/dev/null",
            "container=",
        ]
    )
    returncode, _output = _run_streamed_command(
        [buildah, "unshare", "/bin/sh", "-s"],
        input_text=script + "\n",
    )
    if returncode != 0:
        raise ConfigError(f"scratch image build failed with exit status {returncode}")


def _run_streamed_command(
    command: list[str],
    input_text: str | None = None,
    line_rewriter: Callable[[str], str] | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    output_lines = []
    try:
        if input_text is not None:
            assert process.stdin is not None
            process.stdin.write(input_text)
            process.stdin.close()

        assert process.stdout is not None
        for line in process.stdout:
            output_lines.append(line)
            if line_rewriter is not None:
                line = line_rewriter(line)
            stream(line)

        return process.wait(), "".join(output_lines)
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            _terminate_process_group(process)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os_pgid = process.pid
        signal_term = signal.SIGTERM
        signal_kill = signal.SIGKILL
        os.killpg(os_pgid, signal_term)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os_pgid, signal_kill)
            process.wait(timeout=5)
    except ProcessLookupError:
        pass


def _cache_name(value: str, description: str) -> str:
    if "/" in value or value in ("", ".", ".."):
        raise ConfigError(f"invalid {description} cache name '{value}'")
    return value


def _default_cache_version(
    now: _datetime.datetime | None = None,
) -> str:
    if now is None:
        now = _datetime.datetime.now(_datetime.UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_datetime.UTC)
    else:
        now = now.astimezone(_datetime.UTC)
    iso_day = now.isoweekday()
    monday = now.date() - _datetime.timedelta(days=iso_day - 1)
    return monday.strftime("%Y%m%d")


def _substitute_variables(value: str, variables: dict[str, str]) -> str:
    for key, replacement in variables.items():
        value = value.replace(f"${key}", replacement)
    return value


def _package_hash(packages: tuple[str, ...]) -> str:
    payload = "\n".join(sorted(packages)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]

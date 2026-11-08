"""Delete stale intermediate package versions using GHCR's GitHub API."""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import quote

from .logging import log
from .model import ConfigError


GHCR_CLEANUP_REPOSITORIES = ("orchestrator", "repos", "cards", "builders", "builds")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def validate_ghcr_registry(registry: str) -> str:
    registry = registry.strip().rstrip("/")
    if not re.fullmatch(r"ghcr\.io/[A-Za-z0-9_-]+(?:/[a-z0-9._-]+)*", registry):
        raise ConfigError("cleanup --ci ghcr requires ci.registry = ghcr.io/OWNER[/PREFIX]")
    if any(part in (".", "..") for part in registry.split("/")):
        raise ConfigError("invalid GHCR registry path")
    return registry


def _gh_api(endpoint: str, *, method: str = "GET", missing_ok: bool = False) -> object:
    result = subprocess.run(
        ["gh", "api", "--hostname", "github.com", "--include", "--method", method, endpoint],
        check=False,
        text=True,
        capture_output=True,
    )
    # --include exposes the HTTP status so only an actual 404 is treated as absent.
    parts = re.split(r"\r?\n\r?\n", result.stdout, maxsplit=1)
    status = re.match(r"HTTP/\S+\s+(\d+)", parts[0])
    code = int(status[1]) if status else 0
    if missing_ok and code == 404:
        return None
    if result.returncode != 0 or not 200 <= code < 300:
        raise ConfigError(
            f"GHCR API {method} {endpoint} failed (HTTP {code or 'unknown'}): "
            f"{result.stderr.strip()}"
        )
    body = parts[1] if len(parts) == 2 else ""
    if code == 204:
        return None
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ConfigError(f"Invalid GHCR API response for {endpoint}") from exc


def _package_versions(endpoint: str) -> list[dict]:
    versions: list[dict] = []
    page = 1
    while True:
        entries = _gh_api(
            f"{endpoint}/versions?per_page=100&page={page}", missing_ok=page == 1
        )
        if entries is None:
            return []
        if not isinstance(entries, list):
            raise ConfigError(f"Invalid GHCR versions list for {endpoint}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ConfigError(f"Invalid GHCR package version for {endpoint}")
            metadata = entry.get("metadata")
            container = metadata.get("container") if isinstance(metadata, dict) else None
            tags = container.get("tags") if isinstance(container, dict) else None
            if (
                type(entry.get("id")) is not int
                or entry["id"] <= 0
                or not isinstance(entry.get("name"), str)
                or not _DIGEST_RE.fullmatch(entry["name"])
                or not isinstance(tags, list)
                or not all(isinstance(tag, str) and tag for tag in tags)
            ):
                raise ConfigError(f"Invalid GHCR package version for {endpoint}")
            versions.append(entry)
        if len(entries) < 100:
            return versions
        page += 1


def _referenced_manifests(repository: str, roots: set[str]) -> set[str]:
    """Keep untagged children of retained indexes, including nested indexes."""
    referenced: set[str] = set()
    pending = list(roots)
    while pending:
        digest = pending.pop()
        if digest in referenced:
            continue
        referenced.add(digest)
        result = subprocess.run(
            ["skopeo", "inspect", "--raw", f"docker://{repository}@{digest}"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise ConfigError(f"Failed to inspect retained GHCR manifest {repository}@{digest}")
        try:
            manifest = json.loads(result.stdout)
        except ValueError as exc:
            raise ConfigError(f"Invalid GHCR manifest {repository}@{digest}") from exc
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
            raise ConfigError(f"Invalid GHCR manifest {repository}@{digest}")
        children = manifest.get("manifests", [])
        if not isinstance(children, list):
            raise ConfigError(f"Invalid GHCR image index {repository}@{digest}")
        for child in children:
            child_digest = child.get("digest") if isinstance(child, dict) else None
            if not isinstance(child_digest, str) or not _DIGEST_RE.fullmatch(child_digest):
                raise ConfigError(f"Invalid GHCR image index descriptor {repository}@{digest}")
            pending.append(child_digest)
    return referenced


def cleanup_ghcr_images(
    registry: str,
    keep_refs: set[str],
    *,
    version: str,
    local_prefix: str = "",
    dry_run: bool = False,
) -> int:
    registry = validate_ghcr_registry(registry)
    _, owner, *package_prefix = registry.split("/")
    account = _gh_api(f"/users/{quote(owner, safe='')}")
    if not isinstance(account, dict) or account.get("type") not in ("Organization", "User"):
        raise ConfigError(f"Cannot determine GHCR account type for {owner}")
    account_path = "orgs" if account["type"] == "Organization" else "users"
    removals: list[tuple[str, dict]] = []
    # Finish inventory and reachability checks for every package before deleting.
    for repository in GHCR_CLEANUP_REPOSITORIES:
        local_repository = f"{local_prefix}{repository}"
        package = "/".join((*package_prefix, local_repository))
        endpoint = f"/{account_path}/{quote(owner, safe='')}/packages/container/{quote(package, safe='')}"
        versions = _package_versions(endpoint)
        roots = {
            entry["name"]
            for entry in versions
            if any(
                f"{local_repository}:{tag}" in keep_refs
                or (repository in ("orchestrator", "repos") and tag.endswith(f"-{version}"))
                or (repository == "orchestrator" and tag == "latest")
                for tag in entry["metadata"]["container"]["tags"]
            )
        }
        if all(entry["name"] in roots for entry in versions):
            continue
        referenced = _referenced_manifests(f"ghcr.io/{owner}/{package}", roots)
        removals.extend(
            (endpoint, entry) for entry in versions if entry["name"] not in referenced
        )

    for endpoint, entry in removals:
        tags = ", ".join(entry["metadata"]["container"]["tags"]) or "untagged"
        label = f"{endpoint} version {entry['id']} ({tags})"
        if dry_run:
            log(f"Would remove GHCR image: {label}")
        else:
            log(f"Removing GHCR image: {label}")
            _gh_api(f"{endpoint}/versions/{entry['id']}", method="DELETE")
    log(f"{'Would remove' if dry_run else 'Removed'} {len(removals)} stale GHCR versions")
    return 0

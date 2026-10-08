"""Verify downloaded RPMs against the keys of their configured repositories."""

from __future__ import annotations

import base64
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .model import ConfigError


DNF_REPO_OPTIONS = (
    "--setopt=reposdir=/ludos/dnf/repos",
    "--setopt=cachedir=/ludos/dnf/cache",
    "--setopt=system_cachedir=/ludos/dnf/cache",
    "--setopt=persistdir=/ludos/dnf/persist",
    "--setopt=logdir=/ludos/dnf/log",
    "--disable-repo=*",
    "--enable-repo=*",
)


@dataclass(frozen=True)
class RepositoryRpm:
    nevra: str
    repository: str
    filename: str


def _canonical_nevra(value: str) -> str:
    try:
        name, version, release_arch = value.rsplit("-", 2)
    except ValueError as exc:
        raise ConfigError(f"invalid exact package NEVRA: {value}") from exc
    if ":" not in version:
        version = "0:" + version
    return f"{name}-{version}-{release_arch}"


def repository_rpms(dnf_base: list[str], packages: tuple[str, ...]) -> tuple[RepositoryRpm, ...]:
    if not packages:
        return ()
    result = subprocess.run(
        [*dnf_base, *DNF_REPO_OPTIONS, "repoquery", "--available",
         "--queryformat=%{full_nevra}\t%{repoid}\t%{location}", *packages],
        check=True, text=True, capture_output=True,
    )
    candidates: dict[str, set[RepositoryRpm]] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise ConfigError("invalid repository RPM query output")
        nevra, repo, location = fields
        filename = urllib.parse.unquote(urllib.parse.urlsplit(location).path.rsplit("/", 1)[-1])
        if not repo or not filename.endswith(".rpm") or "/" in filename or "\\" in filename:
            raise ConfigError(f"invalid RPM location for {nevra} from repository {repo}")
        record = RepositoryRpm(_canonical_nevra(nevra), repo, filename)
        candidates.setdefault(record.nevra, set()).add(record)
    records = []
    filenames: set[str] = set()
    for package in dict.fromkeys(packages):
        matches = candidates.get(_canonical_nevra(package), set())
        if len(matches) != 1:
            raise ConfigError(f"expected one repository RPM for {package}, found {len(matches)}")
        record = next(iter(matches))
        if record.filename in filenames:
            raise ConfigError(f"ambiguous repository RPM filename: {record.filename}")
        filenames.add(record.filename)
        records.append(record)
    return tuple(records)


def _repository_settings(output: str) -> dict[str, dict[str, str]]:
    repositories: dict[str, dict[str, str]] = {}
    current = None
    for line in output.splitlines():
        header = re.fullmatch(r'=+ "(.+)" repository configuration: =+', line.strip())
        if header:
            current = repositories.setdefault(header[1], {})
        elif current is not None:
            key, separator, value = line.partition(" = ")
            # Avoid retaining credentials from the configuration dump.
            if separator and key in ("pkg_gpgcheck", "gpgcheck", "gpgkey"):
                current[key] = value.strip()
    return repositories


def _requires_signature(settings: dict[str, str], repository: str) -> bool:
    value = settings.get("pkg_gpgcheck", settings.get("gpgcheck", "")).lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"missing or invalid GPG policy for repository {repository}")


def _container_command(dnf_base: list[str]) -> list[str]:
    if not dnf_base or dnf_base[-1] != "dnf5":
        raise ConfigError("repository verification requires an orchestrator DNF5 command")
    command = dnf_base[:-1]
    try:
        command.insert(command.index("run") + 1, "--interactive")
    except ValueError:
        raise ConfigError("repository verification requires a Podman run command") from None
    return command


def _cache_root(dnf_base: list[str], destdir: str) -> Path:
    for argument in dnf_base:
        source, separator, target = argument.partition(":")
        if separator and target.split(":", 1)[0] == destdir:
            return Path(source)
    raise ConfigError(f"repository RPM cache is not mounted at {destdir}")


def _key_import_lines(keys: str, repository: str) -> list[str]:
    urls = keys.replace(",", " ").split()
    if not urls:
        raise ConfigError(f"no GPG keys configured for repository {repository}")
    lines = []
    for index, url in enumerate(urls):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "file":
            if parsed.netloc not in ("", ".", "localhost"):
                raise ConfigError(f"unsupported GPG key file URL for repository {repository}")
            path = urllib.parse.unquote(parsed.path)
            if parsed.netloc == ".":
                path = "." + path
            lines.append(f'cp -- {shlex.quote(path)} "$verify_root/key-{index}"')
        elif parsed.scheme in ("http", "https"):
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    # Keys are small; do not buffer unbounded remote content.
                    key = response.read(4 * 1024 * 1024 + 1)
                if len(key) > 4 * 1024 * 1024:
                    raise ValueError("GPG key exceeds size limit")
            except Exception:
                raise ConfigError(f"failed to retrieve GPG key for repository {repository}") from None
            encoded = base64.b64encode(key).decode("ascii")
            lines.append(f'printf %s {shlex.quote(encoded)} | base64 -d > "$verify_root/key-{index}"')
        else:
            raise ConfigError(f"unsupported GPG key URL for repository {repository}")
        lines.append(f'rpmkeys --root "$verify_root" --define "_keyring rpmdb" --import "$verify_root/key-{index}"')
    return lines


def verify_repository_rpms(
    dnf_base: list[str], packages: tuple[str, ...], destdir: str = "/ludos/packages",
) -> None:
    if not packages:
        return
    records = repository_rpms(dnf_base, packages)
    config = subprocess.run(
        [*dnf_base, *DNF_REPO_OPTIONS, "--dump-repo-config=*"],
        check=True, text=True, capture_output=True,
    )
    settings = _repository_settings(config.stdout)
    cache_root = _cache_root(dnf_base, destdir)
    cached_files: dict[str, list[Path]] = {}
    wanted = {record.filename for record in records}
    for path in cache_root.rglob("*.rpm"):
        if path.name in wanted:
            cached_files.setdefault(path.name, []).append(path)
    by_repo: dict[str, list[RepositoryRpm]] = {}
    for record in records:
        by_repo.setdefault(record.repository, []).append(record)
    for repository, rpms in by_repo.items():
        if repository not in settings:
            raise ConfigError(f"missing configuration for repository {repository}")
        required = _requires_signature(settings[repository], repository)
        lines = [
            "set -eu",
            "mkdir -p /ludos/dnf/persist",
            "verify_root=$(mktemp -d /ludos/dnf/persist/rpm-verify.XXXXXXXX)",
            'trap \'rm -rf -- "$verify_root"\' EXIT',
            'rpm --root "$verify_root" --initdb',
        ]
        if required:
            lines.extend(_key_import_lines(settings[repository].get("gpgkey", ""), repository))
        for record in rpms:
            matches = cached_files.get(record.filename, [])
            if len(matches) != 1 or not matches[0].is_file():
                raise ConfigError(f"expected one cached RPM for {record.nevra} from repository {repository}")
            rpm_path = shlex.quote(f"{destdir}/{matches[0].relative_to(cache_root).as_posix()}")
            expected = shlex.quote(record.nevra)
            failure = shlex.quote(f"RPM verification failed for {record.nevra} from repository {repository}")
            # Query without signature enforcement, then explicitly require signatures
            # with the isolated keyring when the repository policy enables them.
            queryformat = shlex.quote("%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}")
            check = f'[ "$(rpm -qp --nosignature --queryformat {queryformat} {rpm_path})" = {expected} ]'
            if required:
                check += f' && rpmkeys --root "$verify_root" --define "_keyring rpmdb" --define "_pkgverify_level all" --checksig {rpm_path}'
            else:
                check += f' && rpmkeys --root "$verify_root" --define "_pkgverify_level digest" --nosignature --checksig {rpm_path}'
            lines.extend([
                f"if {check}; then :; else",
                f"  rm -f -- {rpm_path}",
                f"  printf '%s\n' {failure} >&2",
                "  exit 1",
                "fi",
            ])
        result = subprocess.run(
            [*_container_command(dnf_base), "sh", "-s"],
            input="\n".join(lines), text=True, capture_output=True,
        )
        if result.returncode:
            # Do not echo key URLs or subprocess output, which can contain credentials.
            failures = {
                f"RPM verification failed for {record.nevra} from repository {repository}"
                for record in rpms
            }
            for line in result.stderr.splitlines():
                if line in failures:
                    raise ConfigError(line)
            raise ConfigError(f"could not prepare RPM verification for repository {repository}")

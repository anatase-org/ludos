from __future__ import annotations

import argparse
import glob
import grp
import os
import pwd
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


# This projection intentionally leaves mutable /var content out of the commit.
# rpm-ostree generates /var structure from tmpfiles; if we later need factory
# defaults, compare against the reference image before copying /var/lib into
# /usr/lib or /var into /usr/share/factory/var.
KNOWN_STATE_FILES = {
    "var/lib/systemd/random-seed",
    "var/lib/systemd/catalog/database",
    "var/lib/plymouth/boot-duration",
    "var/log/wtmp",
    "var/log/btmp",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project a container rootfs into an rpm-ostree-style commit.",
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("repo", type=Path)
    parser.add_argument("ostree_ref")
    parser.add_argument(
        "--progress-total-prefix",
        default="__LUDOS_OSTREE_APPROX_TOTAL__ ",
    )
    args = parser.parse_args(argv)

    return import_processed_rootfs(
        args.source,
        args.repo,
        args.ostree_ref,
        progress_total_prefix=args.progress_total_prefix,
    )


def import_processed_rootfs(
    source: Path,
    repo: Path,
    ostree_ref: str,
    *,
    progress_total_prefix: str,
) -> int:
    source = source.resolve()
    repo = repo.resolve()

    if not (repo / "objects").is_dir():
        subprocess.run(["ostree", f"--repo={repo}", "init", "--mode=bare-user"], check=True)
        subprocess.run(
            ["ostree", f"--repo={repo}", "config", "set", "core.fsync", "false"],
            check=True,
        )

    producers: list[subprocess.Popen[bytes]] = []
    with tempfile.TemporaryDirectory(prefix="ludos-ostree-import.") as work_text:
        work = Path(work_text)
        base = work / "base"
        final = work / "final"
        _prepare_projection(source, base, final)

        tar_trees: list[str] = []
        fifo_index = 0

        def start_tar(args: list[str]) -> None:
            nonlocal fifo_index
            fifo = work / f"tree-{fifo_index}.tar"
            fifo_index += 1
            os.mkfifo(fifo)
            cmd = f"exec {shlex.join(args)} > {shlex.quote(str(fifo))}"
            producers.append(subprocess.Popen(["/bin/sh", "-c", cmd]))
            tar_trees.append(f"--tree=tar={fifo}")

        start_tar(_tar_args(base, "."))

        if (source / "usr").exists():
            start_tar(
                _tar_args(
                    source,
                    "usr",
                    excludes=(
                        # Build IDs are useful locally but churn between builds
                        # and act as a cache buster for our generated commits.
                        "usr/lib/.build-id/*",
                        "usr/lib/sysimage/rpm",
                        "usr/lib/sysimage/rpm/*",
                        "usr/local",
                        "usr/local/*",
                        "usr/lib/ostree-boot/loader",
                        "usr/lib/ostree-boot/loader/*",
                    ),
                )
            )

        if (source / "etc").exists():
            start_tar(
                _tar_args(
                    source,
                    "etc",
                    excludes=(
                        # Those lock files can cause some pain
                        "etc/.pwd.lock",
                        "etc/passwd-",
                        "etc/group-",
                        "etc/shadow-",
                        "etc/gshadow-",
                        "etc/subuid-",
                        "etc/subgid-",
                        # We recreate it as empty
                        "etc/machine-id",
                    ),
                    transform="s,^etc,usr/etc,",
                )
            )

        # Reference rpm-ostree images keep top-level /boot empty in the commit.
        # Boot assets from container builds are runtime/deployment state here, so
        # we synthesize the empty directory in the base projection instead of
        # importing source /boot.
        rpmdb_source = _rpmdb_source(source)
        if rpmdb_source is not None:
            rpmdb_parent, rpmdb_name, writes_usr_share = rpmdb_source
            if writes_usr_share:
                start_tar(
                    _tar_args(
                        rpmdb_parent,
                        rpmdb_name,
                        transform=f"s,^{rpmdb_name},usr/share/rpm,",
                    )
                )
            start_tar(
                _tar_args(
                    rpmdb_parent,
                    rpmdb_name,
                    transform=f"s,^{rpmdb_name},usr/lib/sysimage/rpm-ostree-base-db,",
                )
            )

        start_tar(_tar_args(final, "."))

        approx_entries = _approx_entries(source, base, final)
        print(
            f"{progress_total_prefix}{approx_entries * 2 + 1}",
            file=sys.stderr,
            flush=True,
        )
        result = _run_ostree_commit(repo, ostree_ref, source, tar_trees)
        if result.returncode != 0:
            _kill_producers(producers)
        producer_status = _wait_for_producers(producers)

    if result.returncode != 0:
        return result.returncode
    if producer_status != 0:
        return producer_status
    sys.stdout.write(result.stdout)
    return 0


def _tar_args(
    directory: Path,
    path: str,
    *,
    excludes: tuple[str, ...] = (),
    transform: str | None = None,
) -> list[str]:
    args = [
        "tar",
        "--xattrs",
        # Added by the container engine
        "--xattrs-exclude=user.overlay.impure",
        "--acls",
        "--selinux",
        "--numeric-owner",
        "-C",
        str(directory),
    ]
    for exclude in excludes:
        args.append(f"--exclude={exclude}")
    if transform is not None:
        args.append(f"--transform={transform}")
    args.extend(["-cf", "-", path])
    return args


def _run_ostree_commit(
    repo: Path,
    ostree_ref: str,
    source: Path,
    tar_trees: list[str],
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("G_MESSAGES_DEBUG", None)
    return subprocess.run(
        [
            "ostree",
            f"--repo={repo}",
            "commit",
            "-v",
            "-b",
            ostree_ref,
            "--tar-autocreate-parents",
            *tar_trees,
            "--bootable",
            f"--selinux-policy={source}",
            "--selinux-labeling-epoch=1",
        ],
        check=False,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )


def _kill_producers(producers: list[subprocess.Popen[bytes]]) -> None:
    for producer in producers:
        if producer.poll() is None:
            producer.kill()


def _wait_for_producers(producers: list[subprocess.Popen[bytes]]) -> int:
    status = 0
    for producer in producers:
        returncode = producer.wait()
        if returncode != 0 and status == 0:
            status = returncode
    return status


def _rpmdb_source(source: Path) -> tuple[Path, str, bool] | None:
    sysimage_rpm = source / "usr/lib/sysimage/rpm"
    if sysimage_rpm.exists():
        return sysimage_rpm.parent, sysimage_rpm.name, True

    usr_share_rpm = source / "usr/share/rpm"
    if usr_share_rpm.exists():
        return usr_share_rpm.parent, usr_share_rpm.name, False

    var_lib_rpm = source / "var/lib/rpm"
    if var_lib_rpm.exists():
        return var_lib_rpm.parent, var_lib_rpm.name, True

    return None


def _approx_entries(source: Path, base: Path, final: Path) -> int:
    roots = [
        source / "usr",
        source / "etc",
        source / "var",
        base,
        final,
    ]
    total = 0
    for root in roots:
        if not root.exists():
            continue
        for _current_root, dirs, files in os.walk(root):
            total += len(dirs) + len(files)
    return total


def _prepare_projection(source: Path, base: Path, final: Path) -> None:
    _ensure_dir(base)
    for name in ("dev", "proc", "run", "sys", "var", "sysroot", "boot"):
        _ensure_dir(base / name)
    _ensure_dir(base / "tmp", 0o1777)
    _symlink("var/home", base / "home")
    _symlink("var/roothome", base / "root")
    _symlink("sysroot/ostree", base / "ostree")
    _symlink("var/srv", base / "srv")
    _symlink("var/mnt", base / "mnt")
    _symlink("run/media", base / "media")
    _symlink("var/opt", base / "opt")
    _symlink("../var/usrlocal", base / "usr/local")
    _symlink("../../usr/share/rpm", base / "var/lib/rpm")
    _symlink("../../share/rpm", base / "usr/lib/sysimage/rpm")

    _write(final / "usr/lib/rpm/macros.d/macros.rpm-ostree", "%_dbpath /usr/share/rpm\n")
    _write(final / "usr/etc/machine-id", "")
    _write(
        final / "usr/lib/tmpfiles.d/rpm-ostree-0-integration.conf",
        "d /var/home 0755 root root -\n"
        "d /var/srv 0755 root root -\n"
        "d /var/roothome 0700 root root -\n"
        "d /var/mnt 0755 root root -\n"
        "d /run/media 0755 root root -\n"
        "L /var/lib/rpm - - - - ../../usr/share/rpm\n"
        "L /var/lib/selinux - - - - ../../etc/selinux\n",
    )
    _write(
        final / "usr/lib/tmpfiles.d/rpm-ostree-0-integration-opt-usrlocal.conf",
        "d /usr/local/bin 0755 root root -\n"
        "d /usr/local/etc 0755 root root -\n"
        "d /usr/local/games 0755 root root -\n"
        "d /usr/local/include 0755 root root -\n"
        "d /usr/local/lib 0755 root root -\n"
        "d /usr/local/sbin 0755 root root -\n"
        "d /usr/local/share 0755 root root -\n"
        "d /usr/local/src 0755 root root -\n"
        "d /var/opt 0755 root root -\n"
        "d /var/usrlocal 0755 root root -\n",
    )

    var_tmpfiles = _collect_var_tmpfiles(source)
    if var_tmpfiles:
        _write(final / "usr/lib/tmpfiles.d/rpm-ostree-1-autovar.conf", var_tmpfiles)

    nsswitch = _read_text(source / "etc/nsswitch.conf")
    if nsswitch is not None:
        _write(final / "usr/etc/nsswitch.conf", _add_altfiles(nsswitch))

    useradd = _read_text(source / "etc/default/useradd")
    if useradd is not None:
        _write(final / "usr/etc/default/useradd", _update_useradd(useradd))

    passwd_split = _split_colon_file(source / "etc/passwd")
    if passwd_split is not None:
        _write(final / "usr/etc/passwd", passwd_split[0])
        _write(final / "usr/lib/passwd", passwd_split[1])

    group_split = _split_colon_file(source / "etc/group", ("root", "wheel"))
    if group_split is not None:
        _write(final / "usr/etc/group", group_split[0])
        _write(final / "usr/lib/group", group_split[1])

    for path in glob.glob(str(source / "etc/selinux/*/contexts/files/file_contexts.subs_dist")):
        src = Path(path)
        rel = src.relative_to(source / "etc")
        contents = _read_text(src)
        if contents is not None:
            _write(final / "usr/etc" / rel, _update_subs_dist(contents))


def _ensure_dir(path: Path, mode: int = 0o755) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    _ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _symlink(target: str, path: Path) -> None:
    _ensure_dir(path.parent)
    if path.exists() or path.is_symlink():
        path.unlink()
    path.symlink_to(target)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def _tmpfiles_path(path: str) -> str:
    if path.startswith("/var/run/"):
        return path.removeprefix("/var")
    return path


def _tmpfiles_line(source: Path, path: Path) -> str | None:
    rel = path.relative_to(source)
    rel_text = rel.as_posix()
    if rel_text in KNOWN_STATE_FILES:
        return None
    if rel_text == "var/run" or rel_text.startswith("var/run/"):
        return None
    path_stat = path.lstat()
    abs_path = _tmpfiles_path("/" + rel_text)
    if stat.S_ISDIR(path_stat.st_mode):
        return (
            f"d {abs_path} {stat.S_IMODE(path_stat.st_mode):04o} "
            f"{_user_name(path_stat.st_uid)} {_group_name(path_stat.st_gid)} - -"
        )
    if stat.S_ISLNK(path_stat.st_mode):
        return f"L {abs_path} - - - - {os.readlink(path)}"
    if stat.S_ISREG(path_stat.st_mode) and rel_text.startswith("var/lib/nfs/"):
        return (
            f"f {abs_path} {stat.S_IMODE(path_stat.st_mode):04o} "
            f"{_user_name(path_stat.st_uid)} {_group_name(path_stat.st_gid)} - -"
        )
    return None


def _collect_var_tmpfiles(source: Path) -> str:
    var = source / "var"
    if not var.exists():
        return ""
    entries: set[str] = set()
    for root, dirs, files in os.walk(var, topdown=True, followlinks=False):
        root_path = Path(root)
        line = _tmpfiles_line(source, root_path)
        if line:
            entries.add(line)
        for name in files:
            child = root_path / name
            line = _tmpfiles_line(source, child)
            if line:
                entries.add(line)
        for name in dirs:
            child = root_path / name
            if child.is_symlink():
                line = _tmpfiles_line(source, child)
                if line:
                    entries.add(line)
    return "".join(f"{entry}\n" for entry in sorted(entries))


def _add_altfiles(contents: str) -> str:
    output: list[str] = []
    for line in contents.splitlines():
        matched = False
        for prefix in ("passwd:", "group:"):
            if not line.startswith(prefix):
                continue
            matched = True
            rest = line[len(prefix) :].split()
            if "altfiles" in rest:
                output.append(line)
                break
            new_rest: list[str] = []
            inserted = False
            for item in rest:
                new_rest.append(item)
                if item == "files" and not inserted:
                    new_rest.append("altfiles")
                    inserted = True
            if not inserted:
                new_rest.append("altfiles")
            output.append(prefix + " " + " ".join(new_rest))
            break
        if not matched:
            output.append(line)
    return "\n".join(output) + "\n"


def _update_useradd(contents: str) -> str:
    output: list[str] = []
    changed = False
    for line in contents.splitlines():
        if line.startswith("HOME="):
            output.append("HOME=/var/home")
            changed = True
        else:
            output.append(line)
    if not changed:
        output.append("HOME=/var/home")
    return "\n".join(output) + "\n"


def _split_colon_file(path: Path, root_names: tuple[str, ...] = ("root",)) -> tuple[str, str] | None:
    contents = _read_text(path)
    if contents is None:
        return None
    root_lines: list[str] = []
    alt_lines: list[str] = []
    for line in contents.splitlines():
        if not line or line.startswith("#"):
            root_lines.append(line)
            continue
        name = line.split(":", 1)[0]
        if name in root_names:
            root_lines.append(line)
        else:
            alt_lines.append(line)
    return "\n".join(root_lines) + "\n", "\n".join(alt_lines) + "\n"


def _update_subs_dist(contents: str) -> str:
    output: list[str] = []
    has_usr_etc = False
    etc_aliases: list[str] = []
    for line in contents.splitlines():
        if line.startswith("/var/home "):
            output.append("# https://github.com/projectatomic/rpm-ostree/pull/1754")
            output.append("# " + line)
            continue
        if line.startswith("/usr/etc "):
            has_usr_etc = True
        if line.startswith("/etc/"):
            etc_aliases.append(line)
        output.append(line)
    output.append("# https://github.com/projectatomic/rpm-ostree/pull/1754")
    output.append("/home /var/home")
    if not has_usr_etc:
        output.append("# https://github.com/coreos/rpm-ostree/pull/4640")
        output.append("/usr/etc /etc")
    if etc_aliases:
        output.append("# https://github.com/coreos/rpm-ostree/pull/5485")
        output.extend("/usr" + line for line in etc_aliases)
    output.append("# https://github.com/coreos/rpm-ostree/pull/1795")
    output.append("/usr/lib/opt /opt")
    return "\n".join(output) + "\n"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

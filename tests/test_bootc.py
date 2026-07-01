from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from ludos.__main__ import build_parser
from ludos.bootc import (
    DEFAULT_OSTREE_REF,
    DEFAULT_OCI_WRITERS,
    _bootc_encapsulate_supports_jobs,
    _manifest_artifact_name,
    _export_rechunked_oci,
    _git_revision,
    _oci_export_line_rewriter,
    _parse_commit,
    _parse_progress_total,
    _resolve_chunks_path,
    _safe_oci_name,
    _count_repo_objects,
    _read_oci_export_stderr,
    _read_oci_export_stdout,
    _read_ostree_stderr,
    _update_object_progress,
    _short_digest,
    bootc_create,
    ostree_import,
)
from ludos.model import ConfigError


class BootcCommandTests(unittest.TestCase):
    def test_parser_accepts_bootc_create(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "create",
                "--chunks",
                "custom-chunks.yml",
                "--cache-dir",
                "custom-cache",
                "--cards-dir",
                "custom-cards",
                "--version",
                "20260618",
                "--cache",
                "--ci",
                "--writers",
                "8",
                "--no-ccache",
                "--force",
                "anatase.yml",
                "other.yml",
            ]
        )

        self.assertEqual(args.command, "bootc")
        self.assertEqual(args.bootc_action, "create")
        self.assertEqual(args.chunks, Path("custom-chunks.yml"))
        self.assertEqual(args.cache_dir, Path("custom-cache"))
        self.assertEqual(args.cards_dir, Path("custom-cards"))
        self.assertEqual(args.version, "20260618")
        self.assertTrue(args.cache)
        self.assertTrue(args.ci)
        self.assertEqual(args.writers, 8)
        self.assertTrue(args.no_ccache)
        self.assertTrue(args.force)
        self.assertEqual(args.manifests, [Path("anatase.yml"), Path("other.yml")])

    def test_parser_defaults_bootc_create_writers(self) -> None:
        args = build_parser().parse_args(["bootc", "create", "anatase.yml"])

        self.assertEqual(args.writers, DEFAULT_OCI_WRITERS)

    def test_parser_accepts_ostree_import(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "ostree-import",
                "--cache-dir",
                "custom-cache",
                "--orchestrator",
                "localhost/orchestrator:latest",
                "--ostree-ref",
                "anatase",
                "localhost/anatase:latest",
            ]
        )

        self.assertEqual(args.command, "bootc")
        self.assertEqual(args.bootc_action, "ostree-import")
        self.assertEqual(args.cache_dir, Path("custom-cache"))
        self.assertEqual(args.orchestrator, "localhost/orchestrator:latest")
        self.assertEqual(args.ostree_ref, "anatase")
        self.assertFalse(args.no_process)
        self.assertEqual(args.ref, "localhost/anatase:latest")

    def test_parser_accepts_ostree_import_no_process(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "ostree-import",
                "--no-process",
                "localhost/anatase:latest",
            ]
        )

        self.assertTrue(args.no_process)

    def test_parser_defaults_ostree_import_orchestrator_to_ref(self) -> None:
        args = build_parser().parse_args(
            [
                "bootc",
                "ostree-import",
                "localhost/anatase:latest",
            ]
        )

        self.assertIsNone(args.orchestrator)
        self.assertEqual(args.ref, "localhost/anatase:latest")

    def test_resolve_chunks_defaults_next_to_first_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks = root / "chunks.yml"
            chunks.write_text("version: 1\nmeta: {}\n", encoding="utf-8")

            self.assertEqual(
                _resolve_chunks_path((root / "anatase.yml",), None),
                chunks.resolve(),
            )

    def test_resolve_chunks_errors_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ConfigError, "chunks file is missing"):
                _resolve_chunks_path((Path(tmp) / "anatase.yml",), None)

    def test_bootc_create_rejects_non_positive_writers(self) -> None:
        with self.assertRaisesRegex(ConfigError, "writers must be at least 1"):
            bootc_create((Path("anatase.yml"),), writers=0)

    def test_safe_oci_name_sanitizes_image_ref(self) -> None:
        self.assertEqual(
            _safe_oci_name("localhost/anatase:f44-x86_64"),
            "anatase-f44-x86_64",
        )
        self.assertEqual(
            _safe_oci_name("registry.example.com/team/anatase:test"),
            "registry.example.com-team-anatase-test",
        )

    def test_manifest_artifact_name_uses_manifest_image_and_distro(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "anatase.yml"
            (root / ".env").write_text("releasever=44\n", encoding="utf-8")
            manifest.write_text(
                "\n".join(
                    [
                        "version: 1",
                        "env:",
                        "  arch: x86_64",
                        "releasever: $releasever",
                        "distro: f$releasever-$arch",
                        "orchestrator: quay.io/fedora/fedora:$releasever",
                        "bootstrap: cards/bootstrap.yml",
                        "repos: []",
                        "cards:",
                        "  - cards/base/kernel",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                _manifest_artifact_name(manifest),
                "anatase-f44-x86_64",
            )

    def test_bootc_create_builds_imports_rechunks_and_exports_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_a = root / "anatase.yml"
            manifest_b = root / "other.yml"
            chunks = root / "chunks.yml"
            chunks.write_text("version: 1\nmeta: {}\n", encoding="utf-8")
            events: list[str] = []
            metadata = (
                SimpleNamespace(
                    image="anatase",
                    distro="f44-x86_64",
                    root_dir=str(root),
                    manifest_labels=(
                        ("org.opencontainers.image.title", "Anatase"),
                        ("org.opencontainers.image.version", "44.20260622"),
                    ),
                ),
                SimpleNamespace(
                    image="other",
                    distro="f44-x86_64",
                    root_dir=str(root),
                    manifest_labels=(),
                ),
            )
            results = (
                SimpleNamespace(
                    output_image="localhost/anatase:f44-x86_64",
                    podman="podman",
                ),
                SimpleNamespace(
                    output_image="localhost/other:f44-x86_64",
                    podman="podman",
                ),
            )

            def mark(name):
                def inner(*_args, **_kwargs):
                    events.append(name)
                    if name == "resolve":
                        return metadata
                    if name == "build-build":
                        return SimpleNamespace()
                    if name == "final":
                        return results
                    return 0

                return inner

            with (
                patch("ludos.bootc.resolve_build_manifests", side_effect=mark("resolve")),
                patch("ludos.bootc.build_package_card_images", side_effect=mark("packages")),
                patch("ludos.bootc.build_build_images", side_effect=mark("build-build")),
                patch("ludos.bootc.build_final_manifest_images", side_effect=mark("final")),
                patch("ludos.bootc._cleanup_dnf_workspaces", side_effect=mark("cleanup")),
                patch("ludos.bootc.ostree_import") as ostree_import_mock,
                patch("ludos.bootc._git_revision", return_value="a" * 40),
                patch("ludos.bootc.rechunk_main") as rechunk_mock,
                patch("ludos.bootc._export_rechunked_oci") as export_mock,
            ):
                ostree_import_mock.side_effect = lambda ref, **_kwargs: events.append(
                    f"import:{ref}"
                )
                rechunk_mock.side_effect = lambda **kwargs: events.append(
                    f"rechunk:{Path(kwargs['contentmeta_fn']).parent.name}"
                )
                export_mock.side_effect = lambda **kwargs: events.append(
                    f"export:{kwargs['safe_name']}"
                )

                result = bootc_create(
                    (manifest_a, manifest_b),
                    chunks=chunks,
                    cache_dir=root / "cache",
                    ci=True,
                    ccache=False,
                    writers=8,
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            events,
            [
                "resolve",
                "packages",
                "build-build",
                "final",
                "cleanup",
                "import:localhost/anatase:f44-x86_64",
                "rechunk:anatase-f44-x86_64",
                "export:anatase-f44-x86_64",
                "import:localhost/other:f44-x86_64",
                "rechunk:other-f44-x86_64",
                "export:other-f44-x86_64",
            ],
        )
        ostree_import_mock.assert_has_calls(
            [
                call(
                    "localhost/anatase:f44-x86_64",
                    cache_dir=(root / "cache").resolve(),
                    orchestrator="localhost/anatase:f44-x86_64",
                    ostree_ref=DEFAULT_OSTREE_REF,
                    ostree_version="44.20260622",
                ),
                call(
                    "localhost/other:f44-x86_64",
                    cache_dir=(root / "cache").resolve(),
                    orchestrator="localhost/other:f44-x86_64",
                    ostree_ref=DEFAULT_OSTREE_REF,
                ),
            ]
        )
        rechunk_mock.assert_has_calls(
            [
                call(
                    repo=str((root / "cache" / "ostree").resolve()),
                    ref=DEFAULT_OSTREE_REF,
                    contentmeta_fn=str(
                        (
                            root
                            / "cache"
                            / "rechunk"
                            / "anatase-f44-x86_64"
                            / "contentmeta.json"
                        ).resolve()
                    ),
                    chunks_fn=str(chunks.resolve()),
                    result_fn=str(
                        (
                            root
                            / "cache"
                            / "rechunk"
                            / "anatase-f44-x86_64"
                            / "results.txt"
                        ).resolve()
                    ),
                    labels=[
                        "org.opencontainers.image.title=Anatase",
                        "org.opencontainers.image.version=44.20260622",
                    ],
                    revision="a" * 40,
                    git_dir=str(root),
                    ostree_image="localhost/anatase:f44-x86_64",
                    podman="podman",
                ),
                call(
                    repo=str((root / "cache" / "ostree").resolve()),
                    ref=DEFAULT_OSTREE_REF,
                    contentmeta_fn=str(
                        (
                            root
                            / "cache"
                            / "rechunk"
                            / "other-f44-x86_64"
                            / "contentmeta.json"
                        ).resolve()
                    ),
                    chunks_fn=str(chunks.resolve()),
                    result_fn=str(
                        (
                            root
                            / "cache"
                            / "rechunk"
                            / "other-f44-x86_64"
                            / "results.txt"
                        ).resolve()
                    ),
                    labels=[],
                    revision="a" * 40,
                    git_dir=str(root),
                    ostree_image="localhost/other:f44-x86_64",
                    podman="podman",
                ),
            ]
        )
        export_mock.assert_has_calls(
            [
                call(
                    podman="podman",
                    image="localhost/anatase:f44-x86_64",
                    ostree_dir=(root / "cache" / "ostree").resolve(),
                    oci_dir=(root / "cache" / "oci").resolve(),
                    work_dir=(
                        root / "cache" / "rechunk" / "anatase-f44-x86_64"
                    ).resolve(),
                    safe_name="anatase-f44-x86_64",
                    writers=8,
                ),
                call(
                    podman="podman",
                    image="localhost/other:f44-x86_64",
                    ostree_dir=(root / "cache" / "ostree").resolve(),
                    oci_dir=(root / "cache" / "oci").resolve(),
                    work_dir=(
                        root / "cache" / "rechunk" / "other-f44-x86_64"
                    ).resolve(),
                    safe_name="other-f44-x86_64",
                    writers=8,
                ),
            ]
        )

    def test_git_revision_returns_current_head(self) -> None:
        completed = subprocess.CompletedProcess(
            ["git", "rev-parse", "HEAD"],
            0,
            stdout="a" * 40 + "\n",
        )
        with patch("ludos.bootc.subprocess.run", return_value=completed) as run:
            self.assertEqual(_git_revision(Path("/repo")), "a" * 40)

        run.assert_called_once_with(
            ["git", "-C", "/repo", "rev-parse", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def test_git_revision_ignores_missing_or_invalid_repo(self) -> None:
        with patch(
            "ludos.bootc.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 1, stdout=""),
        ):
            self.assertIsNone(_git_revision(Path("/missing")))

        with patch(
            "ludos.bootc.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 0, stdout="not-a-hash\n"),
        ):
            self.assertIsNone(_git_revision(Path("/repo")))

    def test_export_rechunked_oci_uses_bootc_ostree_ext_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ostree_dir = root / "ostree"
            oci_dir = root / "oci"
            work_dir = root / "rechunk" / "anatase-f44"

            with patch(
                "ludos.bootc._run_oci_export_command",
                return_value=(0, ""),
            ) as run, patch(
                "ludos.bootc._bootc_encapsulate_supports_jobs",
                return_value=True,
            ):
                _export_rechunked_oci(
                    podman="podman",
                    image="localhost/anatase:f44",
                    ostree_dir=ostree_dir,
                    oci_dir=oci_dir,
                    work_dir=work_dir,
                    safe_name="anatase-f44",
                    writers=8,
                )

        run.assert_called_once_with(
            [
                "podman",
                "run",
                "--rm",
                "--volume",
                f"{ostree_dir}:/ludos/ostree:ro",
                "--volume",
                f"{work_dir}:/ludos/rechunk:ro",
                "--volume",
                f"{oci_dir}:/ludos/oci",
                "localhost/anatase:f44",
                "bootc",
                "internals",
                "ostree-ext",
                "container",
                "encapsulate",
                "--repo",
                "/ludos/ostree",
                "--contentmeta",
                "/ludos/rechunk/contentmeta.json",
                "--jobs",
                "8",
                DEFAULT_OSTREE_REF,
                "oci:/ludos/oci/anatase-f44:latest",
            ]
        )

    def test_export_rechunked_oci_drops_jobs_when_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ostree_dir = root / "ostree"
            oci_dir = root / "oci"
            work_dir = root / "rechunk" / "anatase-f44"

            with patch(
                "ludos.bootc._run_oci_export_command",
                return_value=(0, ""),
            ) as run, patch(
                "ludos.bootc._bootc_encapsulate_supports_jobs",
                return_value=False,
            ):
                _export_rechunked_oci(
                    podman="podman",
                    image="localhost/anatase:f44",
                    ostree_dir=ostree_dir,
                    oci_dir=oci_dir,
                    work_dir=work_dir,
                    safe_name="anatase-f44",
                    writers=8,
                )

        command = run.call_args.args[0]
        self.assertNotIn("--jobs", command)
        self.assertNotIn("8", command)
        self.assertIn(DEFAULT_OSTREE_REF, command)

    def test_export_rechunked_oci_removes_existing_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ostree_dir = root / "ostree"
            oci_dir = root / "oci"
            work_dir = root / "rechunk" / "anatase-f44"
            stale_target = oci_dir / "anatase-f44"
            stale_target.mkdir(parents=True)
            (stale_target / "stale-layer").write_text("", encoding="utf-8")

            with patch(
                "ludos.bootc._run_oci_export_command",
                return_value=(0, ""),
            ), patch(
                "ludos.bootc._bootc_encapsulate_supports_jobs",
                return_value=False,
            ):
                _export_rechunked_oci(
                    podman="podman",
                    image="localhost/anatase:f44",
                    ostree_dir=ostree_dir,
                    oci_dir=oci_dir,
                    work_dir=work_dir,
                    safe_name="anatase-f44",
                )

        self.assertFalse(stale_target.exists())

    def test_bootc_encapsulate_supports_jobs_probes_help(self) -> None:
        completed = subprocess.CompletedProcess(
            ["podman"],
            0,
            stdout="Usage: ostree-ext container encapsulate [OPTIONS]\n  --jobs <JOBS>\n",
        )
        with patch("ludos.bootc.subprocess.run", return_value=completed) as run:
            self.assertTrue(_bootc_encapsulate_supports_jobs("podman", "localhost/anatase:f44"))

        run.assert_called_once_with(
            [
                "podman",
                "run",
                "--rm",
                "localhost/anatase:f44",
                "bootc",
                "internals",
                "ostree-ext",
                "container",
                "encapsulate",
                "--help",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )

    def test_bootc_encapsulate_supports_jobs_warns_on_failed_probe(self) -> None:
        completed = subprocess.CompletedProcess(["podman"], 125, stdout="error\n")
        with (
            patch("ludos.bootc.subprocess.run", return_value=completed),
            patch("ludos.bootc.warning") as warning,
        ):
            self.assertFalse(_bootc_encapsulate_supports_jobs("podman", "localhost/anatase:f44"))

        warning.assert_called_once()

    def test_bootc_encapsulate_supports_jobs_warns_when_flag_missing(self) -> None:
        completed = subprocess.CompletedProcess(["podman"], 0, stdout="Usage\n")
        with (
            patch("ludos.bootc.subprocess.run", return_value=completed),
            patch("ludos.bootc.warning") as warning,
        ):
            self.assertFalse(_bootc_encapsulate_supports_jobs("podman", "localhost/anatase:f44"))

        warning.assert_called_once()

    def test_bootc_encapsulate_supports_jobs_warns_on_timeout(self) -> None:
        with (
            patch(
                "ludos.bootc.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["podman"], 30),
            ),
            patch("ludos.bootc.warning") as warning,
        ):
            self.assertFalse(_bootc_encapsulate_supports_jobs("podman", "localhost/anatase:f44"))

        warning.assert_called_once()

    def test_oci_export_line_rewriter_names_digest_output(self) -> None:
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            _oci_export_line_rewriter(f"{digest}\n"),
            f"Exported OCI digest: {digest}\n",
        )
        self.assertEqual(_oci_export_line_rewriter("copying layer\n"), "copying layer\n")

    def test_read_oci_export_stdout_rewrites_digest_and_captures_output(self) -> None:
        digest = "sha256:" + "a" * 64

        class Process:
            stdout = io.StringIO(f"{digest}\ncopying config\n")

        output: list[str] = []
        with patch("ludos.bootc.pstream") as pstream:
            _read_oci_export_stdout(Process(), output)  # type: ignore[arg-type]

        self.assertEqual(output, [f"{digest}\n", "copying config\n"])
        self.assertEqual(
            pstream.call_args_list,
            [
                call(f"Exported OCI digest: {digest}"),
                call("copying config"),
            ],
        )

    def test_read_oci_export_stderr_updates_progress_for_layer_lines(self) -> None:
        class Process:
            stderr = io.StringIO(
                "Exported OCI layer 7/42: kernel\n"
                "warning: still useful\n"
                "Exported OCI layer 1/42: final ostree\n"
            )

        class Progress:
            total: int | None = None

            def __init__(self) -> None:
                self.n = 0
                self.refresh_count = 0
                self.updates: list[int] = []

            def refresh(self) -> None:
                self.refresh_count += 1

            def update(self, amount: int) -> None:
                self.updates.append(amount)
                self.n += amount

        progress = Progress()

        with patch("ludos.bootc.pstream") as pstream:
            _read_oci_export_stderr(Process(), progress)  # type: ignore[arg-type]

        self.assertEqual(progress.total, 42)
        self.assertEqual(progress.refresh_count, 2)
        self.assertEqual(progress.updates, [1, 1])
        self.assertEqual(
            pstream.call_args_list,
            [
                call("Exported OCI layer 7/42: kernel"),
                call("warning: still useful"),
                call("Exported OCI layer 1/42: final ostree"),
            ],
        )

    def test_ostree_import_defaults_orchestrator_to_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache-root"
            image_exists = subprocess.CompletedProcess(
                ["podman", "image", "exists"], 0
            )
            inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout=(
                    '{"RepoTags":["localhost/anatase:latest"],'
                    '"RepoDigests":["localhost/anatase@sha256:'
                    'abcdef1234567890"]}\n'
                ),
            )

            with (
                patch("ludos.bootc.shutil.which", return_value="podman"),
                patch("ludos.bootc.subprocess.run") as run,
                patch("ludos.bootc.log") as log,
                patch(
                    "ludos.bootc._run_ostree_import_command",
                    return_value=(
                        0,
                        "abcdef1234567890abcdef1234567890"
                        "abcdef1234567890abcdef1234567890\n",
                    ),
                ) as run_import,
            ):
                run.side_effect = [image_exists, inspect]

                result = ostree_import(
                    "localhost/anatase:latest",
                    cache_dir=cache_dir,
                    ostree_ref="anatase",
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    ["podman", "image", "exists", "localhost/anatase:latest"],
                    check=False,
                ),
                call(
                    [
                        "podman",
                        "image",
                        "inspect",
                        "localhost/anatase:latest",
                        "--format",
                        "{{json .}}",
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ),
            ],
        )
        log.assert_any_call(
            "Using orchestrator image: localhost/anatase:latest "
            "(localhost/anatase:latest)"
        )
        command, _repo = run_import.call_args.args
        self.assertIn("localhost/anatase:latest", command)

    def test_ostree_import_mounts_image_and_cache_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache-root"
            ostree_dir = cache_dir / "ostree"
            image_exists = subprocess.CompletedProcess(
                ["podman", "image", "exists"], 0
            )
            source_inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout='{"RepoTags":["localhost/anatase:latest"],"Labels":{}}\n',
            )
            orchestrator_inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout=(
                    '{"RepoTags":["localhost/orchestrator:latest"],'
                    '"RepoDigests":["localhost/orchestrator@sha256:'
                    'abcdef1234567890"]}\n'
                ),
            )

            with (
                patch("ludos.bootc.shutil.which", return_value="podman"),
                patch("ludos.bootc.subprocess.run") as run,
                patch("ludos.bootc.log") as log,
                patch(
                    "ludos.bootc._run_ostree_import_command",
                    return_value=(
                        0,
                        "abcdef1234567890abcdef1234567890"
                        "abcdef1234567890abcdef1234567890\n",
                    ),
                ) as run_import,
            ):
                run.side_effect = [
                    image_exists,
                    image_exists,
                    source_inspect,
                    orchestrator_inspect,
                ]

                result = ostree_import(
                    "localhost/anatase:latest",
                    cache_dir=cache_dir,
                    orchestrator="orchestrator",
                    ostree_ref="anatase",
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    ["podman", "image", "exists", "localhost/anatase:latest"],
                    check=False,
                ),
                call(["podman", "image", "exists", "orchestrator"], check=False),
                call(
                    [
                        "podman",
                        "image",
                        "inspect",
                        "localhost/anatase:latest",
                        "--format",
                        "{{json .}}",
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ),
                call(
                    [
                        "podman",
                        "image",
                        "inspect",
                        "orchestrator",
                        "--format",
                        "{{json .}}",
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ),
            ],
        )
        log.assert_any_call(
            "Using orchestrator image: orchestrator (localhost/orchestrator:latest)"
        )
        run_import.assert_called_once()
        command, repo = run_import.call_args.args
        self.assertEqual(repo, ostree_dir.resolve())
        self.assertEqual(command[:3], ["podman", "run", "--rm"])
        self.assertTrue(
            any(
                item.startswith(
                    "type=image,source=localhost/anatase:latest,target=/ludos/source"
                )
                for item in command
            )
        )
        self.assertIn(
            f"type=bind,source={ostree_dir.resolve()},target=/ludos/ostree",
            command,
        )
        self.assertTrue(
            any(
                item.startswith("type=bind,")
                and "postprocess.py,target=/ludos/postprocess.py,ro" in item
                for item in command
            )
        )
        self.assertIn("LUDOS_OSTREE_REF=anatase", command)
        self.assertIn("orchestrator", command)
        self.assertEqual(
            command[-7:],
            [
                "python3",
                "/ludos/postprocess.py",
                "--progress-total-prefix",
                "__LUDOS_OSTREE_APPROX_TOTAL__ ",
                "/ludos/source",
                "/ludos/ostree",
                "anatase",
            ],
        )

    def test_ostree_import_reads_version_label_from_source_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache-root"
            image_exists = subprocess.CompletedProcess(
                ["podman", "image", "exists"], 0
            )
            inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout=(
                    '{"RepoTags":["localhost/anatase:latest"],'
                    '"Labels":{"org.opencontainers.image.version":"44.20260622"}}\n'
                ),
            )

            with (
                patch("ludos.bootc.shutil.which", return_value="podman"),
                patch("ludos.bootc.subprocess.run") as run,
                patch("ludos.bootc.log"),
                patch(
                    "ludos.bootc._run_ostree_import_command",
                    return_value=(0, f"{'a' * 64}\n"),
                ) as run_import,
            ):
                run.side_effect = [image_exists, inspect]

                result = ostree_import(
                    "localhost/anatase:latest",
                    cache_dir=cache_dir,
                    ostree_ref="anatase",
                )

        self.assertEqual(result, 0)
        command, _repo = run_import.call_args.args
        self.assertIn("LUDOS_OSTREE_VERSION=44.20260622", command)
        self.assertIn("--ostree-version", command)
        self.assertIn("44.20260622", command)

    def test_ostree_import_passes_ostree_version_to_postprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache-root"
            image_exists = subprocess.CompletedProcess(
                ["podman", "image", "exists"], 0
            )
            inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout='{"RepoTags":["localhost/orchestrator:latest"]}\n',
            )

            with (
                patch("ludos.bootc.shutil.which", return_value="podman"),
                patch("ludos.bootc.subprocess.run") as run,
                patch("ludos.bootc.log"),
                patch(
                    "ludos.bootc._run_ostree_import_command",
                    return_value=(0, f"{'a' * 64}\n"),
                ) as run_import,
            ):
                run.side_effect = [image_exists, image_exists, inspect]

                result = ostree_import(
                    "localhost/anatase:latest",
                    cache_dir=cache_dir,
                    orchestrator="orchestrator",
                    ostree_ref="anatase",
                    ostree_version="44.20260622",
                )

        self.assertEqual(result, 0)
        command, _repo = run_import.call_args.args
        self.assertIn("LUDOS_OSTREE_VERSION=44.20260622", command)
        self.assertEqual(
            command[-9:],
            [
                "python3",
                "/ludos/postprocess.py",
                "--progress-total-prefix",
                "__LUDOS_OSTREE_APPROX_TOTAL__ ",
                "--ostree-version",
                "44.20260622",
                "/ludos/source",
                "/ludos/ostree",
                "anatase",
            ],
        )

    def test_ostree_import_no_process_uses_raw_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache-root"
            image_exists = subprocess.CompletedProcess(
                ["podman", "image", "exists"], 0
            )
            source_inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout='{"RepoTags":["localhost/anatase:latest"],"Labels":{}}\n',
            )
            orchestrator_inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout=(
                    '{"RepoTags":["localhost/orchestrator:latest"],'
                    '"RepoDigests":["localhost/orchestrator@sha256:'
                    'abcdef1234567890"]}\n'
                ),
            )

            with (
                patch("ludos.bootc.shutil.which", return_value="podman"),
                patch("ludos.bootc.subprocess.run") as run,
                patch("ludos.bootc.log"),
                patch(
                    "ludos.bootc._run_ostree_import_command",
                    return_value=(
                        0,
                        "abcdef1234567890abcdef1234567890"
                        "abcdef1234567890abcdef1234567890\n",
                    ),
                ) as run_import,
            ):
                run.side_effect = [
                    image_exists,
                    image_exists,
                    source_inspect,
                    orchestrator_inspect,
                ]

                result = ostree_import(
                    "localhost/anatase:latest",
                    cache_dir=cache_dir,
                    orchestrator="orchestrator",
                    ostree_ref="anatase",
                    process=False,
                )

        self.assertEqual(result, 0)
        command, _repo = run_import.call_args.args
        script = command[-1]
        self.assertIn('--tree=dir="$source"', script)
        self.assertNotIn("--tar-autocreate-parents", script)

    def test_ostree_import_no_process_passes_ostree_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache-root"
            image_exists = subprocess.CompletedProcess(
                ["podman", "image", "exists"], 0
            )
            inspect = subprocess.CompletedProcess(
                ["podman", "image", "inspect"],
                0,
                stdout='{"RepoTags":["localhost/orchestrator:latest"]}\n',
            )

            with (
                patch("ludos.bootc.shutil.which", return_value="podman"),
                patch("ludos.bootc.subprocess.run") as run,
                patch("ludos.bootc.log"),
                patch(
                    "ludos.bootc._run_ostree_import_command",
                    return_value=(0, f"{'a' * 64}\n"),
                ) as run_import,
            ):
                run.side_effect = [image_exists, image_exists, inspect]

                result = ostree_import(
                    "localhost/anatase:latest",
                    cache_dir=cache_dir,
                    orchestrator="orchestrator",
                    ostree_ref="anatase",
                    process=False,
                    ostree_version="44.20260622",
                )

        self.assertEqual(result, 0)
        command, _repo = run_import.call_args.args
        self.assertIn("LUDOS_OSTREE_VERSION=44.20260622", command)
        script = command[-1]
        self.assertIn('set -- "--add-metadata-string=version=$LUDOS_OSTREE_VERSION"', script)
        self.assertIn('"$@"', script)

    def test_short_digest_handles_repo_digest_and_image_id(self) -> None:
        self.assertEqual(
            _short_digest("localhost/orchestrator@sha256:abcdef1234567890"),
            "sha256:abcdef123456",
        )
        self.assertEqual(
            _short_digest("abcdef1234567890"),
            "abcdef123456",
        )

    def test_parse_commit_returns_last_checksum(self) -> None:
        first = "0" * 64
        second = "1" * 64
        self.assertEqual(_parse_commit(f"{first}\n{second}\n"), second)

    def test_parse_progress_total(self) -> None:
        self.assertEqual(
            _parse_progress_total("__LUDOS_OSTREE_APPROX_TOTAL__ 42"),
            42,
        )
        self.assertIsNone(_parse_progress_total("__LUDOS_OSTREE_APPROX_TOTAL__ nope"))

    def test_read_ostree_stderr_updates_progress_total_and_streams_output(self) -> None:
        class Process:
            stderr = io.StringIO(
                "__LUDOS_OSTREE_APPROX_TOTAL__ 42\n"
                "OT: using fuse: 0\n"
                "OT: Preparing transaction\n"
            )

        class Progress:
            total: int | None = None

            def __init__(self) -> None:
                self.refresh_count = 0

            def refresh(self) -> None:
                self.refresh_count += 1

        progress = Progress()

        with patch("ludos.bootc.pstream") as pstream:
            _read_ostree_stderr(Process(), progress)  # type: ignore[arg-type]

        self.assertEqual(progress.total, 42)
        self.assertEqual(progress.refresh_count, 1)
        self.assertEqual(
            pstream.call_args_list,
            [
                call("OT: using fuse: 0"),
                call("OT: Preparing transaction"),
            ],
        )

    def test_count_repo_objects_ignores_tmp_and_unrelated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ostree_dir = Path(tmp) / "ostree"
            objects = ostree_dir / "objects"
            (objects / "00").mkdir(parents=True)
            (objects / "tmp").mkdir()
            (objects / "00" / "a.file").write_text("", encoding="utf-8")
            (objects / "00" / "b.dirtree").write_text("", encoding="utf-8")
            (objects / "00" / "c.dirmeta").write_text("", encoding="utf-8")
            (objects / "00" / "d.commit").write_text("", encoding="utf-8")
            (objects / "00" / "e.detachedmeta").write_text("", encoding="utf-8")
            (objects / "tmp" / "f.file").write_text("", encoding="utf-8")

            self.assertEqual(_count_repo_objects(ostree_dir), 4)

    def test_update_object_progress_only_moves_forward(self) -> None:
        class Progress:
            def __init__(self) -> None:
                self.n = 3
                self.updates: list[int] = []

            def update(self, amount: int) -> None:
                self.updates.append(amount)
                self.n += amount

        with tempfile.TemporaryDirectory() as tmp:
            ostree_dir = Path(tmp) / "ostree"
            objects = ostree_dir / "objects" / "00"
            objects.mkdir(parents=True)
            for index in range(5):
                (objects / f"{index}.file").write_text("", encoding="utf-8")

            progress = Progress()
            _update_object_progress(ostree_dir, baseline=1, progress=progress)
            _update_object_progress(ostree_dir, baseline=3, progress=progress)

        self.assertEqual(progress.updates, [1])
        self.assertEqual(progress.n, 4)


if __name__ == "__main__":
    unittest.main()

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
    _export_rechunked_oci,
    _parse_commit,
    _parse_progress_total,
    _resolve_chunks_path,
    _safe_oci_name,
    _count_repo_objects,
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
                "--no-ccache",
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
        self.assertTrue(args.no_ccache)
        self.assertEqual(args.manifests, [Path("anatase.yml"), Path("other.yml")])

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

    def test_safe_oci_name_sanitizes_image_ref(self) -> None:
        self.assertEqual(
            _safe_oci_name("localhost/anatase:f44-x86_64"),
            "anatase-f44-x86_64",
        )
        self.assertEqual(
            _safe_oci_name("registry.example.com/team/anatase:test"),
            "registry.example.com-team-anatase-test",
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
                    manifest_labels=(("org.opencontainers.image.title", "Anatase"),)
                ),
                SimpleNamespace(manifest_labels=()),
            )
            results = (
                SimpleNamespace(output_image="localhost/anatase:f44", podman="podman"),
                SimpleNamespace(output_image="localhost/other:f44", podman="podman"),
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
                "import:localhost/anatase:f44",
                "rechunk:anatase-f44",
                "export:anatase-f44",
                "import:localhost/other:f44",
                "rechunk:other-f44",
                "export:other-f44",
            ],
        )
        ostree_import_mock.assert_has_calls(
            [
                call(
                    "localhost/anatase:f44",
                    cache_dir=(root / "cache").resolve(),
                    orchestrator="localhost/anatase:f44",
                    ostree_ref="master",
                ),
                call(
                    "localhost/other:f44",
                    cache_dir=(root / "cache").resolve(),
                    orchestrator="localhost/other:f44",
                    ostree_ref="master",
                ),
            ]
        )
        rechunk_mock.assert_has_calls(
            [
                call(
                    repo=str((root / "cache" / "ostree").resolve()),
                    ref="master",
                    contentmeta_fn=str(
                        (root / "cache" / "rechunk" / "anatase-f44" / "contentmeta.json").resolve()
                    ),
                    chunks_fn=str(chunks.resolve()),
                    result_fn=str(
                        (root / "cache" / "rechunk" / "anatase-f44" / "results.txt").resolve()
                    ),
                    labels=["org.opencontainers.image.title=Anatase"],
                ),
                call(
                    repo=str((root / "cache" / "ostree").resolve()),
                    ref="master",
                    contentmeta_fn=str(
                        (root / "cache" / "rechunk" / "other-f44" / "contentmeta.json").resolve()
                    ),
                    chunks_fn=str(chunks.resolve()),
                    result_fn=str(
                        (root / "cache" / "rechunk" / "other-f44" / "results.txt").resolve()
                    ),
                    labels=[],
                ),
            ]
        )

    def test_export_rechunked_oci_uses_bootc_ostree_ext_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ostree_dir = root / "ostree"
            oci_dir = root / "oci"
            work_dir = root / "rechunk" / "anatase-f44"

            with patch(
                "ludos.bootc._run_streamed_command",
                return_value=(0, ""),
            ) as run:
                _export_rechunked_oci(
                    podman="podman",
                    image="localhost/anatase:f44",
                    ostree_dir=ostree_dir,
                    oci_dir=oci_dir,
                    work_dir=work_dir,
                    safe_name="anatase-f44",
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
                "master",
                "oci:/ludos/oci/anatase-f44:latest",
            ]
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
            inspect = subprocess.CompletedProcess(
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
                run.side_effect = [image_exists, image_exists, inspect]

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

    def test_ostree_import_no_process_uses_raw_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache-root"
            image_exists = subprocess.CompletedProcess(
                ["podman", "image", "exists"], 0
            )
            inspect = subprocess.CompletedProcess(
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
                run.side_effect = [image_exists, image_exists, inspect]

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

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ludos.build import (
    StagedSpec,
    _build_specs_output_image,
    _card_specs_hash,
    _git_spec_source_revision,
    _git_source_cache_key,
    _github_token,
    _pin_git_source_cache,
    _render_card_build_output_containerfile,
    _render_specs_build_output_containerfile,
    _resolve_git_spec_source,
    _resolve_spec_build_requires,
    _resolve_staged_spec_builder_packages,
    _stage_spec_build_contexts,
    _stage_card_specs,
    _specs_build_script,
)
from ludos.model import Card, ConfigError, SpecBuild


class GitSpecSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is required")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.card_dir = self.root / "card"
        self.card_dir.mkdir()
        self.card_source = self.card_dir / "card.yml"
        self.card_source.write_text("version: 1\n", encoding="utf-8")
        self.cache_dir = self.root / "cache" / "spec-sources" / "git"
        self.workspace_dir = self.root / "workspace"
        self._git(["init", str(self.repo)], cwd=self.root)
        self._git(["config", "user.email", "test@example.com"], cwd=self.repo)
        self._git(["config", "user.name", "Test User"], cwd=self.repo)
        self._git(["config", "commit.gpgsign", "false"], cwd=self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_stage_initializes_and_updates_cache_to_looked_up_revision(self) -> None:
        self._write("pkg/test.spec", "Name: test\nVersion: 1\n")
        self._commit("initial spec")
        spec = self._spec("pkg/test.spec", files=("test.spec",))

        first_hash, first_revisions = self._hash_with_revisions(spec)
        cached_repo = self.cache_dir / _git_source_cache_key(self.repo.as_uri()) / "repo"
        self.assertFalse(cached_repo.exists())

        self._stage(spec, first_revisions)

        self.assertTrue((cached_repo / ".git").is_dir())
        self.assertEqual(self._rev_parse(cached_repo), self._rev_parse(self.repo))

        self._write("pkg/test.spec", "Name: test\nVersion: 2\n")
        self._commit("update spec")
        second_hash, second_revisions = self._hash_with_revisions(spec)
        self.assertNotEqual(self._rev_parse(cached_repo), self._rev_parse(self.repo))

        self._stage(spec, second_revisions)

        self.assertNotEqual(first_hash, second_hash)
        self.assertEqual(self._rev_parse(cached_repo), self._rev_parse(self.repo))

    def test_files_stages_only_selected_entries(self) -> None:
        self._write("hhd.spec", "Name: hhd\nVersion: 1\n")
        self._write("README.md", "not part of this build\n")
        self._commit("hhd files")
        spec = self._spec("hhd.spec", files=("hhd.spec",))

        _hash, revisions = self._hash_with_revisions(spec)
        self._stage(spec, revisions)

        self.assertEqual(tuple(path.name for path in self._workspace_files()), ("hhd.spec",))

    def test_missing_files_stages_spec_directory_with_containerignore(self) -> None:
        self._write(".containerignore", "pkg/ignored.txt\n")
        self._write("pkg/test.spec", "Name: test\nVersion: 1\n")
        self._write("pkg/keep.txt", "keep\n")
        self._write("pkg/ignored.txt", "ignore\n")
        self._commit("directory spec")
        spec = self._spec("pkg/test.spec")

        _hash, revisions = self._hash_with_revisions(spec)
        self._stage(spec, revisions)

        self.assertEqual(
            tuple(path.name for path in self._workspace_files()),
            ("keep.txt", "test.spec"),
        )

    def test_multiple_root_git_specs_do_not_delete_each_other(self) -> None:
        other_repo = self.root / "other-source"
        self._git(["init", str(other_repo)], cwd=self.root)
        self._git(["config", "user.email", "test@example.com"], cwd=other_repo)
        self._git(["config", "user.name", "Test User"], cwd=other_repo)
        self._git(["config", "commit.gpgsign", "false"], cwd=other_repo)
        self._write("hhd.spec", "Name: hhd\nVersion: 1\n")
        self._commit("hhd")
        other_spec = other_repo / "hhd-ui.spec"
        other_spec.write_text("Name: hhd-ui\nVersion: 1\n", encoding="utf-8")
        self._git(["add", "."], cwd=other_repo)
        self._git(["commit", "-m", "hhd-ui"], cwd=other_repo)
        specs = (
            self._spec("hhd.spec", files=("hhd.spec",)),
            SpecBuild(
                spec=f"git+{other_repo.as_uri()}:hhd-ui.spec",
                packages={"*": ("hhd-ui",)},
            ),
        )
        _hash, revisions = _card_specs_hash(
            self.card_source,
            specs,
            {},
            "",
            self.cache_dir,
            cache_only=False,
        )

        _stage_card_specs(
            card_source=self.card_source,
            specs=specs,
            card_env={},
            workspace_dir=self.workspace_dir,
            arch="x86_64",
            spec_source_cache_dir=self.cache_dir,
            cache_only=True,
            source_revisions=revisions,
        )

        self.assertEqual(
            sorted(path.name for path in self.workspace_dir.rglob("*.spec")),
            ["hhd-ui.spec", "hhd.spec"],
        )

    def test_multiple_root_local_specs_do_not_delete_each_other(self) -> None:
        (self.card_dir / "scx-tools.spec").write_text(
            "Name: scx-tools\nVersion: 1\n",
            encoding="utf-8",
        )
        (self.card_dir / "scx-scheds.spec").write_text(
            "Name: scx-scheds\nVersion: 1\n",
            encoding="utf-8",
        )
        specs = (
            SpecBuild(
                spec="scx-tools.spec",
                packages={"*": ("scx-tools",)},
                files=("scx-tools.spec",),
            ),
            SpecBuild(
                spec="scx-scheds.spec",
                packages={"*": ("scx-scheds",)},
                files=("scx-scheds.spec",),
            ),
        )

        staged = _stage_card_specs(
            card_source=self.card_source,
            specs=specs,
            card_env={},
            workspace_dir=self.workspace_dir,
            arch="x86_64",
            spec_source_cache_dir=self.cache_dir,
            cache_only=True,
        )

        self.assertEqual(
            sorted(spec.spec_path.name for spec in staged),
            ["scx-scheds.spec", "scx-tools.spec"],
        )
        self.assertEqual(
            sorted(path.name for path in self.workspace_dir.rglob("*.spec")),
            ["scx-scheds.spec", "scx-tools.spec"],
        )

    def test_git_spec_hash_tracks_commits_outside_selected_files(self) -> None:
        self._write("hhd.spec", "Name: hhd\nVersion: 1\n")
        self._write("README.md", "first\n")
        self._commit("initial")
        spec = self._spec("hhd.spec", files=("hhd.spec",))

        first_hash = self._hash(spec)
        self._write("README.md", "second\n")
        self._commit("unselected update")
        second_hash = self._hash(spec)
        self._write("hhd.spec", "Name: hhd\nVersion: 2\n")
        self._commit("selected update")
        third_hash = self._hash(spec)

        self.assertNotEqual(first_hash, second_hash)
        self.assertNotEqual(second_hash, third_hash)

    def test_git_spec_hash_tracks_head_without_populating_cache(self) -> None:
        self._write("hhd-git.spec", "Name: hhd\nVersion: 1\n")
        self._write("README.md", "first\n")
        self._commit("initial")
        spec = self._spec(
            "hhd-git.spec",
            files=("hhd-git.spec",),
        )

        first_hash = self._hash(spec)
        cached_repo = self.cache_dir / _git_source_cache_key(self.repo.as_uri()) / "repo"
        self.assertFalse(cached_repo.exists())
        self._write("README.md", "second\n")
        self._commit("unselected update")
        second_hash = self._hash(spec)

        self.assertNotEqual(first_hash, second_hash)

    def test_git_spec_revision_lookup_soft_retries_three_times(self) -> None:
        revision = "a" * 40
        failure = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="HTTP 503\n"
        )
        success = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{revision}\tHEAD\n",
            stderr="",
        )
        source = self._spec("hhd.spec").spec

        with patch(
            "ludos.build.subprocess.run",
            side_effect=(failure, failure, failure, success),
        ) as run, patch("ludos.build.time.sleep") as sleep:
            resolved = _git_spec_source_revision(
                source,
                self.cache_dir,
                cache_only=False,
            )

        self.assertEqual(resolved, revision)
        self.assertEqual(run.call_count, 4)
        self.assertEqual(
            [call.args for call in sleep.call_args_list],
            [(2,), (2,), (2,)],
        )

    def test_git_spec_hash_cache_only_uses_cached_head(self) -> None:
        self._write("hhd-git.spec", "Name: hhd\nVersion: 1\n")
        self._commit("initial")
        spec = self._spec("hhd-git.spec")
        _hash, revisions = self._hash_with_revisions(spec)
        self._stage(spec, revisions)
        cached_revision = revisions[0][1]
        self._write("README.md", "new upstream content\n")
        self._commit("new head")

        _hash, cached_revisions = _card_specs_hash(
            self.card_source,
            (spec,),
            {},
            "",
            self.cache_dir,
            cache_only=True,
        )

        self.assertEqual(cached_revisions, ((spec.spec, cached_revision),))

    def test_stage_renders_parse_time_git_revision_macros(self) -> None:
        self._write(
            "pkg/hhd-ui-git.spec",
            "\n".join(
                (
                    "%global commit %(git rev-parse --verify HEAD)",
                    "%global shortcommit %(git rev-parse --short=12 %{commit})",
                    "%global gitversion %(tag=$(git describe --tags --abbrev=0 "
                    "--match 'v[0-9]*' %{commit} 2>/dev/null || :); printf x)",
                    "Name: hhd-ui",
                    "Version: %{gitversion}",
                    "",
                )
            ),
        )
        self._commit("release")
        self._git(["tag", "v1.2.3"], cwd=self.repo)
        self._write("README.md", "new source\n")
        self._commit("snapshot")
        spec = self._spec("pkg/hhd-ui-git.spec")
        _hash, revisions = self._hash_with_revisions(spec)

        self._stage(spec, revisions)

        rendered = self._workspace_file("hhd-ui-git.spec").read_text()
        self.assertNotIn("%(git", rendered)
        self.assertIn(f"%global commit {self._rev_parse(self.repo)}", rendered)
        self.assertIn("%global gitversion 1.2.3+git.1.g", rendered)

    def test_card_hash_expression_overrides_spec_hash(self) -> None:
        self._write("hhd.spec", "Name: hhd\nVersion: 1\n")
        self._commit("initial")
        (self.card_dir / "extra.txt").write_text("first\n", encoding="utf-8")
        spec = self._spec("hhd.spec", files=("hhd.spec",))

        first_hash, _revisions = _card_specs_hash(
            self.card_source,
            (spec,),
            {},
            "",
            self.cache_dir,
            hash_expression="@hash(extra.txt)",
            cache_only=False,
        )
        self._write("hhd.spec", "Name: hhd\nVersion: 2\n")
        self._commit("selected update")
        selected_hash, _revisions = _card_specs_hash(
            self.card_source,
            (spec,),
            {},
            "",
            self.cache_dir,
            hash_expression="@hash(extra.txt)",
            cache_only=False,
        )
        (self.card_dir / "extra.txt").write_text("second\n", encoding="utf-8")
        second_hash, _revisions = _card_specs_hash(
            self.card_source,
            (spec,),
            {},
            "",
            self.cache_dir,
            hash_expression="@hash(extra.txt)",
            cache_only=False,
        )

        self.assertEqual(first_hash, selected_hash)
        self.assertNotEqual(selected_hash, second_hash)

    def test_stage_clones_missing_cache_at_promised_revision(self) -> None:
        self._write("hhd.spec", "Name: hhd\nVersion: 1\n")
        self._commit("initial")
        spec = self._spec("hhd.spec", files=("hhd.spec",))
        _hash, revisions = self._hash_with_revisions(spec)

        self._write("hhd.spec", "Name: hhd\nVersion: 2\n")
        self._commit("new head")
        self._stage(spec, revisions)

        self.assertIn("Version: 1", self._workspace_file("hhd.spec").read_text())

    def test_pinned_git_spec_fetch_soft_retries_three_times(self) -> None:
        repo_dir = self.cache_dir / "source" / "repo"
        revision = "a" * 40
        streamed_results = (
            (0, ""),
            (0, ""),
            (128, "HTTP 503\n"),
            (128, "HTTP 503\n"),
            (128, "HTTP 503\n"),
            (0, ""),
            (0, ""),
        )

        with (
            patch("ludos.build._is_git_repository", return_value=False),
            patch("ludos.build._git_has_revision", return_value=False),
            patch(
                "ludos.build._run_streamed_command",
                side_effect=streamed_results,
            ) as run,
            patch("ludos.build.time.sleep") as sleep,
        ):
            _pin_git_source_cache(
                "git",
                repo_dir,
                "https://src.fedoraproject.org/rpms/ark",
                revision,
                "git+https://src.fedoraproject.org/rpms/ark:ark.spec",
            )

        self.assertEqual(run.call_count, 7)
        self.assertEqual(run.call_args_list[2], run.call_args_list[3])
        self.assertEqual(run.call_args_list[3], run.call_args_list[4])
        self.assertEqual(run.call_args_list[4], run.call_args_list[5])
        self.assertEqual(
            [call.args for call in sleep.call_args_list],
            [(2,), (2,), (2,)],
        )

    def test_stage_repins_existing_cache_to_promised_revision(self) -> None:
        self._write("hhd.spec", "Name: hhd\nVersion: 1\n")
        self._commit("initial")
        spec = self._spec("hhd.spec", files=("hhd.spec",))
        _hash, revisions = self._hash_with_revisions(spec)
        self._stage(spec, revisions)

        self._write("hhd.spec", "Name: hhd\nVersion: 2\n")
        self._commit("new head")
        _new_hash, new_revisions = self._hash_with_revisions(spec)
        self._stage(spec, new_revisions)
        cached_repo = self.cache_dir / _git_source_cache_key(self.repo.as_uri()) / "repo"
        self.assertEqual(self._rev_parse(cached_repo), self._rev_parse(self.repo))

        self._stage(spec, revisions)

        self.assertIn("Version: 1", self._workspace_file("hhd.spec").read_text())
        self.assertNotEqual(self._rev_parse(cached_repo), self._rev_parse(self.repo))

    def test_concurrent_pins_serialize_shared_git_cache(self) -> None:
        self._write("hhd.spec", "Name: hhd\nVersion: 1\n")
        self._commit("initial")
        source = self._spec("hhd.spec").spec
        revision = self._rev_parse(self.repo)
        first_entered = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        active = 0
        max_active = 0
        calls = 0

        def delayed_pin(*args) -> None:
            nonlocal active, max_active, calls
            _pin_git_source_cache(*args)
            with state_lock:
                calls += 1
                active += 1
                max_active = max(max_active, active)
                if calls == 1:
                    first_entered.set()
                else:
                    second_entered.set()
            release.wait(timeout=5)
            with state_lock:
                active -= 1

        def resolve():
            return _resolve_git_spec_source(
                self.card_source,
                source,
                self.cache_dir,
                cache_only=True,
                revision=revision,
            )

        def resolve_second():
            second_started.set()
            return resolve()

        with patch("ludos.build._pin_git_source_cache", side_effect=delayed_pin):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(resolve)
                self.assertTrue(first_entered.wait(timeout=5))
                second = executor.submit(resolve_second)
                self.assertTrue(second_started.wait(timeout=5))
                try:
                    self.assertFalse(second_entered.wait(timeout=0.25))
                finally:
                    release.set()
                first_source = first.result(timeout=5)
                second_source = second.result(timeout=5)

        self.assertEqual(calls, 2)
        self.assertEqual(max_active, 1)
        self.assertEqual(first_source.revision, revision)
        self.assertEqual(second_source.revision, revision)

    def test_spec_files_requires_list(self) -> None:
        card_path = self.root / "list-card.yml"
        card_path.write_text(
            "\n".join(
                (
                    "version: 1",
                    "specs:",
                    "  - spec: git+https://example.com/repo:hhd.spec",
                    "    files:",
                    "      - hhd.spec",
                    "    packages:",
                    "      - hhd",
                    "",
                )
            ),
            encoding="utf-8",
        )

        card = Card.from_file(card_path)

        self.assertEqual(card.specs[0].files, ("hhd.spec",))

        scalar_card_path = self.root / "scalar-card.yml"
        scalar_card_path.write_text(
            "\n".join(
                (
                    "version: 1",
                    "specs:",
                    "  - spec: git+https://example.com/repo:hhd.spec",
                    "    files: hhd.spec",
                    "    packages:",
                    "      - hhd",
                    "",
                )
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "must be a list of strings"):
            Card.from_file(scalar_card_path)

    def test_spec_can_request_authenticated_downloads(self) -> None:
        card_path = self.root / "private-card.yml"
        card_path.write_text(
            "\n".join(
                (
                    "version: 1",
                    "specs:",
                    "  - spec: git+https://github.com/example/private:pkg.spec",
                    "    auth: github",
                    "    packages:",
                    "      - pkg",
                    "",
                )
            ),
            encoding="utf-8",
        )

        card = Card.from_file(card_path)

        self.assertEqual(card.specs[0].auth, "github")

    def test_github_token_falls_back_to_gh(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="gh-token\n", stderr=""
        )
        with (
            patch.dict(os.environ, {"GH_TOKEN": ""}),
            patch("ludos.build.shutil.which", return_value="/usr/bin/gh"),
            patch("ludos.build.subprocess.run", return_value=completed) as run,
        ):
            token = _github_token()

        self.assertEqual(token, "gh-token")
        run.assert_called_once_with(
            ["/usr/bin/gh", "auth", "token", "--hostname", "github.com"],
            check=False,
            text=True,
            capture_output=True,
        )

    def _spec(
        self,
        spec_path: str,
        *,
        files: tuple[str, ...] = tuple(),
    ) -> SpecBuild:
        return SpecBuild(
            spec=f"git+{self.repo.as_uri()}:{spec_path}",
            packages={"*": ("test",)},
            files=files,
        )

    def _hash(self, spec: SpecBuild) -> str:
        return self._hash_with_revisions(spec)[0]

    def _hash_with_revisions(
        self,
        spec: SpecBuild,
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        return _card_specs_hash(
            self.card_source,
            (spec,),
            {},
            "",
            self.cache_dir,
            cache_only=False,
        )

    def _stage(
        self,
        spec: SpecBuild,
        revisions: tuple[tuple[str, str], ...] = tuple(),
    ) -> None:
        _stage_card_specs(
            card_source=self.card_source,
            specs=(spec,),
            card_env={},
            workspace_dir=self.workspace_dir,
            arch="x86_64",
            spec_source_cache_dir=self.cache_dir,
            cache_only=True,
            source_revisions=revisions,
        )

    def _write(self, relative: str, contents: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _commit(self, message: str) -> None:
        self._git(["add", "."], cwd=self.repo)
        self._git(["commit", "-m", message], cwd=self.repo)

    def _rev_parse(self, repo: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def _workspace_file(self, name: str) -> Path:
        matches = tuple(self.workspace_dir.rglob(name))
        self.assertEqual(len(matches), 1)
        return matches[0]

    def _workspace_files(self) -> tuple[Path, ...]:
        return tuple(
            sorted(
                path.relative_to(self.workspace_dir)
                for path in self.workspace_dir.rglob("*")
                if path.is_file()
            )
        )

    def _git(self, args: list[str], *, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class SpecBuildRequiresResolutionTests(unittest.TestCase):
    def test_builddep_resolution_keeps_direct_requires_from_partial_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            spec_path = workspace_dir / "pkg.spec"
            spec_path.write_text("Name: pkg\nBuildRequires: cargo\n", encoding="utf-8")
            seen_commands = []
            seen_hash_inputs = []

            def preview(cmd, _resolve_cache_dir, _repo_images, extra_hash_inputs=()):
                seen_commands.append(cmd)
                seen_hash_inputs.append(extra_hash_inputs)
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="\n".join(
                        (
                            "Package Arch Version Repository Size",
                            "Installing:",
                            " cargo x86_64 0:1.94.1-1.fc44 fedora 23.2 MiB",
                            "Transaction Summary:",
                        )
                    ),
                    stderr="",
                )

            with patch("ludos.build._run_cached_transaction_preview", preview):
                packages = _resolve_spec_build_requires(
                    [],
                    "44",
                    workspace_dir,
                    (spec_path,),
                    "x86_64",
                    {},
                    workspace_dir / "resolve",
                    tuple(),
                    include_dependencies=False,
                )

        self.assertEqual(packages, ("cargo-0:1.94.1-1.fc44.x86_64",))
        self.assertNotIn("--no-best", seen_commands[0])
        self.assertEqual(seen_hash_inputs[0][0][0], "pkg.spec")
        self.assertEqual(len(seen_hash_inputs[0][0][1]), 64)

    def test_builddep_resolution_rejects_failed_empty_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            spec_path = workspace_dir / "pkg.spec"
            spec_path.write_text("Name: pkg\nBuildRequires: cargo\n", encoding="utf-8")

            def preview(cmd, *_args, **_kwargs):
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="Failed to resolve the transaction:\nProblem 1: conflict\n",
                    stderr="",
                )

            with patch("ludos.build._run_cached_transaction_preview", preview):
                with self.assertRaisesRegex(
                    ConfigError,
                    "dnf did not resolve spec BuildRequires",
                ):
                    _resolve_spec_build_requires(
                        [],
                        "44",
                        workspace_dir,
                        (spec_path,),
                        "x86_64",
                        {},
                        workspace_dir / "resolve",
                        tuple(),
                        include_dependencies=False,
                    )

    def test_i686_build_script_exports_multilib_cxx_include_path(self) -> None:
        workspace_dir = Path("/workspace")
        staged_specs = (
            StagedSpec(
                spec=SpecBuild(spec="mesa.spec"),
                spec_path=workspace_dir / "mesa.spec",
                source_dir=workspace_dir,
                packages=("mesa-libGL.i686",),
                targets=("i686",),
            ),
        )

        script = _specs_build_script(staged_specs, workspace_dir, "x86_64")

        self.assertIn("cmake = 'cmake'", script)
        self.assertIn("i686-redhat-linux", script)
        self.assertIn("export CPLUS_INCLUDE_PATH=", script)

    def test_x86_64_build_script_omits_i686_overrides(self) -> None:
        workspace_dir = Path("/workspace")
        staged_specs = (
            StagedSpec(
                spec=SpecBuild(spec="kate.spec"),
                spec_path=workspace_dir / "kate.spec",
                source_dir=workspace_dir,
                packages=("kate",),
                targets=("x86_64",),
            ),
        )

        script = _specs_build_script(staged_specs, workspace_dir, "x86_64")

        self.assertNotIn("ludos-meson-i686", script)
        self.assertNotIn("BINDGEN_EXTRA_CLANG_ARGS", script)
        self.assertNotIn("i686-redhat-linux", script)
        self.assertNotIn("if [ \"$target\" = i686 ]; then", script)

    def test_remote_sources_are_cached_per_spec_name(self) -> None:
        workspace_dir = Path("/workspace")
        staged_specs = (
            StagedSpec(
                spec=SpecBuild(spec="scx-tools.spec"),
                spec_path=workspace_dir / "scx-tools.spec",
                source_dir=workspace_dir,
                packages=("scx-tools",),
                targets=("x86_64",),
            ),
            StagedSpec(
                spec=SpecBuild(spec="scx-scheds.spec"),
                spec_path=workspace_dir / "scx-scheds.spec",
                source_dir=workspace_dir,
                packages=("scx-scheds",),
                targets=("x86_64",),
            ),
        )

        script = _specs_build_script(staged_specs, workspace_dir, "x86_64")

        self.assertTrue(script.startswith("set -euxo pipefail\n"))
        self.assertIn('spec_source_cache="$source_cache/scx_tools"', script)
        self.assertIn('spec_source_cache="$source_cache/scx_scheds"', script)
        self.assertIn('[ ! -f "$spec_source_cache/$source_name" ]', script)
        self.assertIn(
            'spectool -g -C "$spec_source_cache" "$topdir/SPECS/scx-tools.spec" '
            '2>&1 | tee "$topdir/spectool-download.log"',
            script,
        )
        self.assertIn(
            "grep -q '^Download failed:' \"$topdir/spectool-download.log\"",
            script,
        )
        self.assertIn('cp -f -t "$topdir/SOURCES"', script)
        self.assertNotIn('cp -n -t "$topdir/SOURCES"', script)

    def test_failed_spectool_download_stops_before_rpmbuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace_dir = root / "workspace"
            workspace_dir.mkdir()
            spec_path = workspace_dir / "missing-source.spec"
            spec_path.write_text("Name: missing-source\n", encoding="utf-8")
            staged_specs = (
                StagedSpec(
                    spec=SpecBuild(spec="missing-source.spec"),
                    spec_path=spec_path,
                    source_dir=workspace_dir,
                    packages=("missing-source",),
                    targets=("x86_64",),
                ),
            )
            script = _specs_build_script(
                staged_specs,
                workspace_dir,
                "x86_64",
            )
            script = script.replace("/workspace", str(workspace_dir))
            script = script.replace(
                "/cache/artifacts/sources",
                str(root / "source-cache"),
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            spectool = bin_dir / "spectool"
            spectool.write_text(
                "\n".join(
                    (
                        "#!/bin/sh",
                        'if [ "$1" = -l ]; then',
                        "  echo 'Source0: https://example.invalid/missing.tar.gz'",
                        "else",
                        "  echo 'Downloading: https://example.invalid/missing.tar.gz'",
                        "  echo 'Download failed:'",
                        "  echo '404 Client Error'",
                        "fi",
                        "exit 0",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            spectool.chmod(0o755)
            rpmbuild_called = root / "rpmbuild-called"
            rpmbuild = bin_dir / "rpmbuild"
            rpmbuild.write_text(
                "#!/bin/sh\ntouch \"$RPMBUILD_CALLED\"\n",
                encoding="utf-8",
            )
            rpmbuild.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["RPMBUILD_CALLED"] = str(rpmbuild_called)

            result = subprocess.run(
                ["/bin/bash"],
                input=script,
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Download failed:", result.stdout)
            self.assertFalse(rpmbuild_called.exists())

    def test_spec_output_containerfile_builds_each_spec_stage(self) -> None:
        workspace_dir = Path("/tmp/build/workspace")
        staged_specs = (
            StagedSpec(
                spec=SpecBuild(spec="scx-tools.spec"),
                spec_path=workspace_dir / "tools" / "scx-tools.spec",
                source_dir=workspace_dir / "tools",
                packages=("scx-tools",),
                targets=("x86_64",),
            ),
            StagedSpec(
                spec=SpecBuild(spec="scx-scheds.spec"),
                spec_path=workspace_dir / "scheds" / "scx-scheds.spec",
                source_dir=workspace_dir / "scheds",
                packages=("scx-scheds",),
                targets=("x86_64",),
            ),
        )

        containerfile = _render_specs_build_output_containerfile(
            orchestrator="localhost/builders:f44",
            staged_specs=staged_specs,
            workspace_dir=workspace_dir,
            card_env={"releasever": "44"},
            arch="x86_64",
            rpmbuild_defines=("flatpak 1", "_prefix /app"),
            ccache_dir=Path("/cache/ccache"),
        )

        self.assertIn("FROM localhost/builders:f44 AS spec_scx_tools_0", containerfile)
        self.assertIn("FROM localhost/builders:f44 AS spec_scx_scheds_1", containerfile)
        self.assertIn("#\n# Build: spec_scx_tools_0\n#\n", containerfile)
        self.assertIn(
            "LUDOS_SPEC_BUILD_spec_scx_tools_0\n\n#\n# Build: spec_scx_scheds_1\n#\n",
            containerfile,
        )
        self.assertIn(
            'COPY "spec-workspaces/spec_scx_tools_0/" "/workspace/tools/"',
            containerfile,
        )
        self.assertIn(
            'COPY "spec-workspaces/spec_scx_scheds_1/" "/workspace/scheds/"',
            containerfile,
        )
        self.assertNotIn("COPY workspace/ /workspace/", containerfile)
        self.assertIn("RUN env CCACHE_DIR=/cache/ccache", containerfile)
        self.assertNotIn("\nENV CCACHE_DIR=", containerfile)
        self.assertIn("export PATH=/usr/lib64/ccache:/usr/lib/ccache:$PATH", containerfile)
        self.assertIn('spec_source_cache="$source_cache/scx_tools"', containerfile)
        self.assertIn('spec_source_cache="$source_cache/scx_scheds"', containerfile)
        self.assertIn("--define 'flatpak 1'", containerfile)
        self.assertIn("--define '_prefix /app'", containerfile)
        self.assertIn("FROM scratch", containerfile)
        self.assertIn("COPY rpms/ /rpms/", containerfile)
        self.assertIn("COPY files/ /files/", containerfile)
        self.assertIn("COPY --from=spec_scx_tools_0 /rpms/ /rpms/", containerfile)
        self.assertIn("COPY --from=spec_scx_scheds_1 /files/ /files/", containerfile)
        self.assertNotIn("auth_secret", containerfile)

    def test_unknown_auth_type_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staged_specs = (
                StagedSpec(
                    spec=SpecBuild(spec="private.spec", auth="unknown"),
                    spec_path=root / "private.spec",
                    source_dir=root,
                    packages=("private",),
                    targets=("x86_64",),
                ),
            )

            with (
                patch("ludos.build._stage_card_specs", return_value=staged_specs),
                self.assertRaisesRegex(ConfigError, "unknown spec auth type: unknown"),
            ):
                _build_specs_output_image(
                    podman="podman",
                    orchestrator="builder",
                    image="output",
                    build_dir=root / "build",
                    artifact_cache_dir=root / "artifacts",
                    ccache_dir=None,
                    card_name="private",
                    card_source=root / "card.yml",
                    card_env={},
                    specs=(SpecBuild(spec="private.spec", auth="unknown"),),
                    prepare_script="",
                    arch="x86_64",
                    spec_source_cache_dir=root / "spec-cache",
                    source_revisions=tuple(),
                )

    def test_auth_mounts_credentials_for_the_requested_spec(self) -> None:
        workspace_dir = Path("/tmp/build/workspace")
        staged_specs = (
            StagedSpec(
                spec=SpecBuild(spec="spaces.spec", auth="github"),
                spec_path=workspace_dir / "spaces.spec",
                source_dir=workspace_dir,
                packages=("spaces",),
                targets=("x86_64",),
            ),
        )

        containerfile = _render_specs_build_output_containerfile(
            orchestrator="localhost/builders:f44",
            staged_specs=staged_specs,
            workspace_dir=workspace_dir,
            card_env={"releasever": "44"},
            arch="x86_64",
            rpmbuild_defines=tuple(),
            ccache_dir=None,
        )

        run_lines = [line for line in containerfile.splitlines() if line.startswith("RUN ")]
        build_run = next(line for line in run_lines if "LUDOS_SPEC_BUILD" in line)
        self.assertIn("--mount=type=secret,id=auth_secret", build_run)
        self.assertIn(
            'Authorization: Bearer $(cat /run/secrets/auth_secret)', containerfile
        )
        self.assertIn("https://api.github.com/repos/", containerfile)
        self.assertIn("archive/refs/tags/", containerfile)
        self.assertIn('--transform "s|^\\.|$github_root|"', containerfile)
        self.assertNotIn("NETRC", containerfile)
        self.assertNotIn("machine github.com", containerfile)

    def test_spec_stage_context_excludes_sibling_specs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace_dir = root / "workspace"
            source_dir = workspace_dir / "specs"
            source_dir.mkdir(parents=True)
            (source_dir / "scx-tools.spec").write_text("Name: scx-tools\n", encoding="utf-8")
            (source_dir / "scx-scheds.spec").write_text("Name: scx-scheds\n", encoding="utf-8")
            (source_dir / "source.tar.xz").write_text("source\n", encoding="utf-8")
            staged_specs = (
                StagedSpec(
                    spec=SpecBuild(spec="scx-tools.spec"),
                    spec_path=source_dir / "scx-tools.spec",
                    source_dir=source_dir,
                    packages=("scx-tools",),
                    targets=("x86_64",),
                ),
            )

            _stage_spec_build_contexts(root / "build", workspace_dir, staged_specs)

            stage_dir = root / "build" / "spec-workspaces" / "spec_scx_tools_0"
            self.assertTrue((stage_dir / "scx-tools.spec").is_file())
            self.assertTrue((stage_dir / "source.tar.xz").is_file())
            self.assertFalse((stage_dir / "scx-scheds.spec").exists())

    def test_card_build_output_containerfile_runs_build_stage(self) -> None:
        containerfile = _render_card_build_output_containerfile(
            orchestrator="localhost/builders:f44",
            card_env={"FOO": "bar baz"},
            build_script="mkdir -p build/RPMS\nprintf data > /files/output.txt",
            ccache_dir=Path("/cache/ccache"),
        )

        self.assertIn("FROM localhost/builders:f44 AS build", containerfile)
        self.assertIn(
            "RUN env CCACHE_DIR=/cache/ccache "
            "CCACHE_SLOPPINESS=include_file_ctime,include_file_mtime,time_macros "
            "FOO='bar baz'",
            containerfile,
        )
        self.assertNotIn("\nENV FOO=", containerfile)
        self.assertIn("COPY workspace/ /workspace/", containerfile)
        self.assertIn("RUN mkdir -p /rpms /files /cache/artifacts /cache/podman", containerfile)
        self.assertIn("printf data > /files/output.txt", containerfile)
        self.assertIn("find /workspace/build/RPMS -type f -name '*.rpm' ! -name '*.src.rpm'", containerfile)
        self.assertIn("FROM scratch", containerfile)
        self.assertIn("COPY --from=build /rpms/ /rpms/", containerfile)
        self.assertIn("COPY --from=build /files/ /files/", containerfile)

    def test_cross_arch_variants_are_discovered_from_builddep_dependencies(self) -> None:
        package_id_by_nevra: dict[str, tuple[str, str]] = {}
        workspace_dir = Path("/workspace")
        staged_specs = (
            StagedSpec(
                spec=SpecBuild(spec="mesa.spec"),
                spec_path=workspace_dir / "mesa.spec",
                source_dir=workspace_dir,
                packages=("mesa-libGL.i686",),
                targets=("i686",),
            ),
        )
        build_requires_include_dependencies = []

        def build_requires(*args, include_dependencies: bool) -> tuple[str, ...]:
            build_requires_include_dependencies.append(include_dependencies)
            package_id_by_nevra[
                "cargo-rpm-macros-0:28.4-3.fc44.noarch"
            ] = ("cargo-rpm-macros", "noarch")
            if include_dependencies:
                package_id_by_nevra[
                    "rust-std-static-0:1.94.1-1.fc44.x86_64"
                ] = ("rust-std-static", "x86_64")
                package_id_by_nevra[
                    "libstdc++-devel-0:16.0.1-0.10.fc44.x86_64"
                ] = ("libstdc++-devel", "x86_64")
                return (
                    "cargo-rpm-macros-0:28.4-3.fc44.noarch",
                    "libstdc++-devel-0:16.0.1-0.10.fc44.x86_64",
                    "rust-std-static-0:1.94.1-1.fc44.x86_64",
                )
            return ("cargo-rpm-macros-0:28.4-3.fc44.noarch",)

        def arch_variants(*args) -> tuple[str, ...]:
            packages = args[2]
            self.assertIn("libstdc++-devel-0:16.0.1-0.10.fc44.x86_64", packages)
            self.assertIn("rust-std-static-0:1.94.1-1.fc44.x86_64", packages)
            return (
                "libstdc++-devel-0:16.0.1-0.10.fc44.i686",
                "rust-std-static-0:1.94.1-1.fc44.i686",
            )

        with (
            patch("ludos.build._resolve_spec_build_requires", build_requires),
            patch("ludos.build._resolve_package_arch_variants", arch_variants),
        ):
            packages = _resolve_staged_spec_builder_packages(
                [],
                "44",
                workspace_dir,
                staged_specs,
                "x86_64",
                package_id_by_nevra,
                Path("/cache"),
                (),
                card_name="mesa",
            )

        self.assertEqual(build_requires_include_dependencies, [False, True])
        self.assertEqual(
            packages,
            (
                "cargo-rpm-macros-0:28.4-3.fc44.noarch",
                "libstdc++-devel-0:16.0.1-0.10.fc44.i686",
                "rust-std-static-0:1.94.1-1.fc44.i686",
            ),
        )

    def test_mixed_arch_builds_retain_native_builddep_dependencies(self) -> None:
        workspace_dir = Path("/workspace")
        staged_specs = (
            StagedSpec(
                spec=SpecBuild(spec="mangohud.spec"),
                spec_path=workspace_dir / "mangohud.spec",
                source_dir=workspace_dir,
                packages=("mangohud", "mangohud.i686"),
                targets=("x86_64", "i686"),
            ),
        )
        build_requires_calls = []

        def build_requires(*args, include_dependencies: bool) -> tuple[str, ...]:
            target = args[4]
            build_requires_calls.append((target, include_dependencies))
            if target == "x86_64":
                return (
                    "wayland-devel-0:1.24.0-3.fc44.x86_64",
                    "libffi-devel-0:3.4.8-2.fc44.x86_64",
                )
            if include_dependencies:
                return (
                    "wayland-devel-0:1.24.0-3.fc44.i686",
                    "libffi-devel-0:3.4.8-2.fc44.x86_64",
                )
            return ("wayland-devel-0:1.24.0-3.fc44.i686",)

        with (
            patch("ludos.build._resolve_spec_build_requires", build_requires),
            patch(
                "ludos.build._resolve_package_arch_variants",
                return_value=("libffi-devel-0:3.4.8-2.fc44.i686",),
            ),
        ):
            packages = _resolve_staged_spec_builder_packages(
                [],
                "44",
                workspace_dir,
                staged_specs,
                "x86_64",
                {},
                Path("/cache"),
                (),
                card_name="gaming",
            )

        self.assertEqual(
            build_requires_calls,
            [("x86_64", True), ("i686", False), ("i686", True)],
        )
        self.assertEqual(
            packages,
            (
                "wayland-devel-0:1.24.0-3.fc44.x86_64",
                "libffi-devel-0:3.4.8-2.fc44.x86_64",
                "wayland-devel-0:1.24.0-3.fc44.i686",
                "libffi-devel-0:3.4.8-2.fc44.i686",
            ),
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ludos.__main__ import build_command, build_parser
from ludos.flatpaks import (
    FlatpakCard,
    build_flatpaks,
    _flatpak_appstream_labels_with_remote_icon,
    _flatpak_build_env,
    _flatpak_commit_metadata_labels,
    _flatpak_metadata,
    _ensure_flatpak_images,
    _prepare_flatpak_build_plan,
    _flatpak_rpmbuild_defines,
    _run_flatpak_image_build,
    _stage_flatpak_files,
    _substitute_specs,
    _write_flatpak_containerfile,
)
from ludos.model import (
    ConfigError,
    FlatpakImagesConfig,
    Manifest,
    ManifestRuntime,
    SpecBuild,
    validate_manifest,
)


class FlatpakParserTests(unittest.TestCase):
    def test_build_parser_accepts_flatpak_target(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["build", "--flatpak", "flatpaks/kate", "anatase.yml"]
        )

        self.assertEqual(args.flatpak, Path("flatpaks/kate"))
        self.assertIsNone(args.card)
        self.assertFalse(args.flatpaks)

    def test_appstream_labels_append_remote_icon_from_project_uri(self) -> None:
        labels = _flatpak_appstream_labels_with_remote_icon(
            {
                "org.freedesktop.appstream.appdata": (
                    "<components><component type=\"desktop-application\">"
                    "<id>org.anatase.ArchiveManager</id>"
                    "<icon type=\"cached\" width=\"128\" height=\"128\">"
                    "org.anatase.ArchiveManager"
                    "</icon>"
                    "</component></components>"
                ),
                "org.freedesktop.appstream.icon-128": "data:image/png;base64,AA==",
            },
            "org.anatase.ArchiveManager",
            "https://flatpaks.anatase.org/icons/",
        )

        self.assertIn(
            '<icon type="remote" width="128" height="128">'
            "https://flatpaks.anatase.org/icons/128x128/"
            "org.anatase.ArchiveManager.png"
            "</icon>",
            labels["org.freedesktop.appstream.appdata"],
        )

    def test_build_parser_accepts_flatpaks_target(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["build", "--flatpaks", "anatase.yml"])

        self.assertTrue(args.flatpaks)
        self.assertIsNone(args.flatpak)
        self.assertIsNone(args.card)

    def test_build_parser_rejects_card_and_flatpak_together(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "build",
                    "--card",
                    "cards/base",
                    "--flatpak",
                    "flatpaks/kate",
                    "anatase.yml",
                ]
            )

    def test_build_parser_rejects_flatpaks_and_flatpak_together(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "build",
                    "--flatpaks",
                    "--flatpak",
                    "flatpaks/kate",
                    "anatase.yml",
                ]
            )

    def test_build_command_calls_build_flatpaks(self) -> None:
        args = build_parser().parse_args(
            [
                "build",
                "--flatpaks",
                "--cache",
                "--cache-dir",
                "cache",
                "--version",
                "20260629",
                "--no-ccache",
                "anatase.yml",
            ]
        )
        result = SimpleNamespace(
            ref="app/org.anatase.TextEditor/x86_64/stable",
            image="localhost/flatpaks:f44-x86_64-kate",
            latest_image="localhost/flatpaks:f44-x86_64-kate",
        )

        with (
            patch("ludos.__main__.show_logo"),
            patch("ludos.__main__.build_flatpaks", return_value=(result,)) as build,
            patch("ludos.__main__.log") as log,
        ):
            exit_code = build_command(args)

        self.assertEqual(exit_code, 0)
        build.assert_called_once_with(
            Path("anatase.yml"),
            cards_dir=None,
            cache_dir=Path("cache"),
            cache_version="20260629",
            cache_only=True,
            ccache=False,
        )
        self.assertIn(
            "Built flatpak app/org.anatase.TextEditor/x86_64/stable",
            log.call_args.args[0],
        )
        self.assertNotIn("latest:", log.call_args.args[0])

    def test_manifest_parser_accepts_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self._write_manifest(
                Path(temp),
                flatpaks=("flatpaks/kate",),
            )

            manifest = Manifest.from_file(manifest_path)

        self.assertEqual(manifest.flatpaks, ("flatpaks/kate",))

    def test_manifest_parser_accepts_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self._write_manifest(
                Path(temp),
                flatpaks=("flatpaks/kate",),
            )

            manifest = Manifest.from_file(manifest_path)

        self.assertEqual(
            manifest.runtime,
            ManifestRuntime(
                id="org.anatase.Platform",
                repo="runtime",
                branch="stable",
                title="Anatase Test Runtime",
                author="Anatase Test Authors",
                description="Anatase Platform runtime for tests.",
                license="LicenseRef-Anatase-Test",
                image="https://flatpaks.example.test/icons/128x128/org.anatase.Platform.png",
            ),
        )

    def test_manifest_parser_requires_runtime_fields_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = self._write_manifest(
                root,
                flatpaks=("flatpaks/kate",),
            )
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8").replace(
                    "  repo: runtime\n",
                    "",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "repo"):
                Manifest.from_file(manifest_path)

    def test_validate_manifest_reports_missing_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self._write_manifest(
                Path(temp),
                flatpaks=("flatpaks/kate", "flatpaks/missing"),
                create_flatpaks=("flatpaks/kate",),
            )

            validation = validate_manifest(manifest_path)

        self.assertEqual(validation.missing_flatpaks, ("flatpaks/missing",))
        self.assertFalse(validation.ok)

    def test_build_flatpaks_runs_grouped_phases_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "anatase.yml"
            context = SimpleNamespace(
                validation=SimpleNamespace(
                    missing_flatpaks=tuple(),
                    manifest=SimpleNamespace(
                        flatpaks=("flatpaks/kate", "flatpaks/ark/card.yaml")
                    )
                ),
                root_dir=root,
                dnf_workspace_dir=root / "dnf-workspace",
                podman="podman",
            )
            plans = (SimpleNamespace(name="kate"), SimpleNamespace(name="ark"))
            results = (SimpleNamespace(ref="kate"), SimpleNamespace(ref="ark"))
            events: list[object] = []

            def prepare(
                _context: object,
                flatpak_path: Path,
                **_kwargs: object,
            ) -> object:
                events.append(("prepare", flatpak_path))
                return plans[len(events) - 1]

            def builders(
                _context: object,
                phase_plans: tuple[object, ...],
                **_kwargs: object,
            ) -> None:
                events.append(("builders", phase_plans))

            def rpms(
                _context: object,
                phase_plans: tuple[object, ...],
                **_kwargs: object,
            ) -> tuple[object, ...]:
                events.append(("rpms", phase_plans))
                return phase_plans

            def images(
                _context: object,
                phase_plans: tuple[object, ...],
                **_kwargs: object,
            ) -> tuple[object, ...]:
                events.append(("images", phase_plans))
                return results

            with (
                patch(
                    "ludos.flatpaks.resolve_manifest_context",
                    return_value=context,
                ),
                patch(
                    "ludos.flatpaks._prepare_flatpak_build_plan",
                    side_effect=prepare,
                ),
                patch(
                    "ludos.flatpaks._ensure_flatpak_builders",
                    side_effect=builders,
                ),
                patch(
                    "ludos.flatpaks._ensure_flatpak_rpm_builds",
                    side_effect=rpms,
                ),
                patch(
                    "ludos.flatpaks._ensure_flatpak_images",
                    side_effect=images,
                ),
            ):
                built = build_flatpaks(manifest_path)

        self.assertEqual(built, results)
        self.assertEqual(
            events,
            [
                ("prepare", root / "flatpaks/kate"),
                ("prepare", root / "flatpaks/ark/card.yaml"),
                ("builders", plans),
                ("rpms", plans),
                ("images", plans),
            ],
        )

    def test_build_flatpaks_rejects_manifest_without_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = SimpleNamespace(
                validation=SimpleNamespace(
                    missing_flatpaks=tuple(),
                    manifest=SimpleNamespace(flatpaks=tuple()),
                ),
                root_dir=root,
                dnf_workspace_dir=root / "dnf-workspace",
                podman="podman",
            )

            with (
                patch("ludos.flatpaks.resolve_manifest_context", return_value=context),
                self.assertRaisesRegex(ConfigError, "flatpaks"),
            ):
                build_flatpaks(root / "anatase.yml")

    def test_build_flatpaks_rejects_missing_manifest_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = SimpleNamespace(
                validation=SimpleNamespace(
                    missing_flatpaks=("flatpaks/missing",),
                    manifest=SimpleNamespace(flatpaks=("flatpaks/missing",)),
                ),
                root_dir=root,
                dnf_workspace_dir=root / "dnf-workspace",
                podman="podman",
            )

            with (
                patch("ludos.flatpaks.resolve_manifest_context", return_value=context),
                patch("ludos.flatpaks._build_flatpak_with_context") as build,
                self.assertRaisesRegex(ConfigError, "missing flatpak definitions"),
            ):
                build_flatpaks(root / "anatase.yml")

        build.assert_not_called()

    def test_prepare_flatpak_build_plan_uses_manifest_runtime_branch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card_path = self._write_flatpak_card(
                root,
                "kate",
                app_id="org.anatase.TextEditor",
                command="kate",
            )
            context = self._flatpak_context(root, runtime_branch="beta")

            with self._mock_plan_dependencies():
                plan = _prepare_flatpak_build_plan(
                    context,
                    card_path.parent,
                    cache_only=False,
                )

        self.assertEqual(plan.app_name, "kate")
        self.assertEqual(plan.branch, "beta")
        self.assertEqual(plan.app_ref, "app/org.anatase.TextEditor/x86_64/beta")
        self.assertIn("runtime=org.anatase.Platform/x86_64/beta", plan.metadata)
        self.assertIn("sdk=org.anatase.ludos.Sdk/x86_64/beta", plan.metadata)
        self.assertEqual(plan.output_image, "localhost/flatpaks:f44-x86_64-kate")
        self.assertEqual(plan.latest_image, plan.output_image)

    def test_prepare_flatpak_build_plan_hashes_only_scoped_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flatpak_dir = root / "flatpaks" / "kate"
            flatpak_dir.mkdir(parents=True)
            card_path = flatpak_dir / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.anatase.TextEditor
  command: kate
build-deps:
  - rpm-build
env:
  EXTRA_VERSION: $tag-extra
specs:
  - spec: git+https://example.test/kate:kate.spec#branch=$EXTRA_VERSION
    packages: [kate]
""",
                encoding="utf-8",
            )
            context = self._flatpak_context(root)
            context.manifest_env = {
                "arch": "x86_64",
                "releasever": "44",
                "tag": "44",
                "version": "20260629",
                "distro": "f44-x86_64",
                "UNUSED": "changes-should-not-matter",
            }
            captured: dict[str, object] = {}

            def card_specs_hash(
                _card_source: Path,
                specs: tuple[SpecBuild, ...],
                card_env: dict[str, str],
                *_args: object,
                **_kwargs: object,
            ) -> tuple[str, dict[str, str]]:
                captured["hash_specs"] = specs
                captured["hash_env"] = dict(card_env)
                return "spechash", {}

            def stage_card_specs(
                *,
                specs: tuple[SpecBuild, ...],
                card_env: dict[str, str],
                **_kwargs: object,
            ) -> tuple[object, ...]:
                captured["stage_specs"] = specs
                captured["stage_env"] = dict(card_env)
                return tuple()

            with patch.multiple(
                "ludos.flatpaks",
                _card_specs_hash=card_specs_hash,
                _stage_card_specs=stage_card_specs,
                _resolve_staged_spec_builder_packages=lambda *args, **kwargs: tuple(),
                _resolve_packages=lambda *args, **kwargs: ("rpm-build",),
            ):
                plan = _prepare_flatpak_build_plan(
                    context,
                    card_path.parent,
                    cache_only=False,
                )

        expected_env = {
            "arch": "x86_64",
            "releasever": "44",
            "EXTRA_VERSION": "44-extra",
        }
        self.assertEqual(plan.substitution_env, expected_env)
        self.assertEqual(plan.build_env, expected_env)
        self.assertEqual(captured["hash_env"], expected_env)
        self.assertEqual(captured["stage_env"], expected_env)
        self.assertNotIn("distro", captured["hash_env"])
        self.assertNotIn("version", captured["hash_env"])
        self.assertEqual(
            captured["hash_specs"][0].spec,
            "git+https://example.test/kate:kate.spec#branch=44-extra",
        )
        self.assertEqual(captured["hash_specs"], captured["stage_specs"])

    def test_prepare_flatpak_build_plan_names_multiple_apps_from_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kate_card = self._write_flatpak_card(
                root,
                "kate",
                app_id="org.anatase.TextEditor",
                command="kate",
            )
            ark_card = self._write_flatpak_card(
                root,
                "ark",
                app_id="org.anatase.ArchiveManager",
                command="ark",
            )
            context = self._flatpak_context(root)

            with self._mock_plan_dependencies():
                plans = (
                    _prepare_flatpak_build_plan(
                        context,
                        kate_card.parent,
                        cache_only=False,
                    ),
                    _prepare_flatpak_build_plan(
                        context,
                        ark_card.parent,
                        cache_only=False,
                    ),
                )

        self.assertEqual(
            tuple(plan.output_image for plan in plans),
            (
                "localhost/flatpaks:f44-x86_64-kate",
                "localhost/flatpaks:f44-x86_64-ark",
            ),
        )
        self.assertEqual(tuple(plan.branch for plan in plans), ("stable", "stable"))
        self.assertTrue(
            all(
                plan.output_image.startswith("localhost/flatpaks:")
                for plan in plans
            )
        )

    def test_prepare_flatpak_build_plan_rejects_missing_manifest_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card_path = self._write_flatpak_card(
                root,
                "kate",
                app_id="org.anatase.TextEditor",
                command="kate",
            )
            context = self._flatpak_context(root)
            context.validation.manifest.runtime = None

            with self.assertRaisesRegex(ConfigError, "runtime"):
                _prepare_flatpak_build_plan(
                    context,
                    card_path.parent,
                    cache_only=False,
                )

    def test_cache_only_still_builds_final_flatpak_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            card_path = self._write_flatpak_card(
                root,
                "kate",
                app_id="org.anatase.TextEditor",
                command="kate",
            )
            card = FlatpakCard.from_file(card_path)
            context = self._flatpak_context(root)
            context.buildah = "buildah"
            context.flatpak_images = FlatpakImagesConfig()
            plan = SimpleNamespace(
                final_build_dir=root / "build" / "flatpak",
                flatpak_dir=card_path.parent,
                card=card,
                build_image="localhost/builds:f44-x86_64-flatpak-kate",
                output_image="localhost/flatpaks:f44-x86_64-kate",
                metadata="[Application]\nname=org.anatase.TextEditor\n",
                app_ref="app/org.anatase.TextEditor/x86_64/stable",
                branch="stable",
                flatpak_arch="x86_64",
                app_id="org.anatase.TextEditor",
                latest_image="localhost/flatpaks:f44-x86_64-kate",
                builder_image="localhost/builders:f44-x86_64-flatpak-kate",
            )

            with (
                patch("ludos.flatpaks._write_flatpak_containerfile") as write,
                patch("ludos.flatpaks._run_flatpak_image_build") as run,
            ):
                results = _ensure_flatpak_images(
                    context,
                    (plan,),
                    cache_only=True,
                )

        write.assert_called_once()
        run.assert_called_once_with(
            "podman",
            "buildah",
            plan.final_build_dir,
            "localhost/flatpaks:f44-x86_64-kate",
            "[Application]\nname=org.anatase.TextEditor\n",
            "org.anatase.TextEditor",
            flatpak_images=context.flatpak_images,
        )
        self.assertEqual(results[0].image, "localhost/flatpaks:f44-x86_64-kate")

    def test_card_parser_accepts_metadata_hooks_and_specs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            card_path = Path(temp) / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.kde.kate
  command: kate
  rename: Text Editor (Kate)
  rename-author: "%s (packaged by Anatase)"
  finish-args: |-
    --device=dri
  rename-icon: kate
build-deps:
  - rpm-build
  - flatpak-rpm-macros
env:
  EXTRA_VERSION: $releasever-app
specs:
  - spec: git+https://example.test/kate:kate.spec#branch=f$releasever
    packages:
      - kate
files:
  - app/share/test.txt
postprocess: |
  true
""",
                encoding="utf-8",
            )

            card = FlatpakCard.from_file(card_path)

        self.assertEqual(card.flatpak.app_id, "org.kde.kate")
        self.assertEqual(card.flatpak.command, "kate")
        self.assertEqual(card.flatpak.rename, "Text Editor (Kate)")
        self.assertEqual(card.flatpak.rename_author, "%s (packaged by Anatase)")
        self.assertEqual(card.flatpak.rename_icon, "kate")
        self.assertEqual(card.env, {"EXTRA_VERSION": "$releasever-app"})
        self.assertEqual(card.build_deps, ("rpm-build", "flatpak-rpm-macros"))
        self.assertEqual(card.specs[0].spec, "git+https://example.test/kate:kate.spec#branch=f$releasever")
        self.assertEqual(card.files, ("app/share/test.txt",))
        self.assertEqual(card.postprocess.strip(), "true")

    def test_card_parser_rejects_fedora_cleanup_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            card_path = Path(temp) / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.kde.kate
  command: kate
  cleanup-commands: |
    true
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "cleanup-commands"):
                FlatpakCard.from_file(card_path)

    def test_card_parser_rejects_user_runtime_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            card_path = Path(temp) / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.kde.kate
  command: kate
  runtime-version: f44
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "runtime-version"):
                FlatpakCard.from_file(card_path)

    def test_spec_source_substitution_updates_git_branch(self) -> None:
        specs = (
            SpecBuild(
                spec="git+https://example.test/kate:kate.spec#branch=f$releasever",
                packages={"*": ("kate",)},
            ),
        )

        substituted = _substitute_specs(specs, {"releasever": "44"})

        self.assertEqual(
            substituted[0].spec,
            "git+https://example.test/kate:kate.spec#branch=f44",
        )

    def _write_manifest(
        self,
        root: Path,
        *,
        flatpaks: tuple[str, ...],
        create_flatpaks: tuple[str, ...] | None = None,
    ) -> Path:
        create_flatpaks = flatpaks if create_flatpaks is None else create_flatpaks
        (root / "cards").mkdir()
        (root / "cards/bootstrap.yml").write_text("version: 1\n", encoding="utf-8")
        (root / "cards/base.yml").write_text("version: 1\n", encoding="utf-8")
        for flatpak in create_flatpaks:
            flatpak_path = root / flatpak
            card_path = (
                flatpak_path if flatpak_path.suffix else flatpak_path / "card.yaml"
            )
            card_path.parent.mkdir(parents=True, exist_ok=True)
            card_path.write_text("version: 1\n", encoding="utf-8")
        manifest_path = root / "anatase.yml"
        manifest_path.write_text(
            "\n".join(
                [
                    "version: 1",
                    "name: Anatase Test",
                    "releasever: '44'",
                    "distro: f$releasever-$arch",
                    "orchestrator: quay.io/fedora/fedora:$releasever",
                    "runtime:",
                    "  id: org.anatase.Platform",
                    "  repo: runtime",
                    "  branch: stable",
                    "  title: Anatase Test Runtime",
                    "  author: Anatase Test Authors",
                    "  description: Anatase Platform runtime for tests.",
                    "  license: LicenseRef-Anatase-Test",
                    "  image: https://flatpaks.example.test/icons/128x128/org.anatase.Platform.png",
                    "bootstrap: cards/bootstrap.yml",
                    "repos: []",
                    "cards:",
                    "  - cards/base.yml",
                    "flatpaks:",
                    *(f"  - {flatpak}" for flatpak in flatpaks),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return manifest_path

    def _write_flatpak_card(
        self,
        root: Path,
        name: str,
        *,
        app_id: str,
        command: str,
    ) -> Path:
        flatpak_dir = root / "flatpaks" / name
        flatpak_dir.mkdir(parents=True)
        card_path = flatpak_dir / "card.yaml"
        card_path.write_text(
            f"""
version: 1
flatpak:
  id: {app_id}
  command: {command}
build-deps:
  - rpm-build
specs:
  - spec: {command}.spec
    packages: [{command}]
""",
            encoding="utf-8",
        )
        return card_path

    def _flatpak_context(
        self,
        root: Path,
        *,
        runtime_branch: str = "stable",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            arch="x86_64",
            build_artifact_cache_dir=root / "build-artifacts",
            distro="f44-x86_64",
            distro_cache_dir=root / "cache" / "f44-x86_64",
            dnf_cache_dir=root / "dnf" / "cache",
            dnf_log_dir=root / "dnf" / "log",
            dnf_persist_dir=root / "dnf" / "persist",
            dnf_resolve_dir=root / "dnf" / "resolves",
            local_prefix="",
            manifest_env={"releasever": "44", "arch": "x86_64"},
            orchestrator="localhost/orchestrator:f44",
            package_dir=root / "packages",
            podman="podman",
            releasever="44",
            repo_dir=root / "repos",
            repo_images=tuple(),
            root_dir=root,
            spec_source_cache_dir=root / "spec-sources",
            validation=SimpleNamespace(
                manifest=SimpleNamespace(
                    runtime=ManifestRuntime(
                        id="org.anatase.Platform",
                        repo="runtime",
                        branch=runtime_branch,
                    ),
                    source=root / "anatase.yml",
                )
            ),
        )

    def _mock_plan_dependencies(self):
        return patch.multiple(
            "ludos.flatpaks",
            _card_specs_hash=lambda *args, **kwargs: ("spechash", {}),
            _stage_card_specs=lambda *args, **kwargs: tuple(),
            _resolve_staged_spec_builder_packages=lambda *args, **kwargs: tuple(),
            _resolve_packages=lambda *args, **kwargs: ("rpm-build",),
        )


class FlatpakAssemblyTests(unittest.TestCase):
    def test_flatpak_build_env_defaults_to_arch_and_releasever_only(self) -> None:
        env = _flatpak_build_env(
            {
                "arch": "x86_64",
                "releasever": "44",
                "tag": "44.20260629",
                "version": "20260629",
            },
            {},
        )

        self.assertEqual(env, {"arch": "x86_64", "releasever": "44"})

    def test_flatpak_build_env_substitutes_explicit_entries(self) -> None:
        env = _flatpak_build_env(
            {
                "arch": "x86_64",
                "releasever": "44",
                "tag": "44.20260629",
                "version": "20260629",
            },
            {"EXTRA_VERSION": "$tag", "releasever": "$releasever-app"},
        )

        self.assertEqual(
            env,
            {
                "arch": "x86_64",
                "releasever": "44-app",
                "EXTRA_VERSION": "44.20260629",
            },
        )

    def test_flatpak_rpmbuild_defines_match_fedora_flatpak_macros(self) -> None:
        defines = _flatpak_rpmbuild_defines()

        self.assertIn("flatpak 1", defines)
        self.assertIn("distcore .fc%{fedora}app", defines)
        self.assertIn("_prefix /app", defines)
        self.assertIn("_sysconfdir %{_prefix}/etc", defines)
        self.assertIn("_localstatedir %{_prefix}/var", defines)
        self.assertTrue(
            any(
                define.startswith("build_ldflags ")
                and "-L%{_prefix}/lib64" in define
                for define in defines
            )
        )
        self.assertIn("__brp_check_rpaths %{nil}", defines)
        self.assertIn("debug_package %{nil}", defines)
        self.assertNotIn("_lib lib", defines)
        self.assertNotIn("_libdir /app/lib", defines)

    def test_metadata_translates_finish_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            card_path = Path(temp) / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.kde.kate
  command: kate
  finish-args: |-
    --device=dri
    --filesystem=host
    --share=ipc
    --socket=wayland
    --talk-name=org.kde.KGlobalSettings
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
""",
                encoding="utf-8",
            )
            card = FlatpakCard.from_file(card_path)

        metadata = _flatpak_metadata(
            card.flatpak,
            branch="f44",
            flatpak_arch="x86_64",
            runtime_id="org.anatase.Platform",
        )

        self.assertIn("runtime=org.anatase.Platform/x86_64/f44", metadata)
        self.assertIn("sdk=org.anatase.ludos.Sdk/x86_64/f44", metadata)
        self.assertIn("command=kate", metadata)
        self.assertIn("devices=dri;", metadata)
        self.assertIn("filesystems=host;", metadata)
        self.assertIn("shared=ipc;", metadata)
        self.assertIn("sockets=wayland;", metadata)
        self.assertIn("[Session Bus Policy]", metadata)
        self.assertIn("org.kde.KGlobalSettings=talk", metadata)

    def test_metadata_uses_configured_command_for_renamed_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            card_path = Path(temp) / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.anatase.TextEditor
  command: kate
  rename: Text Editor (Kate)
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
""",
                encoding="utf-8",
            )
            card = FlatpakCard.from_file(card_path)

        metadata = _flatpak_metadata(
            card.flatpak,
            branch="f44",
            flatpak_arch="x86_64",
            runtime_id="org.anatase.Platform",
        )

        self.assertIn("name=org.anatase.TextEditor", metadata)
        self.assertIn("command=kate", metadata)

    def test_containerfile_uses_orchestrator_and_applies_final_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flatpak_dir = root / "kate"
            flatpak_dir.mkdir()
            (flatpak_dir / "payload.txt").write_text("payload", encoding="utf-8")
            card_path = flatpak_dir / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.kde.kate
  command: kate
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
files:
  - app/share/payload.txt::payload.txt
postprocess: |
  touch postprocessed
""",
                encoding="utf-8",
            )
            card = FlatpakCard.from_file(card_path)
            build_dir = root / "build"

            _write_flatpak_containerfile(
                final_build_dir=build_dir,
                flatpak_dir=flatpak_dir,
                card=card,
                build_image="localhost/builds:f44-flatpak-kate",
                orchestrator="localhost/orchestrator:f44",
                metadata="metadata-body\n",
                app_ref="app/org.kde.kate/x86_64/f44",
                branch="f44",
                flatpak_arch="x86_64",
            )

            containerfile = (build_dir / "Containerfile").read_text(encoding="utf-8")
            labels = dict(
                json.loads((build_dir / "labels.json").read_text(encoding="utf-8"))
            )

        self.assertIn("FROM localhost/orchestrator:f44 AS build", containerfile)
        self.assertIn("COPY --from=rpms /rpms /rpms", containerfile)
        self.assertIn("COPY files/ /flatpak/", containerfile)
        self.assertIn(
            "rpm --root /flatpak -Uvh --allfiles --nodeps --noscripts --notriggers /rpms/*.rpm",
            containerfile,
        )
        self.assertIn("warning: removing /usr entries from app flatpak payload", containerfile)
        self.assertIn("rm -rf /flatpak/usr", containerfile)
        self.assertIn("WORKDIR /flatpak", containerfile)
        self.assertIn("touch postprocessed", containerfile)
        self.assertIn("cp -a \"$appdata_source\" \"/out/files/share/appdata/$app_id.appdata.xml\"", containerfile)
        self.assertIn("appstreamcli compose --verbose --prefix /out/files --origin flatpak --components \"$app_id\"", containerfile)
        self.assertIn("bundle = ET.SubElement(component, 'bundle', {'type': 'flatpak'})", containerfile)
        self.assertIn("bundle.text = app_ref", containerfile)
        self.assertIn("find /out/files/share/mime/packages -maxdepth 1 -type f -name \"$app_id*.xml\"", containerfile)
        self.assertIn("for dir in dbus-1 gnome-shell krunner; do", containerfile)
        self.assertNotIn("FROM scratch", containerfile)
        self.assertEqual(labels["org.flatpak.ref"], "app/org.kde.kate/x86_64/f44")
        self.assertEqual(labels["org.flatpak.metadata"], "metadata-body\n")
        self.assertNotIn("org.flatpak.commit-metadata.xa.metadata", labels)
        self.assertNotIn("org.flatpak.commit-metadata.xa.ref", labels)
        self.assertNotIn("org.flatpak.commit-metadata.ostree.ref-binding", labels)
        self.assertEqual(labels["org.flatpak.subject"], "Export org.kde.kate")
        self.assertIn("Name: org.kde.kate", labels["org.flatpak.body"])
        self.assertIn("org.flatpak.timestamp", labels)

    def test_commit_metadata_labels_match_flatpak_oci_shape(self) -> None:
        labels = _flatpak_commit_metadata_labels(
            "[Application]\nname=org.kde.kate\n",
            "app/org.kde.kate/x86_64/f44",
            download_size=123,
            installed_size=456,
        )

        self.assertEqual(
            labels["org.flatpak.commit-metadata.xa.metadata"],
            "W0FwcGxpY2F0aW9uXQpuYW1lPW9yZy5rZGUua2F0ZQoAAHM=",
        )
        self.assertEqual(
            labels["org.flatpak.commit-metadata.xa.ref"],
            "YXBwL29yZy5rZGUua2F0ZS94ODZfNjQvZjQ0AABz",
        )
        self.assertEqual(
            labels["org.flatpak.commit-metadata.ostree.ref-binding"],
            "YXBwL29yZy5rZGUua2F0ZS94ODZfNjQvZjQ0ABwAYXM=",
        )
        self.assertEqual(
            labels["org.flatpak.commit-metadata.ostree.collection-binding"],
            "AABz",
        )
        self.assertEqual(
            labels["org.flatpak.commit-metadata.xa.download-size"],
            "AAAAAAAAAHsAdA==",
        )
        self.assertEqual(
            labels["org.flatpak.commit-metadata.xa.installed-size"],
            "AAAAAAAAAcgAdA==",
        )
        self.assertEqual(labels["org.flatpak.download-size"], "123")
        self.assertEqual(labels["org.flatpak.installed-size"], "456")

    def test_containerfile_rewrites_desktop_icon_for_rename_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flatpak_dir = root / "kate"
            flatpak_dir.mkdir()
            card_path = flatpak_dir / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.kde.kate
  command: kate
  rename-icon: org.kde.kate
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
""",
                encoding="utf-8",
            )
            card = FlatpakCard.from_file(card_path)
            build_dir = root / "build"

            _write_flatpak_containerfile(
                final_build_dir=build_dir,
                flatpak_dir=flatpak_dir,
                card=card,
                build_image="localhost/builds:f44-flatpak-kate",
                orchestrator="localhost/orchestrator:f44",
                metadata="metadata-body\n",
                app_ref="app/org.kde.kate/x86_64/f44",
                branch="f44",
                flatpak_arch="x86_64",
            )

            containerfile = (build_dir / "Containerfile").read_text(encoding="utf-8")

        self.assertIn("old_icon=org.kde.kate", containerfile)
        self.assertIn("new_icon=org.kde.kate", containerfile)
        self.assertIn("-name 'org.kde.kate.*'", containerfile)
        self.assertIn("s/^Icon=${old_icon}$/Icon=${new_icon}/", containerfile)

    def test_containerfile_renames_desktop_appdata_without_command_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flatpak_dir = root / "kate"
            flatpak_dir.mkdir()
            card_path = flatpak_dir / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.anatase.TextEditor
  command: kate
  rename: Text Editor (Kate)
  rename-author: "%s (packaged by Anatase)"
  rename-desktop-file: org.kde.kate.desktop
  rename-appdata-file: org.kde.kate.appdata.xml
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
""",
                encoding="utf-8",
            )
            card = FlatpakCard.from_file(card_path)
            build_dir = root / "build"

            _write_flatpak_containerfile(
                final_build_dir=build_dir,
                flatpak_dir=flatpak_dir,
                card=card,
                build_image="localhost/builds:f44-flatpak-kate",
                orchestrator="localhost/orchestrator:f44",
                metadata="metadata-body\n",
                app_ref="app/org.anatase.TextEditor/x86_64/f44",
                branch="f44",
                flatpak_arch="x86_64",
            )

            containerfile = (build_dir / "Containerfile").read_text(encoding="utf-8")

        self.assertIn("mv -f /out/files/share/applications/org.kde.kate.desktop /out/files/share/applications/org.anatase.TextEditor.desktop", containerfile)
        self.assertIn("mv -f /out/files/share/appdata/org.kde.kate.appdata.xml /out/files/share/appdata/org.anatase.TextEditor.appdata.xml", containerfile)
        self.assertIn("DISPLAY_NAME=\"$display_name\"", containerfile)
        self.assertIn("line = f'Name={display_name}", containerfile)
        self.assertNotIn("FLATPAK_COMMAND", containerfile)
        self.assertNotIn("original_command", containerfile)
        self.assertNotIn("kate-anatase", containerfile)
        self.assertNotIn("qwindowtitle", containerfile)
        self.assertIn("name.text = display_name", containerfile)
        self.assertIn("AUTHOR_TEMPLATE=\"$author_template\"", containerfile)
        self.assertIn("while author.startswith(prefix) and author.endswith(suffix):", containerfile)
        self.assertIn("author = author[:-len(suffix)]", containerfile)
        self.assertIn("rewritten = author_template.replace('%s', author).strip()", containerfile)
        self.assertIn("name.text = rewritten", containerfile)
        self.assertIn("developer_name.text = rewritten", containerfile)
        self.assertNotIn("component.remove(developer_name)", containerfile)
        self.assertEqual(containerfile.count("AUTHOR_TEMPLATE=\"$author_template\""), 1)
        self.assertEqual(
            containerfile.count("rewritten = author_template.replace('%s', author).strip()"),
            1,
        )
        self.assertIn("APP_ICON=\"$app_icon\"", containerfile)
        self.assertIn("icon.text = app_icon", containerfile)

    def test_containerfile_exports_app_id_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flatpak_dir = root / "kate"
            flatpak_dir.mkdir()
            card_path = flatpak_dir / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.kde.kate
  command: kate
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
""",
                encoding="utf-8",
            )
            card = FlatpakCard.from_file(card_path)
            build_dir = root / "build"

            _write_flatpak_containerfile(
                final_build_dir=build_dir,
                flatpak_dir=flatpak_dir,
                card=card,
                build_image="localhost/builds:f44-flatpak-kate",
                orchestrator="localhost/orchestrator:f44",
                metadata="metadata-body\n",
                app_ref="app/org.kde.kate/x86_64/f44",
                branch="f44",
                flatpak_arch="x86_64",
            )

            containerfile = (build_dir / "Containerfile").read_text(encoding="utf-8")

        self.assertIn("app_id=org.kde.kate", containerfile)
        self.assertIn("-name \"$app_id.appdata.xml\"", containerfile)
        self.assertIn("-name \"$app_id.metainfo.xml\"", containerfile)
        self.assertIn("find /out/files/share/icons -type f -name \"$app_id.*\"", containerfile)
        self.assertIn("find /out/files/share/mime/packages -maxdepth 1 -type f -name \"$app_id*.xml\"", containerfile)
        self.assertNotIn("for dir in mime dbus-1", containerfile)

    def test_flatpak_image_build_labels_with_buildah_squash_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            build_dir = Path(temp)
            containerfile = build_dir / "Containerfile"
            containerfile.write_text(
                "FROM localhost/builds:f44-flatpak-kate AS build\n"
                "RUN mkdir -p /out/files\n",
                encoding="utf-8",
            )
            (build_dir / "labels.json").write_text(
                json.dumps(
                    [
                        ["org.flatpak.ref", "app/org.kde.kate/x86_64/f44"],
                        ["org.flatpak.metadata", "metadata-body\n"],
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            streamed = []
            runs = []

            def run_streamed(command, **_kwargs):
                streamed.append(command)
                if "--iidfile" in command:
                    iidfile = Path(command[command.index("--iidfile") + 1])
                    iidfile.write_text("sha256:buildimageid\n", encoding="utf-8")
                return 0, ""

            def run(command, **kwargs):
                runs.append((command, kwargs))
                if command[:3] == ["buildah", "from", "--quiet"]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="flatpak-label-container\n",
                    )
                return SimpleNamespace(returncode=0, stdout="")

            with (
                patch("ludos.flatpaks._run_streamed_command", side_effect=run_streamed),
                patch("ludos.flatpaks.subprocess.run", side_effect=run),
                patch("ludos.flatpaks._podman_cp", return_value=False) as podman_cp,
                patch(
                    "ludos.flatpaks._flatpak_appstream_labels",
                    return_value={
                        "org.freedesktop.appstream.appdata": "<component/>",
                        "org.freedesktop.appstream.icon-64": "data:image/png;base64,AA==",
                    },
                ) as appstream_labels,
                patch(
                    "ludos.flatpaks._flatpak_payload_size",
                    return_value=123,
                ) as payload_size,
            ):
                _run_flatpak_image_build(
                    "podman",
                    "buildah",
                    build_dir,
                    "localhost/flatpaks:f44-x86_64-kate",
                    "metadata-body\n",
                    "org.kde.kate",
                )
            final_containerfile = (build_dir / "Containerfile.final").read_text(
                encoding="utf-8"
            )

        self.assertEqual(
            streamed[0],
            [
                "podman",
                "build",
                "--pull=false",
                "--tag",
                "localhost/flatpaks:f44-x86_64-kate-build-stage",
                "--iidfile",
                str(build_dir / "build-image.id"),
                "--file",
                str(containerfile),
                "--target",
                "build",
                str(build_dir),
            ],
        )
        self.assertEqual(
            streamed[1],
            [
                "podman",
                "build",
                "--pull=false",
                "--tag",
                "localhost/flatpaks:f44-x86_64-kate-unlabeled",
                "--file",
                str(build_dir / "Containerfile.final"),
                str(build_dir),
            ],
        )
        self.assertEqual(
            runs[0][0],
            [
                "buildah",
                "from",
                "--quiet",
                "localhost/flatpaks:f44-x86_64-kate-unlabeled",
            ],
        )
        self.assertIn(
            (
                [
                    "buildah",
                    "config",
                    "--label",
                    "org.flatpak.metadata=metadata-body\n",
                    "flatpak-label-container",
                ],
                {
                    "check": False,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.PIPE,
                    "text": True,
                },
            ),
            runs,
        )
        self.assertIn(
            (
                [
                    "buildah",
                    "config",
                    "--label",
                    "org.flatpak.download-size=123",
                    "flatpak-label-container",
                ],
                {
                    "check": False,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.PIPE,
                    "text": True,
                },
            ),
            runs,
        )
        self.assertIn(
            (
                [
                    "buildah",
                    "config",
                    "--label",
                    "org.flatpak.commit-metadata.xa.metadata=bWV0YWRhdGEtYm9keQoAAHM=",
                    "flatpak-label-container",
                ],
                {
                    "check": False,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.PIPE,
                    "text": True,
                },
            ),
            runs,
        )
        self.assertIn(
            (
                [
                    "buildah",
                    "config",
                    "--label",
                    "org.flatpak.commit-metadata.xa.download-size=AAAAAAAAAHsAdA==",
                    "flatpak-label-container",
                ],
                {
                    "check": False,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.PIPE,
                    "text": True,
                },
            ),
            runs,
        )
        self.assertIn(
            (
                [
                    "buildah",
                    "commit",
                    "--squash",
                    "--format",
                    "oci",
                    "flatpak-label-container",
                    "localhost/flatpaks:f44-x86_64-kate",
                ],
                {
                    "check": False,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.PIPE,
                    "text": True,
                },
            ),
            runs,
        )
        self.assertEqual(
            runs[-2][0],
            ["buildah", "rm", "flatpak-label-container"],
        )
        self.assertEqual(
            runs[-1][0],
            [
                "podman",
                "rmi",
                "localhost/flatpaks:f44-x86_64-kate-build-stage",
                "sha256:buildimageid",
                "localhost/flatpaks:f44-x86_64-kate-unlabeled",
            ],
        )
        self.assertTrue(all("--label" not in command for command in streamed))
        appstream_labels.assert_called_once_with(
            "podman",
            build_dir,
            "sha256:buildimageid",
            "org.kde.kate",
            files_root="/out/files",
        )
        payload_size.assert_called_once_with("podman", "sha256:buildimageid", "/out")
        podman_cp.assert_not_called()
        self.assertIn("FROM scratch", final_containerfile)
        self.assertIn("COPY --from=sha256:buildimageid /out/ /", final_containerfile)
        self.assertNotIn("LABEL ", final_containerfile)

    def test_stage_flatpak_files_copies_local_files_after_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            flatpak_dir = root / "kate"
            flatpak_dir.mkdir()
            (flatpak_dir / "payload.txt").write_text("payload", encoding="utf-8")
            card_path = flatpak_dir / "card.yaml"
            card_path.write_text(
                """
version: 1
flatpak:
  id: org.kde.kate
  command: kate
build-deps:
  - rpm-build
specs:
  - spec: kate.spec
    packages: [kate]
files:
  - app/share/payload.txt::payload.txt
""",
                encoding="utf-8",
            )
            card = FlatpakCard.from_file(card_path)
            files_dir = root / "build" / "files"

            count = _stage_flatpak_files(card, flatpak_dir, files_dir)

            self.assertEqual(count, 1)
            self.assertEqual(
                (files_dir / "app/share/payload.txt").read_text(encoding="utf-8"),
                "payload",
            )

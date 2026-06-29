from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ludos.__main__ import build_command, build_parser
from ludos.flatpaks import (
    FlatpakCard,
    build_flatpaks,
    _flatpak_build_env,
    _flatpak_commit_metadata_labels,
    _flatpak_metadata,
    _flatpak_rpmbuild_defines,
    _stage_flatpak_files,
    _substitute_specs,
    _write_flatpak_containerfile,
)
from ludos.model import ConfigError, Manifest, SpecBuild, validate_manifest


class FlatpakParserTests(unittest.TestCase):
    def test_build_parser_accepts_flatpak_target(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["build", "--flatpak", "flatpaks/kate", "anatase.yml"]
        )

        self.assertEqual(args.flatpak, Path("flatpaks/kate"))
        self.assertIsNone(args.card)
        self.assertFalse(args.flatpaks)

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
            ref="app/org.kde.kate/x86_64/f44",
            image="localhost/org.kde.kate:f44",
            latest_image="localhost/org.kde.kate:latest",
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
            "Built flatpak app/org.kde.kate/x86_64/f44",
            log.call_args.args[0],
        )

    def test_manifest_parser_accepts_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest_path = self._write_manifest(
                Path(temp),
                flatpaks=("flatpaks/kate",),
            )

            manifest = Manifest.from_file(manifest_path)

        self.assertEqual(manifest.flatpaks, ("flatpaks/kate",))

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
                    "releasever: '44'",
                    "distro: f$releasever-$arch",
                    "orchestrator: quay.io/fedora/fedora:$releasever",
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
        )

        self.assertIn("runtime=org.anatase.Platform/x86_64/f44", metadata)
        self.assertIn("sdk=org.anatase.Sdk/x86_64/f44", metadata)
        self.assertIn("command=kate", metadata)
        self.assertIn("devices=dri;", metadata)
        self.assertIn("filesystems=host;", metadata)
        self.assertIn("shared=ipc;", metadata)
        self.assertIn("sockets=wayland;", metadata)
        self.assertIn("[Session Bus Policy]", metadata)
        self.assertIn("org.kde.KGlobalSettings=talk", metadata)

    def test_metadata_uses_wrapper_command_for_renamed_app(self) -> None:
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
        )

        self.assertIn("name=org.anatase.TextEditor", metadata)
        self.assertIn("command=kate-anatase", metadata)

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
        self.assertIn("FROM scratch", containerfile)
        self.assertIn("LABEL org.flatpak.ref=", containerfile)
        self.assertIn("LABEL org.flatpak.commit-metadata.xa.metadata=", containerfile)
        self.assertIn("LABEL org.flatpak.commit-metadata.xa.ref=", containerfile)
        self.assertIn("LABEL org.flatpak.commit-metadata.ostree.ref-binding=", containerfile)
        self.assertIn("LABEL org.flatpak.subject=", containerfile)
        self.assertIn("LABEL org.flatpak.body=", containerfile)
        self.assertIn("LABEL org.flatpak.timestamp=", containerfile)

    def test_commit_metadata_labels_match_flatpak_oci_shape(self) -> None:
        labels = _flatpak_commit_metadata_labels(
            "[Application]\nname=org.kde.kate\n",
            "app/org.kde.kate/x86_64/f44",
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

    def test_containerfile_renames_desktop_appdata_and_wraps_qt_command(self) -> None:
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
        self.assertIn("flatpak_command + value[len(original_command):]", containerfile)
        self.assertIn("cat > /out/files/bin/kate-anatase", containerfile)
        self.assertIn("exec \"$command\" -qwindowtitle \"$title\" \"$@\"", containerfile)
        self.assertIn("name.text = display_name", containerfile)
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

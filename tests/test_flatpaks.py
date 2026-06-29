from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ludos.__main__ import build_parser
from ludos.flatpaks import (
    FlatpakCard,
    _flatpak_commit_metadata_labels,
    _flatpak_metadata,
    _flatpak_rpmbuild_defines,
    _stage_flatpak_files,
    _substitute_specs,
    _write_flatpak_containerfile,
)
from ludos.model import ConfigError, SpecBuild


class FlatpakParserTests(unittest.TestCase):
    def test_build_parser_accepts_flatpak_target(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["build", "--flatpak", "flatpaks/kate", "anatase.yml"]
        )

        self.assertEqual(args.flatpak, Path("flatpaks/kate"))
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

    def test_card_parser_accepts_metadata_hooks_and_specs(self) -> None:
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
  rename-icon: kate
build-deps:
  - rpm-build
  - flatpak-rpm-macros
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
        self.assertEqual(card.flatpak.rename_icon, "kate")
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


class FlatpakAssemblyTests(unittest.TestCase):
    def test_flatpak_rpmbuild_defines_do_not_force_libdir(self) -> None:
        defines = _flatpak_rpmbuild_defines()

        self.assertIn("_prefix /app", defines)
        self.assertNotIn("_libdir /app/lib64", defines)

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
        self.assertIn("for dir in mime dbus-1 gnome-shell krunner app-info; do", containerfile)
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
  rename-icon: kate
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

        self.assertIn("old_icon=kate", containerfile)
        self.assertIn("new_icon=org.kde.kate", containerfile)
        self.assertIn("s/^Icon=${old_icon}$/Icon=${new_icon}/", containerfile)

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

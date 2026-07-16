from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from ludos.__main__ import build_parser
from ludos.model import (
    ConfigError,
    FlatpakGpgConfig,
    FlatpakImagesConfig,
    OciCosignConfig,
    validate_manifest,
)
from ludos.upload.common import REGISTRY_IMMUTABLE_CACHE_CONTROL
from ludos.upload.flatpaks import (
    FlatpakPromotionPlan,
    export_flatpak_oci_images,
    finish_flatpak_promotions,
    plan_flatpak_promotions,
    tree_shake_flatpaks,
    update_flatpak_index,
    upload_dummy_runtime,
    upload_flatpaks,
    _flatpak_signature_payload,
    _overlay_icon,
)
from ludos.upload.registry import PromotedOciTag
from .test_upload_file import ENV, FakeS3Client


ROOT = Path(__file__).resolve().parents[2]


class UploadFlatpaksTests(unittest.TestCase):
    def test_icon_overlay_requires_flatpaks_extra(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            overlay = Path(temp) / "overlay.png"
            overlay.touch()
            with patch.dict("sys.modules", {"PIL": None}):
                with self.assertRaisesRegex(
                    ConfigError,
                    r"install ludos\[flatpaks\]",
                ):
                    _overlay_icon(b"", overlay)

    def test_plan_flatpak_promotions_uses_only_manifest_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(
                root,
                ("flatpaks/kate", "flatpaks/ark"),
            )

            plans = plan_flatpak_promotions(manifest, prefix="rolling-")

        self.assertEqual(
            [(plan.ref, plan.source_tag, plan.target_tag) for plan in plans],
            [
                ("flatpaks/kate", "rolling-f44-x86_64", "f44-x86_64"),
                ("flatpaks/ark", "rolling-f44-x86_64", "f44-x86_64"),
            ],
        )

    def test_plan_flatpak_promotions_overrides_host_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))

            plans = plan_flatpak_promotions(
                manifest,
                prefix="rolling-",
                arch="aarch64",
            )

        self.assertEqual(
            [(plan.ref, plan.source_tag, plan.target_tag) for plan in plans],
            [("flatpaks/kate", "rolling-f44-aarch64", "f44-aarch64")],
        )

    def test_finish_flatpak_promotions_signs_target_before_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = SimpleNamespace(
                root_dir=root,
                flatpak_gpg=FlatpakGpgConfig(
                    identity="https://flatpaks.example.test/",
                    lookaside="gpg",
                    verify="",
                ),
            )
            plan = FlatpakPromotionPlan(
                context=context,
                ref="flatpaks/kate",
                source_tag="rolling-f44-x86_64",
                target_tag="f44-x86_64",
            )
            digest = "sha256:" + "a" * 64
            result = PromotedOciTag(
                plan.ref,
                plan.source_tag,
                plan.target_tag,
                digest,
            )
            events = []

            def sign(payload: bytes, _config: object) -> bytes:
                data = json.loads(payload)
                events.append(("sign", data["critical"]["identity"]["docker-reference"]))
                return b"signed"

            with (
                patch(
                    "ludos.upload.flatpaks._flatpak_gpg_signing_config",
                    return_value=SimpleNamespace(),
                ),
                patch(
                    "ludos.upload.flatpaks.sign_attached_data",
                    side_effect=sign,
                ),
                patch(
                    "ludos.upload.flatpaks._upload_flatpak_signature",
                    side_effect=lambda *_args, **_kwargs: events.append(("upload",)),
                ) as upload_signature,
                patch(
                    "ludos.upload.flatpaks.update_flatpak_static_index",
                    side_effect=lambda tag, **_kwargs: events.append(("refresh", tag)) or 0,
                ) as refresh,
            ):
                self.assertEqual(
                    finish_flatpak_promotions(
                        (plan,),
                        (result,),
                        refresh=True,
                    ),
                    0,
                )

        self.assertEqual(
            events,
            [
                ("sign", "flatpaks.example.test/flatpaks/kate:f44-x86_64"),
                ("upload",),
                ("refresh", "f44-x86_64"),
            ],
        )
        upload_signature.assert_called_once_with(
            context,
            "flatpaks/kate",
            digest,
            b"signed",
            environ=None,
            client=None,
        )
        refresh.assert_called_once_with(
            "f44-x86_64",
            environ=None,
            client=None,
        )

    def test_update_flatpak_index_prefixes_distro(self) -> None:
        context = SimpleNamespace(distro="f44-x86_64")
        with (
            patch(
                "ludos.upload.flatpaks._resolve_flatpak_upload_context",
                return_value=context,
            ),
            patch(
                "ludos.upload.flatpaks.update_flatpak_static_index",
                return_value=0,
            ) as update,
        ):
            self.assertEqual(
                update_flatpak_index(Path("anatase.yml"), prefix="rolling-"),
                0,
            )

        update.assert_called_once_with("rolling-f44-x86_64")

    def test_registry_flatpak_upload_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "upload",
                "anatase.yml",
                "--flatpak",
                "flatpaks/ark",
                "--flatpak",
                "flatpaks/kate",
                "--build",
                "--cache-dir",
                "out/cache",
                "--cache",
                "--refresh",
            ]
        )

        self.assertEqual(args.registry_action, "flatpak")
        self.assertEqual(args.registry_flatpak_action, "upload")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertEqual(args.flatpaks, [Path("flatpaks/ark"), Path("flatpaks/kate")])
        self.assertTrue(args.build)
        self.assertEqual(args.cache_dir, Path("out/cache"))
        self.assertTrue(args.cache)
        self.assertTrue(args.refresh)

    def test_atomic_flatpak_remote_enforces_gpg_and_lookaside(self) -> None:
        repo = (ROOT / "cards/base/atomic/flatpak/anatase.flatpakrepo").read_text(
            encoding="utf-8"
        )
        service = (
            ROOT / "cards/base/atomic/flatpak/anatase-flatpaks.service"
        ).read_text(encoding="utf-8")
        helper = (
            ROOT / "cards/base/atomic/flatpak/anatase-flatpak-remote"
        ).read_text(encoding="utf-8")

        self.assertIn("#GPGKey=@gpg@", repo)
        self.assertIn("#xa.signature-lookaside=https://flatpaks.anatase.org/gpg", repo)
        self.assertIn(
            "ExecStart=/usr/libexec/anatase-flatpak-remote %distro%",
            service,
        )
        self.assertIn("--gpg-verify", helper)
        self.assertIn(
            "--gpg-import=/usr/share/anatase/keys/anatase-gpg.pub.asc",
            helper,
        )
        self.assertIn("--signature-lookaside=https://flatpaks.anatase.org/gpg", helper)

    def test_registry_flatpak_upload_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "upload",
                "anatase.yml",
                "--flatpak",
                "flatpaks/ark",
                "--build",
                "--cache-dir",
                "out/cache",
            ]
        )

        with (
            patch("ludos.__main__.upload_flatpaks", return_value=0) as upload,
            patch("ludos.__main__.update_flatpak_index", return_value=0) as update,
        ):
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("anatase.yml"),
            (Path("flatpaks/ark"),),
            build=True,
            cache_dir=Path("out/cache"),
            cache_only=False,
        )
        update.assert_not_called()

    def test_registry_flatpak_upload_refresh_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "upload",
                "anatase.yml",
                "--flatpak",
                "flatpaks/ark",
                "--refresh",
            ]
        )

        with (
            patch("ludos.__main__.upload_flatpaks", return_value=0) as upload,
            patch("ludos.__main__.update_flatpak_index", return_value=0) as update,
        ):
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("anatase.yml"),
            (Path("flatpaks/ark"),),
            build=False,
            cache_dir=None,
            cache_only=False,
        )
        update.assert_called_once_with(Path("anatase.yml"))

    def test_registry_flatpak_upload_refresh_skips_after_failed_upload(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "upload", "anatase.yml", "--refresh"]
        )

        with (
            patch("ludos.__main__.upload_flatpaks", return_value=17) as upload,
            patch("ludos.__main__.update_flatpak_index", return_value=0) as update,
        ):
            self.assertEqual(args.func(args), 17)

        upload.assert_called_once_with(
            Path("anatase.yml"),
            tuple(),
            build=False,
            cache_dir=None,
            cache_only=False,
        )
        update.assert_not_called()

    def test_registry_flatpak_upload_command_passes_cache_only(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "upload", "anatase.yml", "--cache"]
        )

        with (
            patch("ludos.__main__.upload_flatpaks", return_value=0) as upload,
            patch("ludos.__main__.update_flatpak_index", return_value=0),
        ):
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(
            Path("anatase.yml"),
            tuple(),
            build=False,
            cache_dir=None,
            cache_only=True,
        )

    def test_registry_flatpak_init_dummy_runtime_parser(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "init-dummy-runtime",
                "anatase.yml",
                "--prefix",
                "rolling-",
            ]
        )

        self.assertEqual(args.registry_action, "flatpak")
        self.assertEqual(args.registry_flatpak_action, "init-dummy-runtime")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertEqual(args.prefix, "rolling-")

    def test_registry_flatpak_init_dummy_runtime_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "init-dummy-runtime",
                "anatase.yml",
                "--prefix",
                "rolling-",
            ]
        )

        with patch("ludos.__main__.upload_dummy_runtime", return_value=0) as upload:
            self.assertEqual(args.func(args), 0)

        upload.assert_called_once_with(Path("anatase.yml"), prefix="rolling-")

    def test_registry_flatpak_rejects_old_update_spellings(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["registry", "flatpak", "upload", "anatase.yml", "--update"]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(["registry", "flatpak", "update", "anatase.yml"])
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["registry", "flatpak", "upload-dummy-runtime", "anatase.yml"]
            )

    def test_registry_flatpak_tree_shake_parser(self) -> None:
        args = build_parser().parse_args(
            ["registry", "flatpak", "tree-shake", "anatase.yml"]
        )

        self.assertEqual(args.registry_action, "flatpak")
        self.assertEqual(args.registry_flatpak_action, "tree-shake")
        self.assertEqual(args.manifest, Path("anatase.yml"))
        self.assertIsNone(args.flatpaks)
        self.assertFalse(args.dry_run)

    def test_registry_flatpak_tree_shake_parser_accepts_selected_flatpaks(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "tree-shake",
                "custom.yml",
                "--flatpak",
                "flatpaks/ark",
                "--flatpak",
                "flatpaks/kate/card.yaml",
                "--dry-run",
            ]
        )

        self.assertEqual(args.manifest, Path("custom.yml"))
        self.assertEqual(
            args.flatpaks,
            [Path("flatpaks/ark"), Path("flatpaks/kate/card.yaml")],
        )
        self.assertTrue(args.dry_run)

    def test_registry_flatpak_tree_shake_command_dispatches(self) -> None:
        args = build_parser().parse_args(
            [
                "registry",
                "flatpak",
                "tree-shake",
                "anatase.yml",
                "--flatpak",
                "flatpaks/ark",
                "--dry-run",
            ]
        )

        with patch("ludos.__main__.tree_shake_flatpaks", return_value=0) as shake:
            self.assertEqual(args.func(args), 0)

        shake.assert_called_once_with(
            Path("anatase.yml"),
            (Path("flatpaks/ark"),),
            dry_run=True,
        )

    def test_upload_flatpaks_exports_all_manifest_flatpaks_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))

            with _mock_upload_deps() as deps:
                self.assertEqual(upload_flatpaks(manifest, tuple(), False), 0)

            self.assertEqual(
                deps.run_streamed.call_args_list,
                [
                    _podman_push_call(
                        root / "cache" / "flatpaks" / "kate-f44-x86_64",
                        "localhost/flatpaks:f44-x86_64-kate-hash",
                    ),
                    _podman_push_call(
                        root / "cache" / "flatpaks" / "ark-f44-x86_64",
                        "localhost/flatpaks:f44-x86_64-ark-hash",
                    ),
                ],
            )
            self.assertEqual(
                deps.upload_oci.call_args_list,
                [
                    call(
                        root / "cache" / "flatpaks" / "kate-f44-x86_64",
                        "flatpaks/kate",
                        ("f44-x86_64",),
                        environ=None,
                        client=None,
                        project_root=root,
                        cosign_config=OciCosignConfig(),
                    ),
                    call(
                        root / "cache" / "flatpaks" / "ark-f44-x86_64",
                        "flatpaks/ark",
                        ("f44-x86_64",),
                        environ=None,
                        client=None,
                        project_root=root,
                        cosign_config=OciCosignConfig(),
                    ),
                ],
            )
            deps.resolve_flatpaks.assert_called_once_with(
                manifest,
                cache_dir=root / "cache",
                cache_only=False,
            )

    def test_upload_flatpaks_can_require_cached_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))

            with _mock_upload_deps() as deps:
                self.assertEqual(
                    upload_flatpaks(
                        manifest,
                        tuple(),
                        False,
                        cache_only=True,
                    ),
                    0,
                )

            deps.resolve_flatpaks.assert_called_once_with(
                manifest,
                cache_dir=root / "cache",
                cache_only=True,
            )

    def test_upload_flatpaks_ignores_project_oci_cosign(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))
            _write_project_cosign(root)

            with _mock_upload_deps() as deps:
                self.assertEqual(upload_flatpaks(manifest, tuple(), False), 0)

            deps.upload_oci.assert_called_once_with(
                root / "cache" / "flatpaks" / "kate-f44-x86_64",
                "flatpaks/kate",
                ("f44-x86_64",),
                environ=None,
                client=None,
                project_root=root,
                cosign_config=OciCosignConfig(),
            )

    def test_export_flatpak_oci_images_returns_refs_and_image_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))

            with _mock_upload_deps() as deps:
                exported = export_flatpak_oci_images(manifest, tuple())

        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0].source_ref, "flatpaks/kate")
        self.assertEqual(exported[0].name, "kate")
        self.assertEqual(exported[0].image_id, "sha256:" + "a" * 64)
        self.assertEqual(
            exported[0].flatpak_ref,
            "app/org.anatase.Kate/x86_64/stable",
        )
        self.assertEqual(
            exported[0].export_dir,
            root / "cache" / "flatpaks" / "kate-f44-x86_64",
        )
        deps.upload_oci.assert_not_called()

    def test_upload_flatpaks_uses_selected_flatpaks_and_cache_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))
            cache_dir = root / "out" / "cache"

            with _mock_upload_deps() as deps:
                self.assertEqual(
                    upload_flatpaks(
                        manifest,
                        (Path("flatpaks/ark"),),
                        False,
                        cache_dir=cache_dir,
                    ),
                    0,
                )

            export_dir = cache_dir / "flatpaks" / "ark-f44-x86_64"
            deps.run_streamed.assert_called_once_with(
                [
                    "/usr/bin/podman",
                    "push",
                    "--format",
                    "oci",
                    "--compression-format",
                    "gzip",
                    "--force-compression",
                    "localhost/flatpaks:f44-x86_64-ark-hash",
                    f"oci:{export_dir}:f44-x86_64",
                ]
            )
            deps.upload_oci.assert_called_once_with(
                export_dir,
                "flatpaks/ark",
                ("f44-x86_64",),
                environ=None,
                client=None,
                project_root=root,
                cosign_config=OciCosignConfig(),
            )

    def test_upload_flatpaks_uses_prepared_image_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))

            with _mock_upload_deps() as deps:
                self.assertEqual(
                    upload_flatpaks(
                        manifest,
                        (Path("flatpaks/kate"),),
                        False,
                        image_overrides={
                            "flatpaks/kate": "flatpaks:f44-kate-prepared"
                        },
                        prefix="rolling-",
                    ),
                    0,
                )

            deps.resolve_flatpaks.assert_not_called()
            self.assertEqual(
                deps.run_streamed.call_args.args[0][-2],
                "flatpaks:f44-kate-prepared",
            )
            self.assertEqual(
                deps.run_streamed.call_args.args[0][-1],
                "oci:"
                f"{root / 'cache' / 'flatpaks' / 'kate-rolling-f44-x86_64'}:"
                "rolling-f44-x86_64",
            )
            self.assertEqual(
                deps.upload_oci.call_args.args[2],
                ("rolling-f44-x86_64",),
            )

    def test_upload_flatpaks_rejects_missing_local_image_without_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))

            with _mock_upload_deps(image_exists=False) as deps:
                with self.assertRaisesRegex(ConfigError, "flatpak image is not cached"):
                    upload_flatpaks(manifest, tuple(), False)

            deps.run_streamed.assert_not_called()
            deps.upload_oci.assert_not_called()

    def test_upload_flatpaks_builds_manifest_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))
            cache_dir = root / "cache"
            resolved = _resolved_context(manifest, root, cache_dir)
            results = (
                SimpleNamespace(image="localhost/flatpaks:built-kate"),
                SimpleNamespace(image="localhost/flatpaks:built-ark"),
            )

            with _mock_upload_deps() as deps:
                with (
                    patch(
                        "ludos.upload.flatpaks.resolve_manifest_context",
                        return_value=resolved,
                    ) as resolve,
                    patch(
                        "ludos.upload.flatpaks.build_flatpaks_with_context",
                        return_value=results,
                    ) as build,
                    patch("ludos.upload.flatpaks.build_flatpaks") as build_public,
                    patch("ludos.upload.flatpaks._remove_tree") as remove_tree,
                ):
                    self.assertEqual(
                        upload_flatpaks(manifest, tuple(), True, cache_dir=cache_dir),
                        0,
                    )

            resolve.assert_called_once_with(
                manifest,
                cache_dir=cache_dir,
                cache_only=False,
            )
            build.assert_called_once_with(
                resolved,
                manifest_path=manifest,
                cache_only=False,
            )
            build_public.assert_not_called()
            remove_tree.assert_called_once_with(
                resolved.dnf_workspace_dir,
                podman=resolved.podman,
            )
            self.assertEqual(
                [item.args[0][7] for item in deps.run_streamed.call_args_list],
                ["localhost/flatpaks:built-kate", "localhost/flatpaks:built-ark"],
            )

    def test_upload_flatpaks_builds_manifest_flatpaks_from_resolved_context_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))
            cache_dir = root / "cache"
            resolved = _resolved_context(manifest, root, cache_dir)
            results = (
                SimpleNamespace(image="localhost/flatpaks:built-kate"),
                SimpleNamespace(image="localhost/flatpaks:built-ark"),
            )

            with _mock_upload_deps():
                with (
                    patch(
                        "ludos.upload.flatpaks.resolve_manifest_context",
                        return_value=resolved,
                    ) as resolve,
                    patch(
                        "ludos.upload.flatpaks.build_flatpaks_with_context",
                        return_value=results,
                    ),
                    patch("ludos.upload.flatpaks._resolve_flatpak_upload_context") as upload_resolve,
                    patch("ludos.upload.flatpaks._remove_tree"),
                ):
                    self.assertEqual(
                        upload_flatpaks(manifest, tuple(), True, cache_dir=cache_dir),
                        0,
                    )

            resolve.assert_called_once()
            upload_resolve.assert_not_called()

    def test_upload_flatpaks_builds_selected_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))
            cache_dir = root / "cache"
            resolved = _resolved_context(manifest, root, cache_dir)

            def build_one(_context: object, flatpak: Path, **_kwargs: object) -> object:
                return SimpleNamespace(
                    image=f"localhost/flatpaks:built-{flatpak.parent.name}"
                )

            with _mock_upload_deps() as deps:
                with (
                    patch(
                        "ludos.upload.flatpaks.resolve_manifest_context",
                        return_value=resolved,
                    ),
                    patch(
                        "ludos.upload.flatpaks._build_flatpak_with_context",
                        side_effect=build_one,
                    ) as build,
                    patch("ludos.upload.flatpaks.build_flatpak") as build_public,
                    patch("ludos.upload.flatpaks._remove_tree"),
                ):
                    self.assertEqual(
                        upload_flatpaks(
                            manifest,
                            (Path("flatpaks/ark/card.yaml"),),
                            True,
                            cache_dir=cache_dir,
                        ),
                        0,
                    )

            build.assert_called_once_with(
                resolved,
                (root / "flatpaks" / "ark" / "card.yaml").resolve(),
                cache_only=False,
                force=False,
            )
            build_public.assert_not_called()
            self.assertEqual(
                deps.run_streamed.call_args.args[0][7],
                "localhost/flatpaks:built-ark",
            )

    def test_upload_flatpaks_uploads_configured_remote_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))
            (root / "ludos.yml").write_text(
                "\n".join(
                    [
                        "version: 1",
                        "name: Test",
                        "flatpaks:",
                        "  images:",
                        "    uri: https://flatpaks.example.test/icons/",
                        "    s3: icons/",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            icon = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
            )

            def run_streamed(command: list[str]) -> tuple[int, str]:
                export = Path(command[-1].removeprefix("oci:").rsplit(":", 1)[0])
                _write_exported_flatpak(
                    export,
                    labels={
                        "org.flatpak.ref": "app/org.anatase.Kate/x86_64/stable",
                        "org.freedesktop.appstream.appdata": (
                            "<components><component type=\"desktop-application\">"
                            "<id>org.anatase.Kate</id>"
                            "</component></components>"
                        ),
                        "org.freedesktop.appstream.icon-128": (
                            "data:image/png;base64,"
                            + base64.b64encode(icon).decode()
                        ),
                    },
                )
                return 0, ""

            client = FakeS3Client()
            with (
                patch("ludos.upload.flatpaks.shutil.which", return_value="/usr/bin/podman"),
                patch("ludos.upload.flatpaks._image_exists", return_value=True),
                patch(
                    "ludos.upload.flatpaks._run_streamed_command",
                    side_effect=run_streamed,
                ),
                patch(
                    "ludos.upload.flatpaks.resolve_manifest_flatpak_images",
                    side_effect=_mock_flatpak_resolution,
                ),
                patch(
                    "ludos.upload.flatpaks._podman_image_id",
                    return_value="sha256:" + "b" * 64,
                ),
                patch("ludos.upload.flatpaks.upload_oci", return_value=0) as upload_oci,
            ):
                self.assertEqual(
                    upload_flatpaks(
                        manifest,
                        tuple(),
                        False,
                        environ=ENV,
                        client=client,
                    ),
                    0,
                )

            export_dir = root / "cache" / "flatpaks" / "kate-f44-x86_64"
            upload_oci.assert_called_once_with(
                export_dir,
                "flatpaks/kate",
                ("f44-x86_64",),
                environ=ENV,
                client=client,
                project_root=root,
                cosign_config=OciCosignConfig(),
            )
            self.assertEqual(
                [(put["Key"], put["ContentType"], put["Body"]) for put in client.puts],
                [("icons/128x128/org.anatase.Kate.png", "image/png", icon)],
            )
            labels = _exported_flatpak_labels(export_dir)
            self.assertIn(
                "https://flatpaks.example.test/icons/128x128/org.anatase.Kate.png",
                labels["org.freedesktop.appstream.appdata"],
            )

    def test_flatpak_signature_payload_uses_atomic_container_signature(self) -> None:
        payload = _flatpak_signature_payload(
            FlatpakGpgConfig(
                identity="https://flatpaks.example.test/",
                lookaside="gpg",
                verify="./keys/test.pub.asc",
            ),
            repo="flatpaks/kate",
            tag="f44-x86_64",
            manifest_digest="sha256:" + "a" * 64,
        )

        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data["critical"]["type"], "atomic container signature")
        self.assertEqual(
            data["critical"]["image"]["docker-manifest-digest"],
            "sha256:" + "a" * 64,
        )
        self.assertEqual(
            data["critical"]["identity"]["docker-reference"],
            "flatpaks.example.test/flatpaks/kate:f44-x86_64",
        )
        self.assertEqual(data["optional"], {})

    def test_upload_flatpaks_signs_and_uploads_lookaside_before_oci(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))
            _write_project_gpg(root)
            events = []
            client = FakeS3Client()
            cert = root / "keys" / "test.pub.asc"

            def sign(data: bytes, _config: object) -> bytes:
                events.append("sign")
                payload = json.loads(data.decode("utf-8"))
                digest = payload["critical"]["image"]["docker-manifest-digest"]
                client.objects[
                    (
                        "anatase-artifacts",
                        "gpg/flatpaks/kate@sha256="
                        + digest.removeprefix("sha256:")
                        + "/signature-1",
                    )
                ] = b"existing"
                return b"signed-flatpak-payload"

            def verify(*_args: object, **_kwargs: object) -> None:
                events.append("verify")

            def upload_oci(*_args: object, **_kwargs: object) -> int:
                events.append("upload-oci")
                return 0

            with _mock_upload_deps() as deps:
                with (
                    patch(
                        "ludos.upload.flatpaks.config_from_env",
                        return_value=SimpleNamespace(),
                    ) as config_from_env,
                    patch("ludos.upload.flatpaks.sign_attached_data", side_effect=sign),
                    patch(
                        "ludos.upload.flatpaks.verify_attached_data",
                        side_effect=verify,
                    ) as verify_attached,
                ):
                    deps.upload_oci.side_effect = upload_oci
                    self.assertEqual(
                        upload_flatpaks(
                            manifest,
                            tuple(),
                            False,
                            environ={
                                **ENV,
                                "LUDOS_GPG_CERT": "./keys/signing.pub.asc:s2",
                                "LUDOS_GPG_KEY": "gcloud://example",
                            },
                            client=client,
                        ),
                        0,
                    )

            self.assertEqual(events, ["sign", "verify", "upload-oci"])
            config_from_env.assert_called_once()
            self.assertEqual(
                config_from_env.call_args.args[0]["LUDOS_GPG_CERT"],
                "./keys/signing.pub.asc:s2",
            )
            verify_attached.assert_called_once_with(
                b"signed-flatpak-payload",
                cert,
                project_root=root,
            )
            signature_puts = [
                put
                for put in client.puts
                if put["ContentType"] == "application/octet-stream"
            ]
            self.assertEqual(len(signature_puts), 1)
            self.assertEqual(signature_puts[0]["Body"], b"signed-flatpak-payload")
            self.assertTrue(
                str(signature_puts[0]["Key"]).startswith(
                    "gpg/flatpaks/kate@sha256="
                )
            )
            self.assertTrue(str(signature_puts[0]["Key"]).endswith("/signature-2"))
            self.assertEqual(
                signature_puts[0]["CacheControl"],
                REGISTRY_IMMUTABLE_CACHE_CONTROL,
            )

    def test_upload_flatpaks_gpg_missing_key_fails_before_oci_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))
            _write_project_gpg(root)

            with _mock_upload_deps() as deps:
                with patch(
                    "ludos.upload.flatpaks.config_from_env",
                    side_effect=ConfigError("LUDOS_GPG_KEY is required"),
                ):
                    with self.assertRaisesRegex(ConfigError, "LUDOS_GPG_KEY"):
                        upload_flatpaks(manifest, tuple(), False, environ=ENV)

            deps.upload_oci.assert_not_called()

    def test_upload_flatpaks_preserves_env_gpg_cert_selector(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))
            _write_project_gpg(root)
            cert = root / "keys" / "test.pub.asc"

            with _mock_upload_deps() as deps:
                with (
                    patch(
                        "ludos.upload.flatpaks.config_from_env",
                        return_value=SimpleNamespace(),
                    ) as config_from_env,
                    patch(
                        "ludos.upload.flatpaks.sign_attached_data",
                        return_value=b"signed-flatpak-payload",
                    ),
                    patch("ludos.upload.flatpaks.verify_attached_data"),
                ):
                    self.assertEqual(
                        upload_flatpaks(
                            manifest,
                            tuple(),
                            False,
                            environ={
                                **ENV,
                                "LUDOS_GPG_CERT": "./keys/signing.pub.asc:s2",
                                "LUDOS_GPG_KEY": "gcloud://example",
                            },
                            client=FakeS3Client(),
                        ),
                        0,
                    )

            self.assertEqual(
                config_from_env.call_args.args[0]["LUDOS_GPG_CERT"],
                "./keys/signing.pub.asc:s2",
            )
            deps.upload_oci.assert_called_once()

    def test_upload_flatpaks_without_verify_still_signs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))
            _write_project_gpg(root, verify="")

            with _mock_upload_deps() as deps:
                with (
                    patch(
                        "ludos.upload.flatpaks.config_from_env",
                        return_value=SimpleNamespace(),
                    ),
                    patch(
                        "ludos.upload.flatpaks.sign_attached_data",
                        return_value=b"signed-flatpak-payload",
                    ) as sign,
                    patch("ludos.upload.flatpaks.verify_attached_data") as verify,
                ):
                    self.assertEqual(
                        upload_flatpaks(
                            manifest,
                            tuple(),
                            False,
                            environ={
                                **ENV,
                                "LUDOS_GPG_CERT": "./keys/signing.pub.asc:s2",
                                "LUDOS_GPG_KEY": "gcloud://example",
                            },
                            client=FakeS3Client(),
                        ),
                        0,
                    )

            sign.assert_called_once()
            verify.assert_not_called()
            deps.upload_oci.assert_called_once()

    def test_tree_shake_flatpaks_uses_all_manifest_flatpaks_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))

            with patch("ludos.upload.flatpaks.tree_shake_oci", return_value=0) as shake:
                self.assertEqual(tree_shake_flatpaks(manifest, tuple()), 0)

        self.assertEqual(
            shake.call_args_list,
            [
                call(
                    "flatpaks/kate",
                    dry_run=False,
                    environ=None,
                    client=None,
                    project_root=root,
                    cosign_config=OciCosignConfig(),
                ),
                call(
                    "flatpaks/ark",
                    dry_run=False,
                    environ=None,
                    client=None,
                    project_root=root,
                    cosign_config=OciCosignConfig(),
                ),
            ],
        )

    def test_tree_shake_flatpaks_uses_selected_flatpaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate", "flatpaks/ark"))

            with patch("ludos.upload.flatpaks.tree_shake_oci", return_value=0) as shake:
                self.assertEqual(
                    tree_shake_flatpaks(
                        manifest,
                        (Path("flatpaks/ark/card.yaml"),),
                        dry_run=True,
                    ),
                    0,
                )

        shake.assert_called_once_with(
            "flatpaks/ark",
            dry_run=True,
            environ=None,
            client=None,
            project_root=root,
            cosign_config=OciCosignConfig(),
        )

    def test_tree_shake_flatpaks_prunes_unreferenced_signatures(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))
            _write_project_gpg(root)
            live_manifest = _manifest_body("b" * 64)
            live_digest = hashlib.sha256(live_manifest).hexdigest()
            dead_digest = "d" * 64
            client = FakeS3Client(
                {
                    (
                        "anatase-artifacts",
                        "v2/flatpaks/kate/manifests/f44-x86_64",
                    ): live_manifest,
                    (
                        "anatase-artifacts",
                        f"gpg/flatpaks/kate@sha256={live_digest}/signature-1",
                    ): b"live",
                    (
                        "anatase-artifacts",
                        f"gpg/flatpaks/kate@sha256={dead_digest}/signature-1",
                    ): b"dead",
                }
            )

            self.assertEqual(
                tree_shake_flatpaks(
                    manifest,
                    tuple(),
                    environ=ENV,
                    client=client,
                ),
                0,
            )

        self.assertEqual(
            [delete["Key"] for delete in client.deletes],
            [f"gpg/flatpaks/kate@sha256={dead_digest}/signature-1"],
        )

    def test_tree_shake_flatpaks_signature_pruning_honors_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, ("flatpaks/kate",))
            _write_project_gpg(root)
            dead_digest = "d" * 64
            client = FakeS3Client(
                {
                    (
                        "anatase-artifacts",
                        f"gpg/flatpaks/kate@sha256={dead_digest}/signature-1",
                    ): b"dead",
                }
            )

            self.assertEqual(
                tree_shake_flatpaks(
                    manifest,
                    tuple(),
                    dry_run=True,
                    environ=ENV,
                    client=client,
                ),
                0,
            )

        self.assertEqual(client.deletes, [])

    def test_upload_dummy_runtime_writes_runtime_oci_and_updates_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, tuple())
            events = []
            manifest_digests = []

            def upload(
                path: Path,
                ref: str,
                tags: tuple[str, ...],
                **_kwargs: object,
            ) -> int:
                events.append(("upload", ref, tags))
                index = json.loads((path / "index.json").read_text(encoding="utf-8"))
                manifest_desc = index["manifests"][0]
                manifest_digests.append(manifest_desc["digest"])
                manifest_blob = json.loads(
                    (
                        path
                        / "blobs"
                        / "sha256"
                        / manifest_desc["digest"].removeprefix("sha256:")
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    manifest_blob["layers"][0]["mediaType"],
                    "application/vnd.oci.image.layer.v1.tar",
                )
                config_desc = manifest_blob["config"]
                config = json.loads(
                    (
                        path
                        / "blobs"
                        / "sha256"
                        / config_desc["digest"].removeprefix("sha256:")
                    ).read_text(encoding="utf-8")
                )
                labels = config["config"]["Labels"]
                title = "Anatase Test Runtime"
                description = "Anatase Platform runtime for tests."
                license = "LicenseRef-Anatase-Test"
                author = "Anatase Test Authors"
                self.assertEqual(config["architecture"], "amd64")
                self.assertEqual(
                    labels["org.flatpak.ref"],
                    "runtime/org.anatase.Platform/x86_64/stable",
                )
                self.assertIn("[Runtime]", labels["org.flatpak.metadata"])
                self.assertIn(
                    "sdk=org.anatase.ludos.Sdk/x86_64/stable",
                    labels["org.flatpak.metadata"],
                )
                self.assertEqual(labels["org.flatpak.timestamp"], "0")
                self.assertEqual(
                    _uint64_variant_label(
                        labels["org.flatpak.commit-metadata.xa.download-size"]
                    ),
                    1,
                )
                self.assertEqual(labels["org.flatpak.download-size"], "1")
                self.assertEqual(
                    _uint64_variant_label(
                        labels["org.flatpak.commit-metadata.xa.installed-size"]
                    ),
                    1,
                )
                self.assertEqual(labels["org.flatpak.installed-size"], "1")
                self.assertEqual(labels["org.flatpak.subject"], title)
                self.assertEqual(labels["org.flatpak.body"], description)
                self.assertEqual(
                    labels["org.opencontainers.image.title"],
                    title,
                )
                self.assertEqual(
                    labels["org.opencontainers.image.description"],
                    description,
                )
                self.assertEqual(labels["org.opencontainers.image.licenses"], license)
                self.assertEqual(labels["org.opencontainers.image.authors"], author)
                self.assertEqual(labels["org.opencontainers.image.vendor"], author)
                appdata = labels["org.freedesktop.appstream.appdata"]
                self.assertIn('<component type="runtime">', appdata)
                self.assertIn("<id>org.anatase.Platform</id>", appdata)
                self.assertIn(
                    "<bundle type=\"flatpak\">"
                    "runtime/org.anatase.Platform/x86_64/stable"
                    "</bundle>",
                    appdata,
                )
                self.assertIn("<name>Anatase Test Runtime</name>", appdata)
                self.assertIn(
                    "<project_group>Anatase Test Authors</project_group>",
                    appdata,
                )
                self.assertIn(
                    "<developer> <name>Anatase Test Authors</name> </developer>",
                    appdata,
                )
                self.assertIn(
                    "<developer_name>Anatase Test Authors</developer_name>",
                    appdata,
                )
                self.assertIn(
                    '<icon type="remote" width="128" height="128">'
                    "https://flatpaks.example.test/icons/128x128/"
                    "org.anatase.Platform.png"
                    "</icon>",
                    appdata,
                )
                self.assertIn("<metadata_license>CC0-1.0</metadata_license>", appdata)
                self.assertIn(
                    "<project_license>LicenseRef-Anatase-Test</project_license>",
                    appdata,
                )
                return 0

            def update(distro: str) -> int:
                events.append(("update", distro))
                return 0

            with (
                patch("ludos.upload.flatpaks.upload_oci", side_effect=upload),
                patch(
                    "ludos.upload.flatpaks.update_flatpak_static_index",
                    side_effect=update,
                ),
            ):
                self.assertEqual(
                    upload_dummy_runtime(manifest, cache_dir=root / "cache"),
                    0,
                )
                self.assertEqual(
                    upload_dummy_runtime(
                        manifest,
                        cache_dir=root / "other-cache",
                        prefix="rolling-",
                    ),
                    0,
                )

        self.assertEqual(
            events,
            [
                ("upload", "flatpaks/runtime", ("f44-x86_64",)),
                ("update", "f44-x86_64"),
                ("upload", "flatpaks/runtime", ("rolling-f44-x86_64",)),
                ("update", "rolling-f44-x86_64"),
            ],
        )
        self.assertEqual(len(set(manifest_digests)), 1)

    def test_upload_dummy_runtime_omits_optional_display_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, tuple())
            manifest.write_text(
                manifest.read_text(encoding="utf-8")
                .replace("  title: Anatase Test Runtime\n", "")
                .replace("  description: Anatase Platform runtime for tests.\n", "")
                .replace("  license: LicenseRef-Anatase-Test\n", ""),
                encoding="utf-8",
            )

            def upload(
                path: Path,
                _ref: str,
                _tags: tuple[str, ...],
                **_kwargs: object,
            ) -> int:
                index = json.loads((path / "index.json").read_text(encoding="utf-8"))
                manifest_desc = index["manifests"][0]
                manifest_blob = json.loads(
                    (
                        path
                        / "blobs"
                        / "sha256"
                        / manifest_desc["digest"].removeprefix("sha256:")
                    ).read_text(encoding="utf-8")
                )
                config_desc = manifest_blob["config"]
                config = json.loads(
                    (
                        path
                        / "blobs"
                        / "sha256"
                        / config_desc["digest"].removeprefix("sha256:")
                    ).read_text(encoding="utf-8")
                )
                labels = config["config"]["Labels"]
                self.assertNotIn("org.flatpak.subject", labels)
                self.assertNotIn("org.flatpak.body", labels)
                self.assertNotIn("org.opencontainers.image.title", labels)
                self.assertNotIn("org.opencontainers.image.description", labels)
                self.assertNotIn("org.opencontainers.image.licenses", labels)
                self.assertNotIn("org.freedesktop.appstream.appdata", labels)
                return 0

            with (
                patch("ludos.upload.flatpaks.upload_oci", side_effect=upload),
                patch("ludos.upload.flatpaks.update_flatpak_static_index", return_value=0),
            ):
                self.assertEqual(
                    upload_dummy_runtime(manifest, cache_dir=root / "cache"),
                    0,
                )

    def test_upload_dummy_runtime_rejects_missing_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = _write_manifest(root, tuple())
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "runtime:\n"
                    "  id: org.anatase.Platform\n"
                    "  repo: runtime\n"
                    "  branch: stable\n"
                    "  title: Anatase Test Runtime\n"
                    "  author: Anatase Test Authors\n"
                    "  description: Anatase Platform runtime for tests.\n"
                    "  license: LicenseRef-Anatase-Test\n"
                    "  image: https://flatpaks.example.test/icons/128x128/org.anatase.Platform.png\n",
                    "",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "runtime"):
                upload_dummy_runtime(manifest, cache_dir=root / "cache")


def _write_manifest(root: Path, flatpaks: tuple[str, ...]) -> Path:
    (root / "cards").mkdir()
    (root / "cards/bootstrap.yml").write_text("version: 1\n", encoding="utf-8")
    (root / "cards/base.yml").write_text("version: 1\n", encoding="utf-8")
    for flatpak in flatpaks:
        card_path = root / flatpak / "card.yaml"
        card_path.parent.mkdir(parents=True, exist_ok=True)
        card_path.write_text("version: 1\n", encoding="utf-8")
    manifest = root / "anatase.yml"
    manifest.write_text(
        "\n".join(
            [
                "version: 1",
                "name: Anatase Test",
                "env:",
                "  arch: x86_64",
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
    return manifest


def _write_project_gpg(root: Path, *, verify: str = "./keys/test.pub.asc") -> None:
    cert = root / "keys" / "test.pub.asc"
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_text("fake public key\n", encoding="utf-8")
    lines = [
        "version: 1",
        "name: Test",
        "flatpaks:",
        "  gpg:",
        "    identity: https://flatpaks.example.test/",
        "    lookaside: gpg",
    ]
    if verify:
        lines.append(f"    verify: {verify}")
    lines.append("")
    (root / "ludos.yml").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def _write_project_cosign(root: Path) -> None:
    (root / "ludos.yml").write_text(
        "\n".join(
            [
                "version: 1",
                "name: Test",
                "oci:",
                "  cosign:",
                "    registry: https://i.example.test/",
                "    identity: cosign.example.test",
                "    verify: root.pem",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _manifest_body(config_digest: str) -> bytes:
    return (
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": f"sha256:{config_digest}",
                    "size": 2,
                },
                "layers": [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _resolved_context(manifest: Path, root: Path, cache_dir: Path) -> object:
    return SimpleNamespace(
        validation=validate_manifest(manifest),
        root_dir=root,
        distro="f44-x86_64",
        arch="x86_64",
        local_prefix="",
        cache_dir=cache_dir,
        podman="/usr/bin/podman",
        flatpak_images=FlatpakImagesConfig(),
        flatpak_gpg=FlatpakGpgConfig(),
        oci_cosign=OciCosignConfig(),
        dnf_workspace_dir=root / "dnf-workspace",
    )


def _write_exported_flatpak(export: Path, *, labels: dict[str, str]) -> None:
    blobs = export / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    (export / "oci-layout").write_text(
        json.dumps({"imageLayoutVersion": "1.0.0"}),
        encoding="utf-8",
    )
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Labels": labels},
        "rootfs": {"type": "layers", "diff_ids": []},
    }
    config_blob = _write_blob(blobs, config)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_blob["digest"],
            "size": config_blob["size"],
        },
        "layers": [],
    }
    manifest_blob = _write_blob(blobs, manifest)
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_blob["digest"],
                "size": manifest_blob["size"],
            }
        ],
    }
    (export / "index.json").write_text(json.dumps(index), encoding="utf-8")


def _write_blob(blobs: Path, data: object) -> dict[str, object]:
    body = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(body).hexdigest()
    (blobs / digest).write_bytes(body)
    return {"digest": f"sha256:{digest}", "size": len(body)}


def _exported_flatpak_labels(export: Path) -> dict[str, str]:
    index = json.loads((export / "index.json").read_text(encoding="utf-8"))
    manifest_digest = index["manifests"][0]["digest"].removeprefix("sha256:")
    manifest = json.loads(
        (export / "blobs" / "sha256" / manifest_digest).read_text(encoding="utf-8")
    )
    config_digest = manifest["config"]["digest"].removeprefix("sha256:")
    config = json.loads(
        (export / "blobs" / "sha256" / config_digest).read_text(encoding="utf-8")
    )
    return config["config"]["Labels"]


def _uint64_variant_label(value: str) -> int:
    return struct.unpack(">Q", base64.b64decode(value)[:8])[0]


class _MockUploadDeps:
    def __init__(
        self,
        run_streamed: object,
        upload_oci: object,
        resolve_flatpaks: object,
    ) -> None:
        self.run_streamed = run_streamed
        self.upload_oci = upload_oci
        self.resolve_flatpaks = resolve_flatpaks


class _mock_upload_deps:
    def __init__(self, *, image_exists: bool = True) -> None:
        self.image_exists = image_exists

    def __enter__(self) -> _MockUploadDeps:
        self.stack = ExitStack()
        self.stack.enter_context(
            patch("ludos.upload.flatpaks.shutil.which", return_value="/usr/bin/podman")
        )
        self.stack.enter_context(
            patch("ludos.upload.flatpaks._image_exists", return_value=self.image_exists)
        )
        resolve_flatpaks = self.stack.enter_context(
            patch(
                "ludos.upload.flatpaks.resolve_manifest_flatpak_images",
                side_effect=_mock_flatpak_resolution,
            )
        )
        self.stack.enter_context(
            patch("ludos.upload.flatpaks._podman_image_id", return_value="sha256:" + "a" * 64)
        )
        run_streamed = self.stack.enter_context(
            patch(
                "ludos.upload.flatpaks._run_streamed_command",
                side_effect=_mock_podman_push,
            )
        )
        upload_oci = self.stack.enter_context(
            patch("ludos.upload.flatpaks.upload_oci", return_value=0)
        )
        return _MockUploadDeps(run_streamed, upload_oci, resolve_flatpaks)

    def __exit__(self, *exc: object) -> None:
        self.stack.close()


def _mock_flatpak_resolution(manifest: Path, **_kwargs: object) -> object:
    names = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- flatpaks/"):
            names.append(Path(stripped.removeprefix("- ")).name)
    return SimpleNamespace(
        output_images=tuple(
            f"localhost/flatpaks:f44-x86_64-{name}-hash" for name in names
        )
    )


def _mock_podman_push(command: list[str]) -> tuple[int, str]:
    export = Path(command[-1].removeprefix("oci:").rsplit(":", 1)[0])
    name = export.name.split("-", 1)[0]
    _write_exported_flatpak(
        export,
        labels={"org.flatpak.ref": f"app/org.anatase.{name.title()}/x86_64/stable"},
    )
    return 0, ""


def _podman_push_call(export_dir: Path, image: str) -> object:
    return call(
        [
            "/usr/bin/podman",
            "push",
            "--format",
            "oci",
            "--compression-format",
            "gzip",
            "--force-compression",
            image,
            f"oci:{export_dir}:f44-x86_64",
        ]
    )


if __name__ == "__main__":
    unittest.main()

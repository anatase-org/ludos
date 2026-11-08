from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import call, patch

from ludos.__main__ import build_parser, cleanup_command
from ludos.build import resolve_build_manifest_context
from ludos.cleanup import cleanup_ci_images
from ludos.cleanup_ghcr import (
    GHCR_CLEANUP_REPOSITORIES,
    _gh_api,
    _package_versions,
    cleanup_ghcr_images,
    validate_ghcr_registry,
)
from ludos.common import _ensure_image
from ludos.model import CiConfig, ConfigError, Project


def digest(number: int) -> str:
    return f"sha256:{number:064x}"


def version(number: int, *tags: str) -> dict:
    return {
        "id": number,
        "name": digest(number),
        "metadata": {"container": {"tags": list(tags)}},
    }


class CleanupCiCommandTests(unittest.TestCase):
    def test_provider_requires_supported_string(self) -> None:
        parser = build_parser()
        with patch("sys.stderr"):
            for args in (["cleanup", "--ci"], ["cleanup", "--ci", "other"]):
                with self.assertRaises(SystemExit):
                    parser.parse_args(args)

    def test_project_defaults_and_explicit_manifests(self) -> None:
        for explicit in ([], ["explicit.yml"]):
            with self.subTest(explicit=explicit):
                args = build_parser().parse_args([
                    "cleanup", "--ci", "ghcr", "--arch", "x86_64",
                    "--arch", "aarch64", "--version", "20261102", "--dry-run",
                    *explicit,
                ])
                args.project = Project(
                    name="Test", root=Path("/project"),
                    ci=CiConfig(registry="ghcr.io/example"),
                    manifests=("anatase.yml", "base.yml"),
                )
                with patch("ludos.__main__.cleanup_ci_images", return_value=0) as clean:
                    self.assertEqual(cleanup_command(args), 0)
                clean.assert_called_once_with(
                    registry="ghcr.io/example", version="20261102", local_prefix="",
                    manifests=(Path("explicit.yml"),) if explicit else (
                        Path("/project/anatase.yml"), Path("/project/base.yml")),
                    arches=("x86_64", "aarch64"), dry_run=True, cache_only=False,
                )

    def test_rejects_purge_and_missing_project(self) -> None:
        for extra, message in ((["--purge"], "--purge"), ([], "ludos.yml")):
            args = build_parser().parse_args(["cleanup", "--ci", "ghcr", *extra])
            with self.assertRaisesRegex(ConfigError, message):
                cleanup_command(args)


class CleanupCiResolutionTests(unittest.TestCase):
    def test_resolves_union_before_deletion_without_final_images(self) -> None:
        events = []
        def context(manifest, **kwargs):
            arch = kwargs["arch"]
            events.append(arch)
            self.assertFalse(kwargs["cache_only"])
            self.assertEqual(kwargs["cache_version"], "20261102")
            workspace = Path("cache") / arch
            kwargs["dnf_workspace_dirs"].append(workspace)
            return NS(arch=arch, ci_registry="ghcr.io/example", local_prefix="")
        def metadata(ctx, **kwargs):
            return NS(
                orchestrator=f"orchestrator:{ctx.arch}-20261102",
                repo_images=(f"repos:{ctx.arch}-20261102",),
                package_images=(NS(image=f"cards:{ctx.arch}-card"),),
                build_images=(NS(image=f"builds:{ctx.arch}-build", builder_image=f"builders:{ctx.arch}-builder"),),
            )
        def flatpaks(ctx, **kwargs):
            return (NS(build_image=f"builds:{ctx.arch}-app", builder_image=f"builders:{ctx.arch}-app"),)
        def delete(*args, **kwargs):
            self.assertEqual(events, ["x86_64", "aarch64"])
            return 0
        with (
            patch("ludos.cleanup.shutil.which", side_effect=lambda name: name),
            patch("ludos.cleanup.resolve_build_manifest_context", side_effect=context) as resolve,
            patch("ludos.cleanup.resolve_build_manifest_from_context", side_effect=metadata),
            patch("ludos.cleanup.plan_manifest_flatpaks_with_context", side_effect=flatpaks),
            patch("ludos.cleanup._remove_tree") as remove_tree,
            patch("ludos.cleanup_ghcr.cleanup_ghcr_images", side_effect=delete) as clean,
        ):
            self.assertEqual(cleanup_ci_images(
                registry="ghcr.io/example/", version="20261102",
                manifests=(Path("anatase.yml"),), arches=("amd64", "arm64", "amd64"),
            ), 0)
        self.assertEqual(resolve.call_count, 2)
        refs = clean.call_args.args[1]
        self.assertEqual(len(refs), 14)
        self.assertEqual({ref.split(":")[0] for ref in refs}, set(GHCR_CLEANUP_REPOSITORIES))
        remove_tree.assert_has_calls([
            call(Path("cache/x86_64"), podman="podman"),
            call(Path("cache/aarch64"), podman="podman"),
        ])

    def test_resolution_failure_prevents_api_access(self) -> None:
        with (
            patch("ludos.cleanup.shutil.which", return_value="tool"),
            patch("ludos.cleanup.resolve_build_manifest_context", side_effect=ConfigError("resolution failed")),
            patch("ludos.cleanup_ghcr.cleanup_ghcr_images") as clean,
        ):
            with self.assertRaisesRegex(ConfigError, "resolution failed"):
                cleanup_ci_images(registry="ghcr.io/example", manifests=(Path("anatase.yml"),))
        clean.assert_not_called()

    def test_wrong_registry_or_prefix_prevents_deletion(self) -> None:
        for registry, prefix in (("ghcr.io/other", ""), ("ghcr.io/example", "test-")):
            with (
                patch("ludos.cleanup.shutil.which", return_value="tool"),
                patch("ludos.cleanup.resolve_build_manifest_context", return_value=NS(ci_registry=registry, local_prefix=prefix)),
                patch("ludos.cleanup_ghcr.cleanup_ghcr_images") as clean,
            ):
                with self.assertRaisesRegex(ConfigError, "differs"):
                    cleanup_ci_images(registry="ghcr.io/example", manifests=(Path("anatase.yml"),))
            clean.assert_not_called()

    def test_no_manifests_cannot_delete_all_caches(self) -> None:
        with self.assertRaisesRegex(ConfigError, "requires manifests"):
            cleanup_ci_images(registry="ghcr.io/example")

    def test_missing_local_mirror_is_pulled_and_tagged(self) -> None:
        with (
            patch("ludos.common._image_exists", return_value=False),
            patch("ludos.common._remote_cache_image_exists", return_value=True),
            patch("ludos.common._run_streamed_command", return_value=(0, "")) as run,
            patch("ludos.common.subprocess.run") as tag,
        ):
            self.assertTrue(_ensure_image("podman", "repos:current", "ghcr.io/example"))
        run.assert_called_once_with(["podman", "pull", "ghcr.io/example/repos:current"])
        tag.assert_called_once_with(["podman", "tag", "ghcr.io/example/repos:current", "repos:current"], check=True)

    def test_missing_orchestrator_and_repo_are_created_when_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ludos.yml").write_text("version: 1\nci:\n  registry: ghcr.io/example\n")
            (root / "bootstrap.yml").write_text("version: 1\n")
            (root / "repos").mkdir()
            (root / "repos/fedora.repo").write_text("[fedora]\nbaseurl=https://example.test\n")
            manifest = root / "anatase.yml"
            manifest.write_text(
                "version: 1\nreleasever: '45'\ndistro: f$releasever-$arch\n"
                "orchestrator: quay.io/fedora/fedora:$releasever\n"
                "bootstrap: bootstrap.yml\nrepos:\n  - repo: fedora\n    priority: 10\ncards: [bootstrap.yml]\n"
            )
            with (
                patch("ludos.build.shutil.which", side_effect=lambda name: name),
                patch("ludos.build._ensure_image", return_value=False),
                patch("ludos.build._create_orchestrator_image") as create_orchestrator,
                patch("ludos.build._create_repo_image") as create_repo,
                patch("ludos.build._extract_image_paths"),
                patch("ludos.build._apply_repo_priority"),
            ):
                context = resolve_build_manifest_context(
                    manifest, arch="x86_64", cache_version="20261102", cache_only=False,
                )
            create_orchestrator.assert_called_once()
            create_repo.assert_called_once()
            self.assertEqual(context.ci_registry, "ghcr.io/example")
            self.assertTrue(context.orchestrator.endswith("-20261102"))
            self.assertTrue(context.repo_images[0].endswith("-20261102"))


class GhcrCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.versions = {}
        self.manifests = {}
        self.api_calls = []

    def api(self, endpoint, *, method="GET", **kwargs):
        self.api_calls.append((method, endpoint))
        if endpoint.startswith("/users/") and "/packages/" not in endpoint:
            return {"type": "Organization"}
        if method == "DELETE":
            return None
        package = endpoint.split("/container/")[1].split("/versions")[0]
        return self.versions.get(package, [])

    def inspect(self, command, **kwargs):
        self.assertEqual(command[:3], ["skopeo", "inspect", "--raw"])
        self.assertTrue(command[3].startswith("docker://ghcr.io/"))
        ref = command[3].split("@", 1)[1]
        children = self.manifests.get(ref)
        body = {"schemaVersion": 2}
        if children is not None:
            body["manifests"] = [{"digest": child} for child in children]
        return NS(returncode=0, stdout=json.dumps(body))

    def clean(self, refs, **kwargs):
        with (
            patch("ludos.cleanup_ghcr._gh_api", side_effect=self.api),
            patch("ludos.cleanup_ghcr.subprocess.run", side_effect=self.inspect),
        ):
            return cleanup_ghcr_images("ghcr.io/example", refs, version="20261102", **kwargs)

    def test_current_arches_mirrors_and_shared_tags_survive(self) -> None:
        self.versions = {
            "cards": [version(1, "x86-current", "old-alias"), version(2, "arm-current"), version(3, "stale"), version(4)],
            "orchestrator": [version(5, "latest"), version(6, "f45-x86_64-20261102"), version(7, "f45-aarch64-20261026")],
            "repos": [version(8, "f45-aarch64-20261102"), version(9, "f45-x86_64-20261026")],
        }
        self.assertEqual(self.clean({"cards:x86-current", "cards:arm-current"}), 0)
        deleted = [int(path.rsplit("/", 1)[1]) for method, path in self.api_calls if method == "DELETE"]
        self.assertEqual(set(deleted), {3, 4, 7, 9})
        packages = {path.split("/container/")[1].split("/versions")[0] for method, path in self.api_calls if "/container/" in path}
        self.assertEqual(packages, set(GHCR_CLEANUP_REPOSITORIES))

    def test_untagged_children_and_nested_indexes_survive(self) -> None:
        self.versions["builds"] = [version(1, "current"), version(2), version(3), version(4)]
        self.manifests = {digest(1): [digest(2)], digest(2): [digest(3)]}
        self.clean({"builds:current"})
        deleted = [path for method, path in self.api_calls if method == "DELETE"]
        self.assertEqual(deleted, ["/orgs/example/packages/container/builds/versions/4"])

    def test_dry_run_does_not_delete(self) -> None:
        self.versions["cards"] = [version(1, "old")]
        self.clean(set(), dry_run=True)
        self.assertFalse(any(method == "DELETE" for method, path in self.api_calls))

    def test_inspection_failure_prevents_every_deletion(self) -> None:
        self.versions["repos"] = [version(1, "old")]
        self.versions["builds"] = [version(2, "current"), version(3, "old")]
        with (
            patch("ludos.cleanup_ghcr._gh_api", side_effect=self.api),
            patch("ludos.cleanup_ghcr.subprocess.run", return_value=NS(returncode=1)),
        ):
            with self.assertRaisesRegex(ConfigError, "Failed to inspect"):
                cleanup_ghcr_images("ghcr.io/example", {"builds:current"}, version="20261102")
        self.assertFalse(any(method == "DELETE" for method, path in self.api_calls))

    def test_listing_failure_prevents_every_deletion(self) -> None:
        self.versions["repos"] = [version(1, "old")]
        def api(endpoint, **kwargs):
            if "/container/builds/" in endpoint:
                raise ConfigError("API unavailable")
            return self.api(endpoint, **kwargs)
        with patch("ludos.cleanup_ghcr._gh_api", side_effect=api):
            with self.assertRaisesRegex(ConfigError, "API unavailable"):
                cleanup_ghcr_images("ghcr.io/example", set(), version="20261102")
        self.assertFalse(any(method == "DELETE" for method, path in self.api_calls))

    def test_delete_failure_is_propagated(self) -> None:
        self.versions["cards"] = [version(1, "old")]
        def api(endpoint, **kwargs):
            if kwargs.get("method") == "DELETE":
                raise ConfigError("Deletion denied")
            return self.api(endpoint, **kwargs)
        with patch("ludos.cleanup_ghcr._gh_api", side_effect=api):
            with self.assertRaisesRegex(ConfigError, "Deletion denied"):
                cleanup_ghcr_images("ghcr.io/example", set(), version="20261102")

    def test_personal_account_and_nested_package_prefix(self) -> None:
        def api(endpoint, **kwargs):
            self.api_calls.append((kwargs.get("method", "GET"), endpoint))
            if endpoint == "/users/example":
                return {"type": "User"}
            return []
        with patch("ludos.cleanup_ghcr._gh_api", side_effect=api):
            cleanup_ghcr_images("ghcr.io/example/ci", set(), version="20261102")
        self.assertIn(("GET", "/users/example/packages/container/ci%2Fcards/versions?per_page=100&page=1"), self.api_calls)

    def test_invalid_registry_is_rejected(self) -> None:
        for registry in ("", "docker.io/example", "https://ghcr.io/example", "ghcr.io/example/../other"):
            with self.assertRaises(ConfigError):
                validate_ghcr_registry(registry)


class GhcrApiTests(unittest.TestCase):
    def test_pagination_collects_all_pages(self) -> None:
        first = [version(number) for number in range(1, 101)]
        with patch("ludos.cleanup_ghcr._gh_api", side_effect=[first, [version(101)]]) as api:
            self.assertEqual(len(_package_versions("/package")), 101)
        self.assertEqual(api.call_args_list, [
            call("/package/versions?per_page=100&page=1", missing_ok=True),
            call("/package/versions?per_page=100&page=2", missing_ok=False),
        ])

    def test_http_json_and_delete_responses(self) -> None:
        for status, body, expected in ((200, '{"type":"User"}', {"type":"User"}), (204, "", None)):
            result = NS(returncode=0, stdout=f"HTTP/2.0 {status}\r\nContent-Type: application/json\r\n\r\n{body}", stderr="")
            with patch("ludos.cleanup_ghcr.subprocess.run", return_value=result):
                self.assertEqual(_gh_api("/endpoint", method="DELETE" if status == 204 else "GET"), expected)

    def test_missing_package_and_http_failures(self) -> None:
        for status in (401, 403, 404, 500):
            result = NS(returncode=1, stdout=f"HTTP/2.0 {status}\n\n{{}}", stderr="error")
            with patch("ludos.cleanup_ghcr.subprocess.run", return_value=result):
                if status == 404:
                    self.assertIsNone(_gh_api("/endpoint", missing_ok=True))
                else:
                    with self.assertRaisesRegex(ConfigError, str(status)):
                        _gh_api("/endpoint", missing_ok=True)
                with self.assertRaises(ConfigError):
                    _gh_api("/endpoint", method="DELETE")

    def test_malformed_version_metadata_fails_closed(self) -> None:
        for entries in ({}, [None], [{**version(1), "metadata": None}], [{**version(1), "name": "unexpected"}]):
            with patch("ludos.cleanup_ghcr._gh_api", return_value=entries):
                with self.assertRaises(ConfigError):
                    _package_versions("/package")

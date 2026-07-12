from __future__ import annotations

import datetime as _datetime
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from ludos.common import (
    _default_cache_version,
    _create_repo_image,
    _ensure_image,
    _remote_cache_image,
    _remote_cache_image_exists,
)


class RepoImageTests(unittest.TestCase):
    def test_refresh_uses_mounted_system_cache(self) -> None:
        with patch("ludos.common._create_scratch_image") as create:
            _create_repo_image(
                podman="podman",
                buildah="buildah",
                orchestrator="orchestrator:test",
                root_dir=Path("/workspace"),
                image="repos:test",
                repo_name="updates.repo",
                repo_id="updates",
                rendered_repo="[updates]\nmetalink=https://example.test\n",
            )

        body = "\n".join(create.call_args.kwargs["body"])
        self.assertIn("--setopt=cachedir=/ludos/dnf/cache", body)
        self.assertIn("--setopt=system_cachedir=/ludos/dnf/cache", body)
        self.assertIn("makecache --refresh", body)


class DefaultCacheVersionTests(unittest.TestCase):
    def test_uses_current_monday_date(self) -> None:
        timestamp = _datetime.datetime(
            2026,
            7,
            1,
            12,
            0,
            tzinfo=_datetime.UTC,
        )

        self.assertEqual(_default_cache_version(timestamp), "20260629")

    def test_rolls_over_on_monday_utc(self) -> None:
        sunday = _datetime.datetime(
            2026,
            6,
            28,
            23,
            59,
            59,
            tzinfo=_datetime.UTC,
        )
        monday = _datetime.datetime(
            2026,
            6,
            29,
            0,
            0,
            0,
            tzinfo=_datetime.UTC,
        )

        self.assertEqual(_default_cache_version(sunday), "20260622")
        self.assertEqual(_default_cache_version(monday), "20260629")

    def test_converts_aware_timestamp_to_utc(self) -> None:
        copenhagen_monday = _datetime.datetime(
            2026,
            6,
            29,
            1,
            30,
            tzinfo=_datetime.timezone(_datetime.timedelta(hours=2)),
        )

        self.assertEqual(_default_cache_version(copenhagen_monday), "20260622")

    def test_uses_monday_calendar_year_at_year_boundary(self) -> None:
        new_year = _datetime.datetime(
            2027,
            1,
            1,
            12,
            0,
            tzinfo=_datetime.UTC,
        )

        self.assertEqual(_default_cache_version(new_year), "20261228")


class CachedImageTests(unittest.TestCase):
    def test_local_hit_does_not_pull(self) -> None:
        with patch(
            "ludos.common.subprocess.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run:
            self.assertTrue(_ensure_image("podman", "cards:f44", "ghcr.io/anatase-org"))

        run.assert_called_once_with(
            ["podman", "image", "exists", "cards:f44"],
            check=False,
        )

    def test_remote_hit_pulls_and_tags_local_ref(self) -> None:
        with (
            patch(
                "ludos.common.subprocess.run",
                side_effect=[
                    SimpleNamespace(returncode=1),
                    SimpleNamespace(returncode=0),
                ],
            ) as run,
            patch(
                "ludos.common._run_streamed_command",
                return_value=(0, ""),
            ) as streamed,
            patch("ludos.common._remote_cache_image_exists", return_value=True),
            patch("ludos.common.log"),
        ):
            self.assertTrue(_ensure_image("podman", "cards:f44", "ghcr.io/anatase-org"))

        self.assertEqual(
            run.call_args_list,
            [
                call(["podman", "image", "exists", "cards:f44"], check=False),
                call(
                    ["podman", "tag", "ghcr.io/anatase-org/cards:f44", "cards:f44"],
                    check=True,
                ),
            ],
        )
        streamed.assert_called_once_with(
            ["podman", "pull", "ghcr.io/anatase-org/cards:f44"]
        )

    def test_remote_miss_skips_pull(self) -> None:
        with (
            patch(
                "ludos.common.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ),
            patch("ludos.common._remote_cache_image_exists", return_value=False),
            patch("ludos.common._run_streamed_command") as streamed,
            patch("ludos.common.log"),
        ):
            self.assertFalse(
                _ensure_image("podman", "cards:f44", "ghcr.io/anatase-org")
            )
        streamed.assert_not_called()

    def test_unset_registry_does_not_pull(self) -> None:
        with patch(
            "ludos.common.subprocess.run",
            return_value=SimpleNamespace(returncode=1),
        ) as run:
            self.assertFalse(_ensure_image("podman", "cards:f44", ""))

        run.assert_called_once_with(
            ["podman", "image", "exists", "cards:f44"],
            check=False,
        )

    def test_remote_mapping_skips_localhost_refs(self) -> None:
        self.assertIsNone(
            _remote_cache_image("ghcr.io/anatase-org", "localhost/cards:f44")
        )

    def test_remote_image_exists_checks_manifest_head(self) -> None:
        with (
            patch("ludos.common._registry_basic_auth", return_value="basic-token"),
            patch("ludos.common._registry_head", return_value=(200, {})) as head,
        ):
            exists = _remote_cache_image_exists(
                "ghcr.io/anatase-org/orchestrator:f44",
            )

        self.assertTrue(exists)
        url, headers = head.call_args.args
        self.assertEqual(
            url,
            "https://ghcr.io/v2/anatase-org/orchestrator/manifests/f44",
        )
        self.assertEqual(headers["Authorization"], "Basic basic-token")
        self.assertIn(
            "application/vnd.oci.image.manifest.v1+json",
            headers["Accept"],
        )

    def test_remote_image_exists_uses_bearer_challenge(self) -> None:
        challenge = (
            'Bearer realm="https://ghcr.io/token",'
            'service="ghcr.io",'
            'scope="repository:anatase-org/orchestrator:pull"'
        )
        with (
            patch("ludos.common._registry_basic_auth", return_value="basic-token"),
            patch(
                "ludos.common._registry_bearer_token",
                return_value="bearer-token",
            ) as token,
            patch(
                "ludos.common._registry_head",
                side_effect=[(401, {"www-authenticate": challenge}), (200, {})],
            ) as head,
        ):
            exists = _remote_cache_image_exists(
                "ghcr.io/anatase-org/orchestrator:f44",
            )

        self.assertTrue(exists)
        token.assert_called_once_with(challenge, "basic-token")
        self.assertEqual(
            head.call_args_list[1].args[1]["Authorization"],
            "Bearer bearer-token",
        )

from __future__ import annotations

import datetime as _datetime
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from ludos.common import _default_cache_version, _ensure_image, _remote_cache_image


class DefaultCacheVersionTests(unittest.TestCase):
    def test_uses_utc_iso_week(self) -> None:
        timestamp = _datetime.datetime(
            2026,
            7,
            1,
            12,
            0,
            tzinfo=_datetime.UTC,
        )

        self.assertEqual(_default_cache_version(timestamp), "2026.27")

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

        self.assertEqual(_default_cache_version(sunday), "2026.26")
        self.assertEqual(_default_cache_version(monday), "2026.27")

    def test_converts_aware_timestamp_to_utc(self) -> None:
        copenhagen_monday = _datetime.datetime(
            2026,
            6,
            29,
            1,
            30,
            tzinfo=_datetime.timezone(_datetime.timedelta(hours=2)),
        )

        self.assertEqual(_default_cache_version(copenhagen_monday), "2026.26")


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

    def test_remote_miss_returns_false(self) -> None:
        with (
            patch(
                "ludos.common.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ),
            patch("ludos.common._run_streamed_command", return_value=(125, "")),
            patch("ludos.common.log"),
        ):
            self.assertFalse(
                _ensure_image("podman", "cards:f44", "ghcr.io/anatase-org")
            )

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

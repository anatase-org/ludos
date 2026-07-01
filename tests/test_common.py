from __future__ import annotations

import datetime as _datetime
import unittest

from ludos.common import _default_cache_version


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

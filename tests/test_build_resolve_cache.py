from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.build import _resolve_cache_key, _run_cached_transaction_preview


class ResolveCacheTests(unittest.TestCase):
    def test_valid_aborted_preview_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "resolves"
            cmd = ["podman", "run", "orchestrator", "dnf5"]
            preview = subprocess.CompletedProcess(
                cmd,
                1,
                """Installing:
 Package  Arch    Version       Repository  Size
 bash     x86_64  0:5.3-2.fc44  fedora      1 MiB

Transaction Summary:
 Installing: 1 package
""",
                "Operation aborted by the user.\n",
            )
            with patch("ludos.build.subprocess.run", return_value=preview) as run:
                first = _run_cached_transaction_preview(
                    cmd,
                    cache,
                    (),
                )
                second = _run_cached_transaction_preview(
                    cmd,
                    cache,
                    (),
                )
            self.assertEqual(first.returncode, 1)
            self.assertEqual(second.stdout, preview.stdout)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(tuple(cache.glob("*.json"))), 1)

    def test_invalid_aborted_preview_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "resolves"
            cmd = ["podman", "run", "orchestrator", "dnf5"]
            failure = subprocess.CompletedProcess(
                cmd,
                1,
                "",
                "Operation aborted by the user.\n",
            )
            with patch("ludos.build.subprocess.run", return_value=failure):
                result = _run_cached_transaction_preview(
                    cmd,
                    cache,
                    (),
                )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(cache.exists())

    def test_failed_preview_is_retried_and_success_is_cached(self) -> None:
        for returncode in (1, 125, 126, 127, -15):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as temp:
                cache = Path(temp) / "resolves"
                cmd = ["podman", "run", "orchestrator", "dnf5"]
                failure = subprocess.CompletedProcess(cmd, returncode, "", "failed")
                success = subprocess.CompletedProcess(cmd, 0, "resolved", "")
                with patch(
                    "ludos.build.subprocess.run", side_effect=[failure, success]
                ) as run:
                    self.assertEqual(
                        _run_cached_transaction_preview(cmd, cache, ()).returncode,
                        returncode,
                    )
                    self.assertFalse(cache.exists())
                    self.assertEqual(
                        _run_cached_transaction_preview(cmd, cache, ()).stdout,
                        "resolved",
                    )
                    self.assertEqual(
                        _run_cached_transaction_preview(cmd, cache, ()).stdout,
                        "resolved",
                    )
                    self.assertEqual(run.call_count, 2)

    def test_legacy_cached_failure_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            cmd = ["podman", "run", "orchestrator", "dnf5"]
            cache_file = cache / f"{_resolve_cache_key(cmd, ())}.json"
            cache_file.write_text(
                json.dumps({"returncode": 126, "stderr": "old mount failure"}),
                encoding="utf-8",
            )
            failure = subprocess.CompletedProcess(cmd, 1, "", "new failure")
            with patch("ludos.build.subprocess.run", return_value=failure) as run:
                result = _run_cached_transaction_preview(cmd, cache, ())
            run.assert_called_once()
            self.assertEqual(result.stderr, "new failure")
            self.assertFalse(cache_file.exists())

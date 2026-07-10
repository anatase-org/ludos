from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from ludos.build import _create_repo_image


class BuildRepoImageTests(unittest.TestCase):
    def test_refresh_uses_mounted_system_cache(self) -> None:
        with patch("ludos.build._create_scratch_image") as create:
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


if __name__ == "__main__":
    unittest.main()

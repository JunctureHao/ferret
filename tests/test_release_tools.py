from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from scripts import release_version


class ReleaseVersionTests(unittest.TestCase):
    def test_numeric_prerelease_order_and_stable_precedence(self):
        self.assertGreater(
            release_version.version_key("1.0.0-rc.10"),
            release_version.version_key("1.0.0-rc.2"),
        )
        self.assertGreater(
            release_version.version_key("1.0.0"),
            release_version.version_key("1.0.0-rc.10"),
        )

    def test_already_published_prerelease_is_not_republished(self):
        releases = [
            {"tag_name": "v1.0.0-rc.10", "prerelease": True},
            {"tag_name": "v1.0.0", "draft": True},
        ]
        self.assertEqual(
            release_version.compare("1.0.0-rc.10", releases)["bump"], "false"
        )
        self.assertEqual(release_version.compare("1.0.0", releases)["bump"], "true")
        self.assertEqual(release_version.compare("1.0.0", [])["bump"], "true")

    def test_invalid_semver_cannot_authorize_first_release(self):
        for version in ("1.0.0-01", "1.0.0-rc.01", "1.0.0+a..b", "1.0.0+.", "1２.0.0"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release_version.compare(version, [])

    def test_build_metadata_does_not_change_version_precedence(self):
        self.assertEqual(
            release_version.version_key("v1.0.0+build-one"),
            release_version.version_key("1.0.0+build-two"),
        )
        self.assertEqual(
            release_version.compare(
                "1.0.0+build-two", [{"tag_name": "v1.0.0+build-one"}]
            )["bump"],
            "false",
        )

    def test_previous_stable_is_selected_independently_of_latest_prerelease(self):
        releases = [
            {"tag_name": "v2.0.0-rc.1", "prerelease": True},
            {"tag_name": "v1.0.10", "prerelease": False},
            {"tag_name": "v1.0.9", "prerelease": False},
        ]
        self.assertEqual(
            release_version.compare("2.0.0", releases),
            {"bump": "true", "previous": "v2.0.0-rc.1", "previous_stable": "v1.0.10"},
        )

    def test_failed_release_query_does_not_write_upload_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with (
                patch.dict(
                    os.environ,
                    GITHUB_REPOSITORY="owner/repo",
                    GITHUB_OUTPUT=str(output),
                ),
                patch.object(
                    release_version.subprocess,
                    "run",
                    side_effect=subprocess.CalledProcessError(1, "gh"),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                release_version.main()
            self.assertFalse(output.exists())

    def test_utf8_release_body_is_decoded_under_windows_ansi_locale(self):
        payload = json.dumps(
            [[{"tag_name": "v1.0.0", "body": "构建成功"}]], ensure_ascii=False
        ).encode("utf-8")
        run = subprocess.run

        def query_releases(_command, **kwargs):
            # A real child writes gh's UTF-8 bytes into subprocess's text pipe.
            return run(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stdout.buffer.write({payload!r})",
                ],
                **kwargs,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                '[project]\nversion = "1.0.1"\n', encoding="utf-8"
            )
            output = root / "output"
            with (
                chdir(root),
                patch.dict(
                    os.environ,
                    GITHUB_REPOSITORY="owner/repo",
                    GITHUB_OUTPUT=str(output),
                ),
                patch.object(
                    release_version.subprocess, "run", side_effect=query_releases
                ),
                # Windows runners default to cp1252 for redirected text streams.
                patch("subprocess._text_encoding", return_value="cp1252"),
            ):
                release_version.main()
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "bump=true\nprevious=v1.0.0\nprevious_stable=v1.0.0\n",
            )

    def test_packaging_dry_run_does_not_need_compiled_artifacts(self):
        result = subprocess.run(
            [sys.executable, "scripts/package.py", "--dry-run", "--upload"],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<build-stamp>", result.stdout)
        self.assertIn("upload github", result.stdout)

    def test_build_metadata_hyphen_does_not_publish_as_prerelease(self):
        result = subprocess.run(
            [
                sys.executable,
                "scripts/package.py",
                "--dry-run",
                "--upload",
                "--version",
                "1.0.0+build-run",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--pre", result.stdout.split())

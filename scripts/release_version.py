"""Compare the package version against all published releases, including prereleases."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path


def version_key(version: str) -> tuple:
    match = re.fullmatch(
        r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
        r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        version,
    )
    if match is None:
        raise ValueError(f"Invalid release version: {version}")
    major, minor, patch, prerelease = match.groups()
    identifiers = (prerelease or "").split(".")
    if any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in identifiers
    ):
        raise ValueError(f"Invalid release version: {version}")
    parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in identifiers
    )
    return int(major), int(minor), int(patch), not prerelease, parts


def compare(version: str, releases: list[dict]) -> dict[str, str]:
    candidate = version_key(version)
    published = [release for release in releases if not release.get("draft")]
    tags = [release["tag_name"] for release in published]
    previous = max(tags, key=version_key, default="")
    stable = [
        release["tag_name"] for release in published if not release.get("prerelease")
    ]
    return {
        "bump": str(not previous or candidate > version_key(previous)).lower(),
        "previous": previous,
        "previous_stable": max(stable, key=version_key, default=""),
    }


def main() -> None:
    with Path("pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    version_key(version)
    # A successful empty list is the only first-release case. Authentication,
    # rate limits and transport errors fail the gate instead of authorising upload.
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{os.environ['GITHUB_REPOSITORY']}/releases?per_page=100",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    pages = json.loads(result.stdout)
    outputs = compare(version, [release for page in pages for release in page])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
        for key, value in outputs.items():
            print(f"{key}={value}", file=stream)
    print(f"Package {version}: {outputs}")


if __name__ == "__main__":
    main()

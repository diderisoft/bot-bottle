"""Canary: the pinned gitleaks release remains downloadable and executable.

The gateway Dockerfile verifies this archive during an image build. Repeating
the upstream check weekly keeps registry/release drift out of normal pull
requests while proving that the pinned URL, architecture checksum, archive
shape, and binary still agree.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile.gateway"


def _docker_arg(text: str, name: str) -> str:
    match = re.search(rf"^ARG {re.escape(name)}=(\S+)$", text, re.MULTILINE)
    if match is None:
        raise AssertionError(f"Dockerfile.gateway has no concrete ARG {name}")
    return match.group(1)


@unittest.skipUnless(
    os.environ.get("BOT_BOTTLE_RUN_CANARIES") == "1",
    "canary suite is opt-in; set BOT_BOTTLE_RUN_CANARIES=1 to run",
)
class TestGitleaksRelease(unittest.TestCase):
    def test_pinned_archive_checksum_and_binary(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        version = _docker_arg(dockerfile, "GITLEAKS_VERSION")
        machine = platform.machine().lower()
        architectures = {
            "x86_64": ("linux_x64", "GITLEAKS_SHA256_AMD64"),
            "amd64": ("linux_x64", "GITLEAKS_SHA256_AMD64"),
            "aarch64": ("linux_arm64", "GITLEAKS_SHA256_ARM64"),
            "arm64": ("linux_arm64", "GITLEAKS_SHA256_ARM64"),
        }
        if machine not in architectures:
            self.fail(f"unsupported canary runner architecture: {machine}")
        asset, checksum_arg = architectures[machine]
        expected_checksum = _docker_arg(dockerfile, checksum_arg)
        url = (
            "https://github.com/gitleaks/gitleaks/releases/download/"
            f"v{version}/gitleaks_{version}_{asset}.tar.gz"
        )

        with tempfile.TemporaryDirectory(prefix="bot-bottle-gitleaks-canary.") as tmp:
            archive = Path(tmp) / "gitleaks.tar.gz"
            urllib.request.urlretrieve(url, archive)
            self.assertEqual(
                expected_checksum,
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                "the pinned upstream archive no longer matches Dockerfile.gateway",
            )
            with tarfile.open(archive, "r:gz") as bundle:
                member = bundle.getmember("gitleaks")
                source = bundle.extractfile(member)
                if source is None:
                    self.fail("gitleaks archive member is not a regular file")
                binary = Path(tmp) / "gitleaks"
                binary.write_bytes(source.read())
            binary.chmod(0o755)
            result = subprocess.run(
                [str(binary), "version"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(version, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

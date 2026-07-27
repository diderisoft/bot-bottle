from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.generate_release_manifest import main


class TestGenerateReleaseManifest(unittest.TestCase):
    def test_writes_validated_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "release.json"
            rc = main([
                "--source-commit", "1" * 40,
                "--orchestrator-image",
                "registry/orchestrator@sha256:" + "a" * 64,
                "--gateway-image", "registry/gateway@sha256:" + "b" * 64,
                "--firecracker-orchestrator-version", "orch-v1",
                "--firecracker-orchestrator-sha256", "c" * 64,
                "--firecracker-gateway-version", "gateway-v1",
                "--firecracker-gateway-sha256", "d" * 64,
                "--output", str(output),
            ])
            self.assertEqual(0, rc)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("orch-v1", data["firecracker"]["orchestrator"]["version"])

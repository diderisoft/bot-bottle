"""Unit: bottle 'runtime' field is no longer supported (PRD 0003).

gVisor is now auto-detected by the Docker factory. A manifest carrying
the legacy 'runtime' field must fail, regardless of value, rather than
silently ignoring."""

import unittest
from typing import Any

from bot_bottle.log import Die
from bot_bottle.manifest import Bottle, Manifest


def _manifest_with_runtime(value: object) -> dict[str, Any]:
    return {
        "bottles": {"dev": {"runtime": value}},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestManifestRuntimeRemoved(unittest.TestCase):
    def test_loads_when_runtime_absent(self):
        m = Manifest.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        self.assertIn("dev", m.bottles)

    def test_bottle_dataclass_has_no_runtime_attribute(self):
        self.assertFalse(hasattr(Bottle(), "runtime"))

    def test_any_runtime_value_is_rejected(self):
        for value in ("runsc", "runc", "kata-runtime", "", 42, None):
            with self.subTest(value=value):
                with self.assertRaises(Die):
                    Manifest.from_json_obj(_manifest_with_runtime(value))


if __name__ == "__main__":
    unittest.main()

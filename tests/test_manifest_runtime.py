"""Unit: manifest_bottle_runtime — defaults to runc, accepts runsc,
rejects unknown values and non-strings."""

import unittest

from claude_bottle.log import Die
from claude_bottle.manifest import manifest_bottle_runtime


def _bottle(runtime_value: object | None) -> dict:
    """Build a minimal manifest with one bottle whose runtime field is
    set (or absent if `runtime_value is _ABSENT`)."""
    bottle: dict = {}
    if runtime_value is not _ABSENT:
        bottle["runtime"] = runtime_value
    return {
        "bottles": {"dev": bottle},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


_ABSENT = object()


class TestManifestBottleRuntime(unittest.TestCase):
    def test_default_runc_when_absent(self):
        self.assertEqual("runc", manifest_bottle_runtime(_bottle(_ABSENT), "dev"))

    def test_explicit_runc(self):
        self.assertEqual("runc", manifest_bottle_runtime(_bottle("runc"), "dev"))

    def test_explicit_runsc(self):
        self.assertEqual("runsc", manifest_bottle_runtime(_bottle("runsc"), "dev"))

    def test_rejects_unknown_runtime(self):
        with self.assertRaises(Die):
            manifest_bottle_runtime(_bottle("kata-runtime"), "dev")

    def test_rejects_non_string(self):
        with self.assertRaises(Die):
            manifest_bottle_runtime(_bottle(42), "dev")

    def test_rejects_empty_string(self):
        with self.assertRaises(Die):
            manifest_bottle_runtime(_bottle(""), "dev")


if __name__ == "__main__":
    unittest.main()

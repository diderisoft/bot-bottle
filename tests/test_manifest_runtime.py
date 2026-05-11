"""Unit: bottle runtime — Manifest.from_json_obj defaults runtime to runc,
accepts runsc, and rejects unknown values, non-strings, and empty strings."""

import unittest

from claude_bottle.log import Die
from claude_bottle.manifest import Manifest


_ABSENT = object()


def _bottle(runtime_value: object) -> dict:
    """Build a minimal manifest JSON shape with one bottle whose runtime
    field is set (or absent if `runtime_value is _ABSENT`)."""
    bottle: dict = {}
    if runtime_value is not _ABSENT:
        bottle["runtime"] = runtime_value
    return {
        "bottles": {"dev": bottle},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestManifestBottleRuntime(unittest.TestCase):
    def test_default_runc_when_absent(self):
        m = Manifest.from_json_obj(_bottle(_ABSENT))
        self.assertEqual("runc", m.bottles["dev"].runtime)

    def test_explicit_runc(self):
        m = Manifest.from_json_obj(_bottle("runc"))
        self.assertEqual("runc", m.bottles["dev"].runtime)

    def test_explicit_runsc(self):
        m = Manifest.from_json_obj(_bottle("runsc"))
        self.assertEqual("runsc", m.bottles["dev"].runtime)

    def test_rejects_unknown_runtime(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_bottle("kata-runtime"))

    def test_rejects_non_string(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_bottle(42))

    def test_rejects_empty_string(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_bottle(""))


if __name__ == "__main__":
    unittest.main()

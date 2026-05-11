"""Unit: bottle 'runtime' field is no longer supported (PRD 0003).

gVisor is now auto-detected by the Docker factory. A manifest carrying
the legacy 'runtime' field must fail loudly with a message pointing the
user at the auto-detect behavior, rather than silently ignoring."""

import io
import sys
import unittest

from claude_bottle.log import Die
from claude_bottle.manifest import Bottle, Manifest


_ABSENT = object()


def _manifest(runtime_value: object) -> dict:
    """Build a minimal manifest JSON shape with one bottle whose runtime
    field is set (or absent if `runtime_value is _ABSENT`)."""
    bottle: dict = {}
    if runtime_value is not _ABSENT:
        bottle["runtime"] = runtime_value
    return {
        "bottles": {"dev": bottle},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    }


class TestManifestRuntimeRemoved(unittest.TestCase):
    def test_loads_when_runtime_absent(self):
        m = Manifest.from_json_obj(_manifest(_ABSENT))
        self.assertIn("dev", m.bottles)

    def test_bottle_dataclass_has_no_runtime_attribute(self):
        """Structural check: the field has been removed from the dataclass."""
        b = Bottle()
        self.assertFalse(hasattr(b, "runtime"))

    def test_rejects_runsc_value_with_helpful_message(self):
        captured = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = captured
        try:
            with self.assertRaises(Die):
                Manifest.from_json_obj(_manifest("runsc"))
        finally:
            sys.stderr = old_stderr
        msg = captured.getvalue()
        self.assertIn("'runtime'", msg, "error names the field")
        self.assertIn("auto-detect", msg, "error points at the new behavior")

    def test_rejects_runc_value(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest("runc"))

    def test_rejects_unknown_value(self):
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest("kata-runtime"))

    def test_rejects_non_string(self):
        """Any presence of the field is an error; type is not consulted."""
        with self.assertRaises(Die):
            Manifest.from_json_obj(_manifest(42))


if __name__ == "__main__":
    unittest.main()

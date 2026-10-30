"""Unit: `check_published_artifacts.py --match-sources`, the release's guard
that the pins it commits match the tree it tags."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import image_inputs as ii
from scripts import check_published_artifacts as cpa
from scripts.check_published_artifacts import check_manifest

_IMAGE = "bot-bottle-gateway"
_PYPROJECT = '[project]\nname = "bot-bottle"\nversion = "{version}"\ndependencies = [{deps}]\n'


class MatchSources(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ii.BUILD_INPUTS[_IMAGE]:
            self.write(name, name)
        self.write("bot_bottle/gateway/bootstrap.py", "x = 1\n")
        # The tested commit, still at the previous version.
        self.write("pyproject.toml", _PYPROJECT.format(version="0.1.4", deps=""))
        tested = ii.inputs_hash(_IMAGE, repo_root=self.root)
        self.write("published-artifacts.json", json.dumps({_IMAGE: {
            "version": "0.1.5",
            "inputs_hash": tested,
            "protocol": 1,
            "artifacts": {
                "oci_linux-arm64": {
                    "type": "oci", "platform": "linux-arm64",
                    "url": "gitea.dideric.is/didericis/bot-bottle-gateway:0.1.5-arm64",
                    "digest": "sha256:" + "a" * 64,
                },
            },
        }}))

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_release_version_bump_still_matches(self) -> None:
        self.write("pyproject.toml", _PYPROJECT.format(version="0.1.5", deps=""))
        self.assertEqual([], check_manifest(self.root, match_sources=True))

    def test_changed_sources_fail(self) -> None:
        self.write("bot_bottle/gateway/bootstrap.py", "x = 2\n")
        problems = check_manifest(self.root, match_sources=True)
        self.assertEqual(1, len(problems))
        self.assertIn(f"{_IMAGE}: pinned inputs_hash", problems[0])

    def test_changed_dependency_fails(self) -> None:
        self.write("pyproject.toml", _PYPROJECT.format(version="0.1.5", deps='"x"'))
        self.assertEqual(1, len(check_manifest(self.root, match_sources=True)))

    def test_off_by_default(self) -> None:
        self.write("bot_bottle/gateway/bootstrap.py", "x = 2\n")
        self.assertEqual([], check_manifest(self.root))

    def test_an_unknown_image_is_reported_once(self) -> None:
        data = json.loads((self.root / "published-artifacts.json").read_text())
        data["bot-bottle-nope"] = data[_IMAGE]
        self.write("published-artifacts.json", json.dumps(data))
        problems = check_manifest(self.root, match_sources=True)
        self.assertEqual(1, len(problems))
        self.assertIn("bot-bottle-nope: not a known published image", problems[0])

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cpa, "REPO_ROOT", self.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cpa.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_main_passes_a_matching_tree(self) -> None:
        self.write("pyproject.toml", _PYPROJECT.format(version="0.1.5", deps=""))
        code, out, _ = self.run_main("--match-sources")
        self.assertEqual(0, code)
        self.assertIn("matches this tree", out)

    def test_main_fails_a_mismatch_and_names_the_image(self) -> None:
        self.write("bot_bottle/gateway/bootstrap.py", "x = 2\n")
        code, _, err = self.run_main("--match-sources")
        self.assertEqual(1, code)
        self.assertIn(f"published-artifacts policy: {_IMAGE}: pinned inputs_hash", err)

    def test_main_without_the_flag_keeps_the_old_check(self) -> None:
        self.write("bot_bottle/gateway/bootstrap.py", "x = 2\n")
        code, out, err = self.run_main()
        self.assertEqual(0, code)
        self.assertIn("artifacts manifest is well-formed", out)
        self.assertEqual("", err)


if __name__ == "__main__":
    unittest.main()

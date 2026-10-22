"""`image_inputs` hashes exactly what each image's Dockerfile bakes (PRD 0087)."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from bot_bottle import image_inputs as ii
from bot_bottle.control.lifecycle import source_hash

ROOT = Path(__file__).resolve().parents[2]
INFRA = ("bot-bottle-gateway", "bot-bottle-orchestrator")


def copy_sources(dockerfile: Path) -> list[str]:
    """Build-context paths a Dockerfile `COPY`s, trailing slashes dropped."""
    text = re.sub(r"\\\n", " ", dockerfile.read_text(encoding="utf-8"))
    sources: list[str] = []
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or tokens[0].upper() != "COPY":
            continue
        if any(t.startswith("--from=") for t in tokens):
            continue
        args = [t for t in tokens[1:] if not t.startswith("--")]
        sources.extend(a.rstrip("/") for a in args[:-1])
    return sources


def dockerfiles(image: str) -> set[Path]:
    return {ROOT / ii.DOCKERFILES[image], ROOT / ii.rootfs_dockerfile(image)}


def covered(source: str, hashed: tuple[str, ...]) -> bool:
    return any(source == h or source.startswith(h + "/") for h in hashed)


class DockerfilesMatchHash(unittest.TestCase):
    def test_every_copied_path_is_hashed(self) -> None:
        for image in ii.IMAGES:
            hashed = (*ii.BUILD_INPUTS[image], *ii.PACKAGE_PATHS.get(image, ()),
                      "pyproject.toml")
            for dockerfile in dockerfiles(image):
                for source in copy_sources(dockerfile):
                    with self.subTest(image=image, source=source):
                        self.assertTrue(covered(source, hashed),
                                        f"{dockerfile.name} copies unhashed {source}")

    def test_every_hashed_package_path_is_copied(self) -> None:
        for image, paths in ii.PACKAGE_PATHS.items():
            copied = {s for d in dockerfiles(image) for s in copy_sources(d)}
            with self.subTest(image=image):
                self.assertEqual(set(), set(paths) - copied)

    def test_infra_images_bake_no_host_code(self) -> None:
        for image in INFRA:
            for source in (s for d in dockerfiles(image) for s in copy_sources(d)):
                if source.startswith("bot_bottle"):
                    with self.subTest(image=image, source=source):
                        self.assertTrue(covered(source, ii.PACKAGE_PATHS[image]))
                        self.assertNotEqual("bot_bottle", source)


class HashScope(unittest.TestCase):
    """A host-only change leaves the infra hashes alone; a contract change
    bumps both; a service change bumps only its own image."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for image in ii.IMAGES:
            for name in ii.BUILD_INPUTS[image]:
                self.write(name, name)
        self.write("pyproject.toml", "")
        for name in ("__init__.py", "contract/http.py", "lib/log.py",
                     "orchestrator/api.py", "gateway/bootstrap.py", "cli/main.py"):
            self.write(f"bot_bottle/{name}", "x = 1\n")

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def hashes(self) -> dict[str, str]:
        return {image: ii.inputs_hash(image, repo_root=self.root) for image in INFRA}

    def changed_after(self, name: str) -> set[str]:
        before = self.hashes()
        self.write(name, "x = 2\n")
        after = self.hashes()
        return {image for image in INFRA if before[image] != after[image]}

    def test_host_change_bumps_neither(self) -> None:
        self.assertEqual(set(), self.changed_after("bot_bottle/cli/main.py"))
        self.assertEqual(set(), self.changed_after("bot_bottle/cli/new.py"))

    def test_shared_change_bumps_both(self) -> None:
        self.assertEqual(set(INFRA), self.changed_after("bot_bottle/contract/http.py"))
        self.assertEqual(set(INFRA), self.changed_after("bot_bottle/lib/log.py"))
        self.assertEqual(set(INFRA), self.changed_after("bot_bottle/__init__.py"))

    def test_service_change_bumps_only_its_image(self) -> None:
        self.assertEqual({"bot-bottle-gateway"},
                         self.changed_after("bot_bottle/gateway/bootstrap.py"))
        self.assertEqual({"bot-bottle-orchestrator"},
                         self.changed_after("bot_bottle/orchestrator/api.py"))

    def test_orchestrator_source_hash_ignores_host_code(self) -> None:
        """The running orchestrator is recreated on its own code changes only."""
        before = source_hash(self.root)
        self.write("bot_bottle/cli/main.py", "x = 2\n")
        self.write("bot_bottle/gateway/bootstrap.py", "x = 2\n")
        self.assertEqual(before, source_hash(self.root))
        self.write("bot_bottle/contract/http.py", "x = 2\n")
        self.assertNotEqual(before, source_hash(self.root))

    def test_bytecode_is_ignored(self) -> None:
        self.assertEqual(
            set(), self.changed_after("bot_bottle/lib/__pycache__/log.cpython-313.pyc"))


if __name__ == "__main__":
    unittest.main()

"""PRD 0085 overlays, built for real on the docker backend.

A throwaway local registry stands in for the published-image registry, so the
overlay is built `FROM <ref@digest>` exactly as it is in production — without
publishing anything. Both overlay shapes go through the docker backend's own
resolver (`oci_agent_image` with the docker runtime): the structured `files`
form and the free-form `dockerfile` form. Each result is run to check its content landed
and that the provider's non-root user was restored.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from bot_bottle.backend import oci_agent_image
from bot_bottle.backend.docker import launch as launch_mod
from bot_bottle.cli.settings import settings_from_env
from bot_bottle.manifest import ManifestImageOverlay
from bot_bottle.published_artifacts import FORMAT_OCI, Artifact
from tests._backend import skip_unless_backend

REGISTRY_IMAGE = "registry:2"
# The claude provider's image_user; the overlay must hand the image back to it.
IMAGE_USER = "node"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=300,
    )


@skip_unless_backend("docker")
class TestAgentOverlayDocker(unittest.TestCase):
    registry: str
    base_ref: str
    pin: Artifact
    _names: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        run_id = uuid.uuid4().hex[:8]
        cls._names = []
        cls.registry = f"bb-overlay-registry-{run_id}"
        _docker("run", "-d", "--rm", "--name", cls.registry,
                "-p", "127.0.0.1::5000", REGISTRY_IMAGE)
        port = _docker("port", cls.registry, "5000/tcp").stdout.strip().rsplit(":", 1)[1]
        repo = f"localhost:{port}/bot-bottle-claude"
        with tempfile.TemporaryDirectory() as tmp:
            # A minimal stand-in for the published agent image: non-root at the
            # end, like the real defaults.
            (Path(tmp) / "Dockerfile").write_text(
                f"FROM busybox\nRUN adduser -D -u 1000 {IMAGE_USER}\nUSER {IMAGE_USER}\n"
            )
            _docker("build", "-q", "-t", f"{repo}:base", tmp)
        _docker("push", f"{repo}:base")
        digests = _docker("inspect", "--format", "{{index .RepoDigests 0}}", f"{repo}:base")
        cls.base_ref = digests.stdout.strip()
        cls.pin = Artifact(
            image="bot-bottle-claude", format=FORMAT_OCI, platform="linux/amd64",
            url=f"{repo}:base", digest=cls.base_ref.rsplit("@", 1)[1],
        )
        cls._names.append(f"{repo}:base")

    @classmethod
    def tearDownClass(cls) -> None:
        for name in cls._names:
            _docker("rmi", "-f", name, check=False)
        _docker("rm", "-f", cls.registry, check=False)

    def _build(self, src: Path, overlay: ManifestImageOverlay) -> str:
        plan = cast(Any, SimpleNamespace(
            spec=SimpleNamespace(user_cwd=str(src), image_policy="build"),
            manifest=SimpleNamespace(agent=SimpleNamespace(agent_provider=SimpleNamespace(
                overlay=overlay, builds_custom_image=False,
            ))),
        ))
        # The base is already in the local store (it was built and pushed
        # here), so the resolver builds on it without contacting the registry.
        with mock.patch.object(oci_agent_image.agent_image, "resolve_agent_pin",
                               return_value=self.pin):
            tag = oci_agent_image.resolve_published_agent_image(
                plan, launch_mod._docker_runtime(settings_from_env()),  # pyright: ignore[reportPrivateUsage]
                harness="claude", image=f"bb-overlay-test-{uuid.uuid4().hex[:8]}:latest",
                smoke_test=(),
            )
        assert tag is not None
        self._names.append(tag)
        return tag

    def _run(self, tag: str, script: str) -> str:
        return _docker("run", "--rm", "--entrypoint", "sh", tag, "-c", script).stdout.strip()

    def test_base_is_digest_pinned(self) -> None:
        self.assertRegex(self.base_ref, r"@sha256:[0-9a-f]{64}$")

    def test_files_overlay_lands_under_overlay_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "extra").mkdir()
            (src / "extra" / "hello.txt").write_text("hello from the overlay\n")
            tag = self._build(src, ManifestImageOverlay(files=("extra/hello.txt",)))
        self.assertEqual(
            "hello from the overlay",
            self._run(tag, "cat /opt/bot-bottle/overlay/extra/hello.txt"),
        )
        self.assertEqual(IMAGE_USER, self._run(tag, "id -un"))

    def test_dockerfile_overlay_builds_on_the_pinned_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            ctx = src / "overlay"
            ctx.mkdir()
            (ctx / "greeting.txt").write_text("built FROM ${BASE}\n")
            # Switches to root and never switches back: the staged copy must
            # restore the provider's user anyway.
            (ctx / "Dockerfile").write_text(
                "ARG BASE\n"
                "FROM ${BASE}\n"
                "USER root\n"
                "COPY greeting.txt /etc/greeting\n"
                "RUN chmod 644 /etc/greeting && touch /etc/overlay-ran-as-root\n"
            )
            tag = self._build(src, ManifestImageOverlay(dockerfile="overlay/Dockerfile"))
        self.assertEqual("built FROM ${BASE}", self._run(tag, "cat /etc/greeting"))
        self.assertEqual("yes", self._run(tag, "test -f /etc/overlay-ran-as-root && echo yes"))
        self.assertEqual(IMAGE_USER, self._run(tag, "id -un"))

    def test_heredoc_overlay_still_ends_as_the_provider_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "Dockerfile").write_text(
                "FROM ${BASE}\n"
                "USER root\n"
                "RUN <<EOF\n"
                "echo heredoc > /etc/heredoc\n"
                "EOF\n"
            )
            tag = self._build(src, ManifestImageOverlay(dockerfile="Dockerfile"))
        self.assertEqual("heredoc", self._run(tag, "cat /etc/heredoc"))
        self.assertEqual(IMAGE_USER, self._run(tag, "id -un"))


if __name__ == "__main__":
    unittest.main()

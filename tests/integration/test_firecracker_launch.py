"""Integration: Firecracker microVM launch.

End-to-end against a real Firecracker microVM: prepare + launch a bottle
on the firecracker backend and verify the agent execs after provisioning
and that the egress proxy env is wired to the sidecar.

Gated on the `backend status` result for firecracker (0 == the privileged
TAP pool + nft isolation table are provisioned). Skips cleanly with setup
instructions otherwise, so the suite runs on hosts without the pool.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bot_bottle.backend import BottleSpec, get_bottle_backend
from bot_bottle.backend.firecracker import FirecrackerBottleBackend
from bot_bottle.manifest import ManifestIndex


def _firecracker_status_ok() -> bool:
    """Gate on `./cli.py backend status --backend=firecracker`: a 0 exit
    means the pool + nft table are ready. Output is captured so the
    decorator stays quiet during collection; any error → not ready."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return FirecrackerBottleBackend.status() == 0
    except Exception:
        return False


_SKIP_MSG = (
    "firecracker backend not ready — provision the network pool with "
    "`./cli.py backend setup --backend=firecracker`, then confirm with "
    "`./cli.py backend status --backend=firecracker`"
)


def _minimal_agent_dockerfile(path: Path) -> None:
    path.write_text(
        "\n".join((
            "FROM node:22-slim",
            "RUN apt-get update \\",
            "  && apt-get install -y --no-install-recommends \\",
            "    ca-certificates curl git \\",
            "  && rm -rf /var/lib/apt/lists/*",
            "USER node",
            "WORKDIR /home/node",
            "CMD [\"sleep\", \"infinity\"]",
            "",
        )),
        encoding="utf-8",
    )


def _minimal_manifest(dockerfile: Path) -> ManifestIndex:
    return ManifestIndex.from_json_obj({
        "bottles": {
            "dev": {
                "agent_provider": {
                    "template": "pi",
                    "dockerfile": str(dockerfile),
                    "settings": {
                        "provider": "example",
                        "base_url": "https://example.com/v1",
                        "models": ["smoke"],
                    },
                },
                "egress": {"routes": [{"host": "example.com"}]},
            },
        },
        "agents": {
            "demo": {"skills": [], "prompt": "smoke", "bottle": "dev"},
        },
    })


@unittest.skipIf(
    os.environ.get("GITEA_ACTIONS") == "true",
    "skipped under act_runner: cannot host Firecracker microVMs",
)
@unittest.skipUnless(_firecracker_status_ok(), _SKIP_MSG)
class TestFirecrackerLaunch(unittest.TestCase):
    """Launch once, reuse the bottle across probes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stage = Path(tempfile.mkdtemp(prefix="cb-firecracker-launch."))
        cls._launch = None
        cls.bottle = None
        dockerfile = cls.stage / "Dockerfile.agent-smoke"
        _minimal_agent_dockerfile(dockerfile)
        os.environ["BOT_BOTTLE_BACKEND"] = "firecracker"
        try:
            backend = get_bottle_backend()
            spec = BottleSpec(
                manifest=_minimal_manifest(dockerfile),
                agent_name="demo",
                copy_cwd=False,
                user_cwd=str(cls.stage),
            )
            cls.plan = backend.prepare(spec, stage_dir=cls.stage)
            cls._launch = backend.launch(cls.plan)
            cls.bottle = cls._launch.__enter__()
        except BaseException:
            if cls._launch is not None:
                cls._launch.__exit__(None, None, None)
            shutil.rmtree(cls.stage, ignore_errors=True)
            os.environ.pop("BOT_BOTTLE_BACKEND", None)
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if cls._launch is not None:
                cls._launch.__exit__(None, None, None)
        finally:
            shutil.rmtree(cls.stage, ignore_errors=True)
            os.environ.pop("BOT_BOTTLE_BACKEND", None)

    def test_smoke_exec_echo(self) -> None:
        r = self.bottle.exec("echo hello-from-firecracker")  # type: ignore[union-attr]
        self.assertEqual(0, r.returncode, msg=r.stderr)
        self.assertIn("hello-from-firecracker", r.stdout)

    def test_proxy_env_points_at_sidecar(self) -> None:
        r = self.bottle.exec(  # type: ignore[union-attr]
            "printf '%s\\n' \"$HTTPS_PROXY\" \"$HTTP_PROXY\""
        )
        self.assertEqual(0, r.returncode, msg=r.stderr)
        self.assertIn("http", r.stdout.lower())


if __name__ == "__main__":
    unittest.main()

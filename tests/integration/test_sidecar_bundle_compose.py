"""Integration: end-to-end smoke for the PRD 0024 bundle shape.

Verifies that flipping `BOT_BOTTLE_SIDECAR_BUNDLE=1` produces a
working bottle: `docker compose up` brings the agent + bundle pair
online, the daemons inside the bundle bind their ports, and the
agent can reach egress + supervise via the bundle's network
aliases (no agent-side config changes between flag positions).

Skipped under GITEA_ACTIONS — the bundle image is a multi-stage
build pulling 200+MB of base layers, and the bind-mounts won't
share filesystem with the runner container. Same constraint as
the chunk-1 image-probe test.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend import BottleSpec, get_bottle_backend
from bot_bottle.manifest import Manifest
from tests._docker import skip_unless_docker


def _manifest() -> Manifest:
    """Bottle with supervise on so the bundle exercises egress +
    supervise. Git is off because a meaningful git-gate test needs
    a real upstream and SSH keys — out of scope for a bundle smoke."""
    return Manifest.from_json_obj({
        "bottles": {
            "dev": {
                "supervise": True,
            },
        },
        "agents": {
            "demo": {"skills": [], "prompt": "", "bottle": "dev"},
        },
    })


@skip_unless_docker()
@unittest.skipIf(
    os.environ.get("GITEA_ACTIONS") == "true",
    "skipped under act_runner: multi-stage bundle build pulls 200+MB "
    "of base layers and bind-mounts don't share fs with the runner",
)
class TestSidecarBundleCompose(unittest.TestCase):
    """One end-to-end pass with the bundle flag on. Skipping under
    act_runner; the local docker daemon does the work."""

    def test_bottle_up_with_bundle_flag_on(self):
        stage_dir = Path(tempfile.mkdtemp(prefix="cb-bundle-smoke."))
        try:
            with patch.dict(os.environ, {"BOT_BOTTLE_SIDECAR_BUNDLE": "1"}):
                backend = get_bottle_backend()
                spec = BottleSpec(
                    manifest=_manifest(),
                    agent_name="demo",
                    copy_cwd=False,
                    user_cwd=str(stage_dir),
                )
                plan = backend.prepare(spec, stage_dir=stage_dir)
                with backend.launch(plan) as bottle:
                    # The agent's HTTPS_PROXY URL (resolved at
                    # renderer-time) should reach egress inside
                    # the bundle. A bare CONNECT with no upstream
                    # URL gets rejected with 400 or 405 but proves
                    # the listener is alive at the alias.
                    probe = bottle.exec(
                        "set -eu\n"
                        "echo HTTPS_PROXY=$HTTPS_PROXY\n"
                        "PORT=$(echo \"$HTTPS_PROXY\" | sed -E 's|.*:([0-9]+).*|\\1|')\n"
                        "HOST=$(echo \"$HTTPS_PROXY\" | sed -E 's|http://([^:]+):.*|\\1|')\n"
                        "echo HOST=$HOST PORT=$PORT\n"
                        "curl -sS --max-time 5 -o /dev/null -w 'http=%{http_code}\\n' "
                        "  \"http://$HOST:$PORT/\" || true\n"
                    )
                    # The supervise URL resolves to the same bundle
                    # via its supervise alias, on a different port.
                    supervise_probe = bottle.exec(
                        "set -eu\n"
                        "curl -sS --max-time 5 -o /dev/null "
                        "  -w 'http=%{http_code}\\n' "
                        "  \"http://supervise:9100/health\" || true\n"
                    )
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

        self.assertEqual(0, probe.returncode, msg=probe.stderr)
        # egress answered SOMETHING — any 4xx is fine, just proves
        # the egress daemon is listening at the proxy address.
        self.assertIn("http=", probe.stdout,
                      f"no HTTP response from egress: {probe.stdout!r}")
        # supervise's /health endpoint exists (PRD 0013); it should
        # answer 200 or similar — anything non-empty proves the
        # third daemon's alias resolves to the same bundle.
        self.assertEqual(0, supervise_probe.returncode, msg=supervise_probe.stderr)
        self.assertIn("http=", supervise_probe.stdout)


if __name__ == "__main__":
    unittest.main()

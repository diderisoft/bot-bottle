"""Integration: a Node script run inside a launched bottle, hitting
a host outside the pipelock allowlist, is blocked.

End-to-end: drives `BottleBackend.prepare → launch` so the real
image build, network plumbing, and pipelock sidecar are all in the
loop. Inside the bottle, a Node script forms an HTTP forward-proxy
request (absolute-URI path) to `example.com` via `$HTTPS_PROXY`. The
fixture's effective allowlist contains only the baked-in defaults,
so pipelock must refuse to forward.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_bottle.backend import BottleSpec, get_bottle_backend
from tests._docker import skip_unless_docker
from tests.fixtures import fixture_minimal


# Node's stdlib http does not respect HTTPS_PROXY on its own; this
# script builds the forward-proxy request shape by hand so the test
# is asserting on pipelock's allowlist decision, not on whatever
# proxy-env auto-detection a Node release happens to ship.
#
# Output contract (parsed by the test):
#   - "status=<code>" when the proxy returns an HTTP response
#   - "error=<code> <message>" on a transport-level failure
#   - "timeout" on a hung request
_PROBE_JS = r"""
const http = require('http');
const proxy = new URL(process.env.HTTPS_PROXY);
const req = http.request({
  host: proxy.hostname,
  port: proxy.port,
  method: 'GET',
  path: 'http://example.com/',
  headers: { Host: 'example.com' },
}, (res) => {
  res.resume();
  res.on('end', () => {
    console.log('status=' + res.statusCode);
    process.exit(0);
  });
});
req.on('error', (e) => {
  console.log('error=' + (e.code || '') + ' ' + e.message);
  process.exit(0);
});
req.setTimeout(5000, () => {
  console.log('timeout');
  req.destroy();
});
req.end();
"""


@skip_unless_docker()
class TestPipelockBlocksNode(unittest.TestCase):
    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_node_request_to_blocked_host_is_rejected(self):
        backend = get_bottle_backend()
        stage_dir = Path(tempfile.mkdtemp(prefix="cb-test-stage."))
        try:
            spec = BottleSpec(
                manifest=fixture_minimal(),
                agent_name="demo",
                copy_cwd=False,
                user_cwd=str(stage_dir),
            )
            plan = backend.prepare(spec, stage_dir=stage_dir)
            with backend.launch(plan) as bottle:
                script = (
                    "set -e\n"
                    "cat > /tmp/probe.js <<'PROBE_EOF'\n"
                    f"{_PROBE_JS}\n"
                    "PROBE_EOF\n"
                    "node /tmp/probe.js\n"
                )
                result = bottle.exec(script)
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

        self.assertEqual(
            0, result.returncode,
            f"exec wrapper failed: stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        # The probe always prints exactly one signal line. If it
        # doesn't, the script failed in a way the test doesn't
        # understand and the surrounding assertions would be
        # ambiguous.
        self.assertTrue(
            "status=" in result.stdout or "error=" in result.stdout or "timeout" in result.stdout,
            f"probe produced no recognized output: {result.stdout!r}",
        )
        # The core invariant: example.com is NOT in fixture_minimal's
        # effective allowlist (only the baked-in defaults), so the
        # proxy must not have forwarded a successful response.
        self.assertNotIn(
            "status=200", result.stdout,
            "example.com is outside the allowlist; pipelock should not have forwarded a 200",
        )


if __name__ == "__main__":
    unittest.main()

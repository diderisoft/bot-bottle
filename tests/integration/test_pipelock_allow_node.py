"""Integration: a Node request to a host on pipelock's allowlist is
tunneled through.

End-to-end mirror of test_pipelock_block_node: drives `BottleBackend.
prepare → launch` so the real image build, network plumbing, and
pipelock sidecar are all in the loop. Inside the bottle, a Node
script issues an HTTPS CONNECT for raw.githubusercontent.com:443 —
a host in the baked-in default allowlist — through `$HTTPS_PROXY`.
Pipelock must answer 200 Connection Established. The 200 vs. 403
split on CONNECT is decided by pipelock itself (the remote never
sees the CONNECT verb), so it isolates the allowlist decision from
anything the remote might return.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bot_bottle.backend import BottleSpec, get_bottle_backend
from tests._docker import skip_unless_docker
from tests.fixtures import fixture_minimal


# Output contract (parsed by the test):
#   - "connect=<code>"  proxy upgraded to a tunnel (CONNECT success path)
#   - "status=<code>"   proxy answered without tunneling (block path)
#   - "error=<code> <message>"  transport-level failure
#   - "timeout"         request hung
_PROBE_JS = r"""
const http = require('http');
const proxy = new URL(process.env.HTTPS_PROXY);
const req = http.request({
  host: proxy.hostname,
  port: proxy.port,
  method: 'CONNECT',
  path: 'raw.githubusercontent.com:443',
});
req.on('connect', (res, socket) => {
  console.log('connect=' + res.statusCode);
  socket.destroy();
  process.exit(0);
});
req.on('response', (res) => {
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
class TestPipelockAllowsNode(unittest.TestCase):
    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_node_request_to_allowed_host_is_tunneled(self):
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
        # raw.githubusercontent.com IS in fixture_minimal's effective
        # allowlist (baked-in default). Pipelock must answer the CONNECT
        # with 200 Connection Established.
        self.assertIn(
            "connect=200", result.stdout,
            f"pipelock should have tunneled to raw.githubusercontent.com; got: {result.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()

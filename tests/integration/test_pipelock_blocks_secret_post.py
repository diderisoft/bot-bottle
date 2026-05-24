"""Integration: pipelock blocks a POST whose body carries a
recognized credential pattern, even when the host is on the
allowlist.

End-to-end companion to the block / allow node tests. The manifest
carries a literal env var whose value matches pipelock's DLP rules.
A Node script POSTs that value to an allowlisted host via plain
HTTP forward proxy (absolute-URI form) so pipelock can scan the
body — routing the same request over CONNECT would tunnel TLS
opaquely and the DLP layer would have nothing to see. The 403
return from pipelock isolates the body-scan layer as the active
control, distinct from the host-allowlist decision the other two
tests pin down.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_bottle.backend import BottleSpec, get_bottle_backend
from claude_bottle.manifest import Manifest
from tests._docker import skip_unless_docker


# Synthetic value shaped like a GitHub Personal Access Token
# (`ghp_` + 36 alnum chars). Not a real token; the only relevant
# property is that pipelock's default DLP rules recognize the
# shape. Kept obviously dummy so a stray grep can't mistake it
# for a real credential.
_FAKE_TOKEN = "ghp_aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX4yZ"


# Output contract (parsed by the test):
#   - "status=<code>"   proxy answered with an HTTP response
#   - "error=<code> <message>"  transport-level failure
#   - "timeout"         request hung
_PROBE_JS = r"""
const http = require('http');
const proxy = new URL(process.env.HTTPS_PROXY);
const body = 'token=' + process.env.FAKE_TOKEN;
const req = http.request({
  host: proxy.hostname,
  port: proxy.port,
  method: 'POST',
  // Absolute-URI form: pipelock acts as a plain HTTP forward proxy
  // and the body is visible to its DLP scanner. CONNECT would
  // tunnel TLS bytes that pipelock can't see into.
  path: 'http://api.anthropic.com/dlp-probe',
  headers: {
    Host: 'api.anthropic.com',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Content-Length': Buffer.byteLength(body),
  },
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
req.write(body);
req.end();
"""


@skip_unless_docker()
class TestPipelockBlocksSecretPost(unittest.TestCase):
    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_post_with_credential_body_is_blocked(self):
        manifest = Manifest.from_json_obj({
            "bottles": {
                "dev": {"env": {"FAKE_TOKEN": _FAKE_TOKEN}},
            },
            "agents": {
                "demo": {"skills": [], "prompt": "", "bottle": "dev"},
            },
        })
        backend = get_bottle_backend()
        stage_dir = Path(tempfile.mkdtemp(prefix="cb-test-stage."))
        try:
            spec = BottleSpec(
                manifest=manifest,
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
        # api.anthropic.com is on the baked-in allowlist, so the
        # host-allowlist layer would have let this through. Pipelock's
        # DLP body-scan layer must catch the credential pattern and
        # answer 403; any other code means the body reached the
        # upstream.
        self.assertIn(
            "status=403", result.stdout,
            f"pipelock DLP should have blocked the credential POST; got: {result.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()

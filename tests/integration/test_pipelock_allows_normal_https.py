"""Integration: with pipelock's tls_interception enabled (PRD 0006),
a clean HTTPS GET to an allowlisted host succeeds end-to-end through
the bumped tunnel.

Complement to test_pipelock_blocks_secret_https_post — together they
pin pipelock's two paths (block on body match, allow on clean
traffic). This test is also the implicit TLS-trust check: if
provision_ca had failed to install pipelock's CA into the agent's
trust store, curl would have rejected the bumped leaf cert and the
fetch would have failed before any HTTP response could come back."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from bot_bottle.backend import BottleSpec, get_bottle_backend
from tests._docker import skip_unless_docker
from tests.fixtures import fixture_minimal


# raw.githubusercontent.com is in the baked-in DEFAULT_ALLOWLIST.
# `git`'s own README on the master branch is a long-lived raw file
# (~3 KB) that any CI runner with internet can fetch.
_TARGET_URL = "https://raw.githubusercontent.com/git/git/master/README.md"


@skip_unless_docker()
class TestPipelockAllowsNormalHttps(unittest.TestCase):
    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_https_get_to_allowed_host_succeeds(self):
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
                    "set -eu\n"
                    'curl --proxy "$HTTPS_PROXY" -s --max-time 10 \\\n'
                    "  -w 'status=%{http_code}\\n' \\\n"
                    "  -o /tmp/probe-body.txt \\\n"
                    f"  {_TARGET_URL}\n"
                    'echo "len=$(wc -c < /tmp/probe-body.txt)"\n'
                )
                result = bottle.exec(script)
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

        self.assertEqual(
            0, result.returncode,
            f"exec wrapper failed: stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        # 200 from the upstream (pipelock forwarded after the body
        # scan passed). If curl had failed the bumped-cert trust
        # check, the exit code or status would be non-200 here.
        self.assertIn(
            "status=200", result.stdout,
            f"expected 200 from raw.githubusercontent.com; got: {result.stdout!r}",
        )
        # The git README is ~3 KB. Anything substantially non-zero
        # proves the response body actually transferred — i.e. the
        # CONNECT tunnel + bumped TLS + body forwarding all worked.
        self.assertNotIn(
            "len=0\n", result.stdout,
            f"response body was empty: {result.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()

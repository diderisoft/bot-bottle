"""Integration: pipelock's `tls_interception.passthrough_domains`
exempts api.anthropic.com from MITM, so request bodies that would
otherwise trip the body-scan layer (notably the BIP-39 seed-phrase
detector firing on user-authored Claude conversation text) are not
inspected and the request reaches Anthropic's TLS endpoint.

Probe: POST the canonical zero-entropy 12-word BIP-39 mnemonic
(`abandon` × 11 + `about`) — checksum-valid by construction — to
`https://api.anthropic.com/v1/messages`. Without the passthrough,
pipelock returns a 403 `blocked: request body contains secret:
BIP-39 Seed Phrase`. With it, pipelock relays the CONNECT opaquely
and the upstream replies with whatever it likes (401/4xx from
Anthropic for an unauthenticated junk POST). We assert that the
verdict is NOT pipelock's block.
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


# Canonical BIP-39 12-word test mnemonic. Valid SHA-256 checksum —
# pipelock's seed-phrase scanner (default `verify_checksum: true`)
# fires on this exact string if it ever sees the cleartext body.
_BIP39_PHRASE = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


@skip_unless_docker()
class TestPipelockLlmPassthrough(unittest.TestCase):
    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_bip39_body_to_anthropic_is_not_blocked(self):
        manifest = Manifest.from_json_obj({
            "bottles": {
                "dev": {"env": {"SEED": _BIP39_PHRASE}},
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
                    "set -eu\n"
                    'curl --proxy "$HTTPS_PROXY" -s --max-time 10 \\\n'
                    "  -w 'status=%{http_code}\\n' \\\n"
                    "  -o /tmp/probe-body.txt \\\n"
                    '  -X POST -H "content-type: application/json" \\\n'
                    '  --data "{\\"phrase\\": \\"$SEED\\"}" \\\n'
                    "  https://api.anthropic.com/v1/messages\n"
                    'echo "body=$(head -c 200 /tmp/probe-body.txt)"\n'
                )
                result = bottle.exec(script)
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

        self.assertEqual(
            0, result.returncode,
            f"exec wrapper failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}",
        )
        # The pipelock block verdict starts with `blocked: ` in the
        # body. Anything else (auth error, 401, 4xx from Anthropic) is
        # an acceptable outcome — it means the body was NOT inspected
        # by the proxy and the request was relayed to the upstream
        # TLS endpoint.
        self.assertNotIn(
            "body=blocked: ", result.stdout,
            f"unexpected pipelock body-scan block on api.anthropic.com; "
            f"expected passthrough to skip MITM. got: {result.stdout!r}",
        )
        self.assertNotIn(
            "BIP-39", result.stdout,
            f"BIP-39 verdict should never appear for api.anthropic.com "
            f"requests under tls_interception.passthrough_domains; "
            f"got: {result.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()

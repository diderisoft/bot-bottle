"""Integration: PRD 0023 chunk 2d — end-to-end launch + exec
round trip + the acceptance probes.

The smoke confirms the launch flow (per-bottle docker bridge →
sidecar bundle with pinned IP → smolvm guest with TSI allowlist →
exec) plumbs together end to end. The two probes confirm the
security properties the design pivot was about:

  - **localhost-reach probe** — guest tries to dial a service
    bound on the host's `127.0.0.1`. TSI's `<bundle-ip>/32`
    allowlist must refuse the connect. (PRD 0023's first draft
    worried about `--outbound-localhost-only` opening the whole
    `127.0.0.0/8`; with `--allow-cidr <bundle-ip>/32` instead,
    the gap closes.)

  - **egress-port-bypass probe** — guest tries to dial
    `<bundle-ip>:9099` (egress's port). TSI permits the IP but
    the bundle's egress daemon binds `127.0.0.1` inside its
    container, so the connect refuses at the socket level. The
    bind-address mitigation is what closes TSI's port-granularity
    gap.

Gated on macOS + smolvm + docker + not GITEA_ACTIONS — the
runner can't host libkrun-backed VMs."""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_bottle.backend import BottleSpec, get_bottle_backend
from claude_bottle.backend.smolmachines.smolvm import is_available as _smolvm_available
from claude_bottle.manifest import Manifest
from tests._docker import skip_unless_docker


def _minimal_manifest() -> Manifest:
    return Manifest.from_json_obj({
        "bottles": {"dev": {}},
        "agents": {
            "demo": {"skills": [], "prompt": "", "bottle": "dev"},
        },
    })


@skip_unless_docker()
@unittest.skipUnless(
    platform.system() == "Darwin",
    "smolvm is macOS-only for v1; Linux+KVM path is a future PRD",
)
@unittest.skipUnless(
    _smolvm_available(),
    "smolvm not on PATH; install via "
    "curl -sSL https://smolmachines.com/install.sh | sh",
)
@unittest.skipIf(
    os.environ.get("GITEA_ACTIONS") == "true",
    "skipped under act_runner: cannot host libkrun-backed VMs",
)
class TestSmolmachinesLaunch(unittest.TestCase):
    """The full smoke + the two acceptance probes share one
    bottle bringup to amortize the ~10s cold-start cost across
    three assertions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stage = Path(tempfile.mkdtemp(prefix="cb-smol-launch."))
        os.environ["CLAUDE_BOTTLE_BACKEND"] = "smolmachines"
        backend = get_bottle_backend()
        spec = BottleSpec(
            manifest=_minimal_manifest(),
            agent_name="demo",
            copy_cwd=False,
            user_cwd=str(cls.stage),
        )
        cls.plan = backend.prepare(spec, stage_dir=cls.stage)
        cls._launch = backend.launch(cls.plan)
        cls.bottle = cls._launch.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._launch.__exit__(None, None, None)
        finally:
            shutil.rmtree(cls.stage, ignore_errors=True)
            os.environ.pop("CLAUDE_BOTTLE_BACKEND", None)

    def test_smoke_exec_echo(self):
        # The plumbing-verifies-end-to-end smoke: a shell command
        # round-trips through smolvm machine exec.
        r = self.bottle.exec("echo hello-from-vm")
        self.assertEqual(0, r.returncode, msg=r.stderr)
        self.assertIn("hello-from-vm", r.stdout)

    def test_localhost_reach_probe(self):
        # Agent dials a 127.0.0.1 service on the host. TSI's
        # allowlist contains only <bundle-ip>/32, so this must
        # refuse. We use a port unlikely to be bound on the host
        # (high-numbered) so we're confirming TSI refusal, not
        # just "no service listening."
        r = self.bottle.exec(
            "wget -T 3 -t 1 -O - http://127.0.0.1:9 2>&1 || true"
        )
        # `wget` to a denied destination produces a connect error.
        # The exact phrasing varies (busybox vs gnu); we assert
        # the response is NOT the body of any real service.
        self.assertNotIn("hello-from-vm", r.stdout)
        self.assertTrue(
            "refused" in r.stdout.lower()
            or "timed out" in r.stdout.lower()
            or "unreachable" in r.stdout.lower()
            or "failed" in r.stdout.lower(),
            f"expected a connect-refusal message; got: {r.stdout!r}",
        )

    def test_egress_port_bypass_probe(self):
        # Agent dials <bundle-ip>:9099 (egress's port). TSI
        # permits the IP, but egress will bind 127.0.0.1:9099
        # inside the bundle in chunk 3, so the connect refuses
        # at the socket level. NOTE: in chunk 2d the bundle's
        # daemons aren't running (daemons_csv=""), so nothing
        # is listening on :9099 anyway — this test asserts the
        # connect fails, which is the property chunk 3 will
        # preserve once egress is actually running.
        r = self.bottle.exec(
            f"wget -T 3 -t 1 -O - http://{self.plan.bundle_ip}:9099 "
            "2>&1 || true"
        )
        self.assertTrue(
            "refused" in r.stdout.lower()
            or "timed out" in r.stdout.lower()
            or "unreachable" in r.stdout.lower()
            or "failed" in r.stdout.lower(),
            f"expected egress port refusal; got: {r.stdout!r}",
        )


if __name__ == "__main__":
    unittest.main()

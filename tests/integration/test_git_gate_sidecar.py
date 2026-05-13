"""Integration: per-agent git-gate sidecar (PRD 0008).

Two tests against a real Docker daemon:

  1. ls-remote against a gate whose upstream is unreachable fails
     with the access-hook's fail-closed rejection. Proves the
     daemon is bound to its port AND the access-hook is wired:
     a working ls-remote against the gate is necessarily a working
     ls-remote against the upstream (PRD 0008's transparent-mirror
     contract).
  2. A push containing a gitleaks-detectable secret is rejected
     by the pre-receive hook with a non-zero exit on the agent
     side and a gitleaks-rejection line in the response. The PRD's
     primary success criterion.

A successful round-trip (clone through gate reflects upstream)
needs a reachable upstream SSH host; deferred to a follow-up.
"""

import dataclasses
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_bottle.backend.docker.git_gate import (
    DockerGitGate,
    build_git_gate_image,
)
from claude_bottle.backend.docker.network import (
    network_create_egress,
    network_create_internal,
    network_remove,
)
from claude_bottle.manifest import Manifest
from tests._docker import skip_unless_docker

# The official gitleaks image already has git + alpine; reusing it
# for the client side too saves a separate image pull.
CLIENT_IMAGE = "zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"

# Synthetic high-entropy AKIA-shaped string; gitleaks's aws-access-token
# rule fires on this with the default config. AWS's own example
# ("AKIAIOSFODNN7EXAMPLE") is NOT flagged by gitleaks v8.x — entropy
# filter rejects it — so we use a distinct random-looking value.
FAKE_AWS_KEY = "AKIAQRJHK7N5ZPM2VXTL"


@skip_unless_docker()
class TestGitGateSidecar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Pre-pull the client/gitleaks base so per-test runs aren't
        # racing the registry. Skip cleanly on pull failure (a real
        # outage is out of scope here).
        result = subprocess.run(
            ["docker", "pull", CLIENT_IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode != 0:
            raise unittest.SkipTest(f"could not pull {CLIENT_IMAGE}")
        # Build the gate image once for the class. Layer cache makes
        # repeated runs cheap.
        build_git_gate_image()

    def setUp(self):
        # DNS hostnames on user-defined Docker networks max out at 63
        # chars per label (RFC 1035). The full container name is
        # `claude-bottle-git-gate-<slug>` = 23 + len(slug), so the slug
        # has to stay under ~40 to be resolvable. Keep it short.
        suffix = self.id().rsplit('.', 1)[-1].replace('_', '-')[-12:]
        self.slug = f"t{os.getpid()}-{suffix}"
        self.gate_name = ""
        self.internal_net = ""
        self.egress_net = ""
        self.work_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        if self.gate_name:
            DockerGitGate().stop(self.gate_name)
        for n in (self.internal_net, self.egress_net):
            if n:
                network_remove(n)
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _start_gate(self, name: str = "foo") -> str:
        """Build a one-upstream gate and bring it up. Returns the
        container name (== git-gate hostname on the internal net)."""
        # Contents of the fake key don't matter for these tests — the
        # rejection-path hook never reaches phase 2 where it would be
        # used, and ls-remote doesn't push.
        fake_key = self.work_dir / "fake-key"
        fake_key.write_text("not-a-real-key\n")

        manifest = Manifest.from_json_obj({
            "bottles": {
                "dev": {
                    "git": [{
                        "Name": name,
                        "Upstream": "ssh://git@upstream.invalid/path.git",
                        "IdentityFile": str(fake_key),
                        "KnownHostKey": "ssh-ed25519 AAAAEXAMPLE",
                    }],
                },
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        bottle = manifest.bottles["dev"]

        gate = DockerGitGate()
        prep = gate.prepare(bottle, self.slug, self.work_dir)

        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)
        plan = dataclasses.replace(
            prep,
            internal_network=self.internal_net,
            egress_network=self.egress_net,
        )
        self.gate_name = gate.start(plan)
        return self.gate_name

    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_ls_remote_fails_closed_when_upstream_unreachable(self):
        """The gate's access-hook runs `git fetch origin --prune` before
        every upload-pack. With the fixture's deliberately unreachable
        `ssh://git@upstream.invalid/...`, that fetch fails and the
        hook exits 1; the daemon reports access-denied. Asserting
        non-zero here is what proves the access-hook is wired: under
        the v1 (push-only) design ls-remote against a fresh gate
        returned exit 0 with no refs."""
        gate = self._start_gate("foo")
        # Daemon still has to bind first; retry the TCP connect a few
        # times. The expected end state is a non-zero exit from the
        # daemon's access-denied response — not a connection refused.
        probe = subprocess.run(
            ["docker", "run", "--rm",
             "--network", self.internal_net,
             "--entrypoint", "sh",
             CLIENT_IMAGE,
             "-c",
             f"for i in $(seq 1 15); do "
             f"  out=$(git ls-remote git://{gate}/foo.git 2>&1) && exit 99;"
             f"  case \"$out\" in *'access denied'*|*'not exported'*) "
             f"    echo \"$out\"; exit 1;; esac;"
             f"  sleep 1;"
             f"done;"
             f"echo TIMEOUT; exit 2"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        # exit 1: daemon access-denied as expected. exit 99 would mean
        # ls-remote actually succeeded against the unreachable upstream
        # (impossible — would indicate stale-data serving, the very
        # thing the access-hook is meant to prevent).
        self.assertEqual(
            1, probe.returncode,
            f"expected fail-closed access-denied; got "
            f"exit={probe.returncode} stdout={probe.stdout!r} "
            f"stderr={probe.stderr!r}",
        )

    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_push_with_secret_is_rejected(self):
        """The PRD 0008 success criterion: a push containing a
        gitleaks-detectable secret is rejected; the hook's "gitleaks
        rejected" line appears in the response, and git push exits
        non-zero on the client side."""
        gate = self._start_gate("foo")
        push_script = (
            "set -e\n"
            "cd /tmp\n"
            # Wait for git daemon to bind. Under the v1.1 design,
            # ls-remote never returns 0 against an unreachable
            # upstream (access-hook fail-closed), so we wait for *any*
            # response (the daemon's access-denied line) as the
            # readiness signal.
            f"for i in $(seq 1 15); do "
            f"  out=$(git ls-remote git://{gate}/foo.git 2>&1) || true;"
            f"  case \"$out\" in *'remote error'*|*'access denied'*) break;; esac;"
            f"  sleep 1;"
            f"done\n"
            "git init -q -b main repo\n"
            "cd repo\n"
            "git config user.email test@example.com\n"
            "git config user.name test\n"
            f"echo '{FAKE_AWS_KEY}' > leak.txt\n"
            "git add leak.txt\n"
            "git commit -q -m leak\n"
            f"git push git://{gate}/foo.git main 2>&1\n"
        )
        result = subprocess.run(
            ["docker", "run", "--rm",
             "--network", self.internal_net,
             "--entrypoint", "sh",
             CLIENT_IMAGE,
             "-c", push_script],
            capture_output=True, text=True, timeout=120, check=False,
        )
        combined = result.stdout + result.stderr
        self.assertNotEqual(
            0, result.returncode,
            f"expected push to fail; output={combined!r}",
        )
        # Hook's stderr is delivered to the client via the `remote:`
        # prefix during a git push. Either token is enough to prove
        # the pre-receive hook ran and rejected the push.
        self.assertTrue(
            "gitleaks rejected" in combined or "leaks found" in combined,
            f"expected a gitleaks rejection in the response; got: {combined!r}",
        )


if __name__ == "__main__":
    unittest.main()

"""Unit: egress_entrypoint.sh argv construction (PRD 0023 chunk 3).

The egress entrypoint is a small POSIX-sh script that builds
the mitmdump argv from env vars. A VM backend (e.g. firecracker)
controls egress's bind address via EGRESS_LISTEN_HOST; the
docker backend leaves it unset and gets mitmdump's default
(all interfaces).

We can't easily unit-test a shell script as Python, but we can
run it under `sh -x` with mitmdump stubbed to print its argv,
which is exactly what these tests do."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


_SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "bot_bottle" / "egress_entrypoint.sh"
)


def _run_entrypoint(env: dict[str, str]) -> str:
    """Run egress_entrypoint.sh with a stubbed mitmdump that
    prints its argv. Returns the argv as a single string.

    The script uses `exec mitmdump ...`. We shim by prepending a
    fake `mitmdump` to PATH; the shim writes its argv to stdout
    and exits 0."""
    with tempfile.TemporaryDirectory() as tmp:
        shim_dir = Path(tmp)
        shim = shim_dir / "mitmdump"
        shim.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$@"\n'
        )
        shim.chmod(0o755)
        run_env = {
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            # Resolver-only egress (PRD 0070): the entrypoint fails closed
            # without an orchestrator URL, so it's a precondition for reaching
            # the argv construction these tests assert on. Individual tests may
            # override it (e.g. to exercise the fail-closed guard).
            "BOT_BOTTLE_ORCHESTRATOR_URL": "http://orchestrator:9000",
            # cat needs to find ca-certificates.crt for the
            # trust-bundle branch; we don't test that path here.
            **env,
        }
        result = subprocess.run(
            ["sh", str(_SCRIPT)],
            capture_output=True, text=True, env=run_env,
            check=True,
        )
        return result.stdout


class TestEgressEntrypointArgv(unittest.TestCase):
    def test_default_mode_regular_no_listen_host(self):
        # No env set: --mode regular@9099 + no --listen-host.
        argv = _run_entrypoint({})
        self.assertIn("--mode\nregular@9099", argv)
        self.assertNotIn("--listen-host", argv)
        # Confdir always present.
        self.assertIn(
            "--set\nconfdir=/home/mitmproxy/.mitmproxy",
            argv,
        )

    def test_listen_host_127_0_0_1_emits_flag(self):
        # A VM backend sets EGRESS_LISTEN_HOST=127.0.0.1
        # to scope egress to localhost inside the bundle.
        argv = _run_entrypoint({"EGRESS_LISTEN_HOST": "127.0.0.1"})
        self.assertIn("--listen-host\n127.0.0.1", argv)

    def test_listen_host_unset_emits_no_flag(self):
        # Docker backend leaves it unset and gets mitmdump's
        # default bind address (all interfaces).
        argv = _run_entrypoint({"EGRESS_LISTEN_HOST": ""})
        self.assertNotIn("--listen-host", argv)

    def test_upstream_mode_combined_with_listen_host(self):
        # VM-backend mode also sets EGRESS_UPSTREAM_PROXY so
        # both flags should compose correctly.
        argv = _run_entrypoint({
            "EGRESS_UPSTREAM_PROXY": "http://192.168.50.2:8888",
            "EGRESS_LISTEN_HOST": "127.0.0.1",
        })
        self.assertIn("--mode\nupstream:http://192.168.50.2:8888", argv)
        self.assertIn("--listen-port\n9099", argv)
        self.assertIn("--listen-host\n127.0.0.1", argv)

    def test_addon_always_loaded(self):
        argv = _run_entrypoint({})
        self.assertIn("-s\n/app/egress_addon.py", argv)

    def test_missing_orchestrator_url_fails_closed(self):
        # Resolver-only egress (PRD 0070): with no policy source the entrypoint
        # must refuse to launch mitmdump rather than come up as a bare
        # TLS-bumping open proxy. Exits nonzero before any argv is emitted.
        result = subprocess.run(
            ["sh", str(_SCRIPT)],
            capture_output=True, text=True, check=False,
            env={"PATH": os.environ["PATH"], "BOT_BOTTLE_ORCHESTRATOR_URL": ""},
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("BOT_BOTTLE_ORCHESTRATOR_URL is required", result.stderr)


if __name__ == "__main__":
    unittest.main()

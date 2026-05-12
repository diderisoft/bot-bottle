"""Integration: cli.py start --dry-run --format=json renders a stable
machine-readable plan and creates zero Docker resources. The shape of
the JSON document is part of the CLI's user-facing contract."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._docker import skip_unless_docker

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@skip_unless_docker()
class TestDryRunPlan(unittest.TestCase):
    def test_dry_run_emits_structured_plan(self):
        work_dir = Path(tempfile.mkdtemp())
        try:
            manifest = work_dir / "claude-bottle.json"
            manifest.write_text(json.dumps({
                "bottles": {"dev": {"egress": {"allowlist": ["example.org"]}}},
                "agents": {
                    "demo": {"skills": [], "prompt": "", "bottle": "dev"},
                },
            }))

            # Under act_runner with a host-mounted docker socket, the
            # `docker network ls` / `docker ps -a` calls from inside the
            # job container exit non-zero (see docs/ci.md for the same
            # topology issue affecting other integration tests). Skip
            # the side-effects guard there; locally the check still
            # catches accidental docker resource creation by the dry
            # run.
            check_side_effects = os.environ.get("GITEA_ACTIONS") != "true"
            nets_before = self._count_claude_bottle_networks() if check_side_effects else 0
            ctrs_before = self._count_claude_bottle_containers() if check_side_effects else 0

            env = os.environ.copy()
            env["HOME"] = str(work_dir)
            env.pop("CLAUDE_BOTTLE_DRY_RUN", None)
            # The HOME override above isolates the manifest under test
            # from the dev's real ~/claude-bottle.json. On Docker Desktop
            # that same override breaks docker CLI endpoint resolution,
            # since the active context lives in $HOME/.docker/config.json
            # and the per-user socket sits under $HOME/.docker/run/.
            # Pin DOCKER_HOST to the parent's resolved endpoint so the
            # subprocess reaches the same daemon regardless of $HOME.
            endpoint = subprocess.run(
                ["docker", "context", "inspect",
                 "--format", "{{.Endpoints.docker.Host}}"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            if endpoint:
                env["DOCKER_HOST"] = endpoint
            result = subprocess.run(
                [
                    sys.executable, str(REPO_ROOT / "cli.py"),
                    "start", "--dry-run", "--format", "json", "demo",
                ],
                cwd=work_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                0, result.returncode,
                f"start --dry-run failed: stderr={result.stderr}",
            )

            plan = json.loads(result.stdout)

            self.assertEqual("demo", plan["agent"])
            self.assertEqual("dev", plan["bottle"])
            self.assertEqual("runc", plan["runtime"],
                             "runsc isn't available on the CI runner")
            self.assertEqual([], plan["skills"])
            self.assertEqual([], plan["ssh_hosts"])
            self.assertEqual([], plan["ssh_gate"])
            self.assertEqual(False, plan["remote_control"])
            self.assertEqual(0, plan["prompt"]["length"])

            # User-declared host + a baked default both present; the union
            # is sorted and deduplicated.
            hosts = plan["egress"]["hosts"]
            self.assertIn("example.org", hosts)
            self.assertIn("api.anthropic.com", hosts)
            self.assertEqual(plan["egress"]["host_count"], len(hosts))
            self.assertEqual(sorted(set(hosts)), hosts,
                             "hosts must be sorted and deduplicated")

            # PRD 0006: TLS interception is on for every launched
            # bottle. Fingerprint is null at dry-run (no CA exists
            # yet); real launches log it from provision_ca.
            self.assertEqual(
                {"enabled": True, "ca_fingerprint": None},
                plan["egress"]["tls_interception"],
            )

            # No Docker side effects (see the GITEA_ACTIONS skip note
            # above — this guard runs locally only).
            if check_side_effects:
                self.assertEqual(nets_before, self._count_claude_bottle_networks(),
                                 "no networks created")
                self.assertEqual(ctrs_before, self._count_claude_bottle_containers(),
                                 "no containers created")
        finally:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)

    def _count_claude_bottle_networks(self) -> int:
        return self._count_with_prefix(
            ["docker", "network", "ls", "--format", "{{.Name}}"], "claude-bottle"
        )

    def _count_claude_bottle_containers(self) -> int:
        return self._count_with_prefix(
            ["docker", "ps", "-a", "--format", "{{.Names}}"], "claude-bottle"
        )

    def _count_with_prefix(self, cmd: list[str], prefix: str) -> int:
        # capture_output + explicit returncode check so a docker
        # failure surfaces its stderr in the test report instead of
        # the bare CalledProcessError we used to get.
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            self.fail(
                f"{' '.join(cmd)!r} failed (exit {result.returncode}): "
                f"stderr={result.stderr.strip()!r}"
            )
        return sum(1 for n in result.stdout.splitlines() if n.startswith(prefix))


if __name__ == "__main__":
    unittest.main()

"""Integration: the git-gate is a bidirectional mirror of its
upstream (PRD 0008 v1.1).

Three round-trip assertions against a real Docker daemon plus a
sibling sshd container playing the role of "real upstream":

  1. clone-through-gate returns whatever the upstream has at the
     moment of clone (refs + content).
  2. After a second commit lands on the upstream out-of-band, a
     fetch through the gate picks it up — the access-hook is
     refreshing before each upload-pack.
  3. A push through the gate (clean commit) lands on the upstream's
     bare repo — the pre-receive hook's forward phase works.

These are the user-facing semantics: every operation against the
gate is observably equivalent to the same operation against the
real upstream.
"""

import dataclasses
import os
import shutil
import subprocess
import tempfile
import textwrap
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


# Same image used by test_git_gate_sidecar — alpine + git + gitleaks.
CLIENT_IMAGE = "zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"

# Built once in setUpClass via `docker build -` from the inline
# Dockerfile below. Carries openssh-server, a `git` user, baked-in
# host keys, and a bare repo at /git/foo.git seeded with one commit.
UPSTREAM_IMAGE = "claude-bottle-test-upstream:latest"

UPSTREAM_DOCKERFILE = textwrap.dedent("""
    FROM alpine:3.20
    RUN apk add --no-cache openssh-server git
    RUN adduser -D -s /usr/bin/git-shell git && \\
        passwd -u git && \\
        mkdir -p /home/git/.ssh && \\
        chown git:git /home/git/.ssh && \\
        chmod 700 /home/git/.ssh && \\
        mkdir -p /git && \\
        chown git:git /git
    # Bake host keys into the image so the test can pin the
    # KnownHostKey value before the container starts. Re-running
    # ssh-keygen -A at boot would invalidate that pinning.
    RUN ssh-keygen -A
    USER git
    RUN git config --global init.defaultBranch main && \\
        git config --global user.email upstream@example && \\
        git config --global user.name upstream && \\
        git init --bare /git/foo.git && \\
        git clone /git/foo.git /tmp/w && \\
        cd /tmp/w && \\
        echo "initial upstream content" > README.md && \\
        git add README.md && \\
        git commit -q -m "initial commit" && \\
        git push -q origin main && \\
        rm -rf /tmp/w
    USER root
    RUN echo "PermitRootLogin no" >> /etc/ssh/sshd_config && \\
        echo "PasswordAuthentication no" >> /etc/ssh/sshd_config && \\
        echo "AuthorizedKeysFile /home/git/.ssh/authorized_keys" >> /etc/ssh/sshd_config
    CMD ["/usr/sbin/sshd", "-D", "-e"]
""").strip()


@skip_unless_docker()
class TestGitGateBidirectionalMirror(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Pull the client image first (other suites do the same — keeps
        # registry races contained to setUpClass).
        if subprocess.run(
            ["docker", "pull", CLIENT_IMAGE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode != 0:
            raise unittest.SkipTest(f"could not pull {CLIENT_IMAGE}")

        # Build the upstream sshd image from stdin (no build context
        # needed — Dockerfile has no COPY/ADD).
        build_result = subprocess.run(
            ["docker", "build", "-t", UPSTREAM_IMAGE, "-"],
            input=UPSTREAM_DOCKERFILE,
            text=True,
            capture_output=True,
            check=False,
        )
        if build_result.returncode != 0:
            raise unittest.SkipTest(
                f"could not build upstream image: {build_result.stderr}"
            )

        # Pull the upstream's baked-in ed25519 host pubkey out of the
        # image so we can pin it as KnownHostKey on the gate's manifest
        # entry. Reading from a transient container ensures we get the
        # same key the running sshd will present.
        pub_result = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "cat",
             UPSTREAM_IMAGE, "/etc/ssh/ssh_host_ed25519_key.pub"],
            capture_output=True, text=True, check=True,
        )
        parts = pub_result.stdout.strip().split()
        # Format: "ssh-ed25519 <base64-pubkey> <comment>" — drop comment.
        cls.upstream_host_key = f"{parts[0]} {parts[1]}"

        # Build the gate image (uses build cache after the first run).
        build_git_gate_image()

    def setUp(self):
        suffix = self.id().rsplit('.', 1)[-1].replace('_', '-')[-12:]
        self.slug = f"t{os.getpid()}-{suffix}"
        self.gate_name = ""
        self.upstream_name = f"claude-bottle-test-upstream-{self.slug}"
        self.internal_net = ""
        self.egress_net = ""
        self.work_dir = Path(tempfile.mkdtemp())

        # Per-test SSH auth keypair. The host gets the private key
        # path on disk (manifest IdentityFile); the upstream's
        # authorized_keys gets the public key, docker-cp'd in just
        # before sshd starts.
        self.auth_key = self.work_dir / "auth_key"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(self.auth_key),
             "-C", "git-gate-test"],
            check=True, stdout=subprocess.DEVNULL,
        )
        self.auth_pub = self.work_dir / "auth_key.pub"

        # Networks first so the upstream can attach to the egress
        # network at create time.
        self.internal_net = network_create_internal(self.slug)
        self.egress_net = network_create_egress(self.slug)

        # Start the upstream sshd container, attached to the egress
        # network (which the gate also lives on). Container name doubles
        # as its DNS-resolvable hostname.
        subprocess.run(
            ["docker", "create",
             "--name", self.upstream_name,
             "--network", self.egress_net,
             UPSTREAM_IMAGE],
            check=True, stdout=subprocess.DEVNULL,
        )
        # docker cp the per-test pubkey into the upstream as
        # /home/git/.ssh/authorized_keys (right user, right path).
        subprocess.run(
            ["docker", "cp", str(self.auth_pub),
             f"{self.upstream_name}:/home/git/.ssh/authorized_keys"],
            check=True, stdout=subprocess.DEVNULL,
        )
        # chown / chmod the authorized_keys before sshd refuses to
        # use it.
        for argv in (
            ["chown", "git:git", "/home/git/.ssh/authorized_keys"],
            ["chmod", "600", "/home/git/.ssh/authorized_keys"],
        ):
            subprocess.run(
                ["docker", "exec", "-u", "0", self.upstream_name, *argv],
                check=False, stdout=subprocess.DEVNULL,
            )
        # The exec-then-start ordering is unusual — exec on a stopped
        # container is OK on modern docker but if it errors we just
        # do the chown after start instead. Retry post-start to be
        # safe.
        subprocess.run(
            ["docker", "start", self.upstream_name],
            check=True, stdout=subprocess.DEVNULL,
        )
        for argv in (
            ["chown", "git:git", "/home/git/.ssh/authorized_keys"],
            ["chmod", "600", "/home/git/.ssh/authorized_keys"],
        ):
            subprocess.run(
                ["docker", "exec", "-u", "0", self.upstream_name, *argv],
                check=False, stdout=subprocess.DEVNULL,
            )
        # Wait for sshd to bind; a short retry against TCP 22 is enough.
        ready = False
        for _ in range(30):
            probe = subprocess.run(
                ["docker", "exec", self.upstream_name,
                 "sh", "-c", "nc -z 127.0.0.1 22"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            if probe.returncode == 0:
                ready = True
                break
            subprocess.run(["sleep", "0.2"], check=False)
        if not ready:
            self.fail("upstream sshd never bound port 22")

        # Build the gate plan + start it. Upstream URL points at the
        # upstream container's hostname (Docker DNS resolves it on the
        # egress network) on port 22, user `git`.
        manifest = Manifest.from_json_obj({
            "bottles": {
                "dev": {
                    "git": [{
                        "Name": "foo",
                        "Upstream": f"ssh://git@{self.upstream_name}/git/foo.git",
                        "IdentityFile": str(self.auth_key),
                        "KnownHostKey": self.upstream_host_key,
                    }],
                },
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        bottle = manifest.bottles["dev"]
        gate = DockerGitGate()
        prep = gate.prepare(bottle, self.slug, self.work_dir)
        plan = dataclasses.replace(
            prep,
            internal_network=self.internal_net,
            egress_network=self.egress_net,
        )
        self.gate_name = gate.start(plan)

    def tearDown(self):
        if self.gate_name:
            DockerGitGate().stop(self.gate_name)
        if self.upstream_name:
            subprocess.run(
                ["docker", "rm", "-f", self.upstream_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        for n in (self.internal_net, self.egress_net):
            if n:
                network_remove(n)
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _upstream_main_sha(self) -> str:
        """Read upstream's current refs/heads/main sha by exec'ing
        directly into the upstream container's bare repo."""
        out = subprocess.run(
            ["docker", "exec", "-u", "git", self.upstream_name,
             "git", "-C", "/git/foo.git", "rev-parse", "refs/heads/main"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()

    def _push_to_upstream_oob(self, message: str) -> str:
        """Make a new commit directly on the upstream's bare repo
        (out-of-band, not through the gate). Returns the new sha."""
        script = textwrap.dedent(f"""
            set -e
            cd /tmp
            rm -rf w
            git clone /git/foo.git w
            cd w
            git config user.email upstream@example
            git config user.name upstream
            echo "$RANDOM-$$" >> README.md
            git add README.md
            git commit -q -m "{message}"
            git push -q origin main
            git rev-parse HEAD
        """).strip()
        out = subprocess.run(
            ["docker", "exec", "-u", "git", self.upstream_name,
             "sh", "-c", script],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip().splitlines()[-1]

    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_clone_and_refetch_reflect_upstream(self):
        """Clone via gate returns upstream's commit. After a second
        commit lands on the upstream out-of-band, a re-fetch through
        the gate picks it up — the access-hook is refreshing before
        each upload-pack."""
        initial_sha = self._upstream_main_sha()

        # Clone via gate.
        clone_script = (
            f"set -e\n"
            f"cd /tmp && git clone -q git://{self.gate_name}/foo.git r\n"
            f"git -C r rev-parse refs/remotes/origin/main\n"
            f"cat r/README.md\n"
        )
        clone = subprocess.run(
            ["docker", "run", "--rm",
             "--network", self.internal_net,
             "--entrypoint", "sh",
             CLIENT_IMAGE,
             "-c", clone_script],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(
            0, clone.returncode,
            f"clone via gate failed: stdout={clone.stdout!r} "
            f"stderr={clone.stderr!r}",
        )
        cloned_sha = clone.stdout.strip().splitlines()[0]
        self.assertEqual(
            initial_sha, cloned_sha,
            "clone via gate must return the upstream's current sha",
        )
        self.assertIn("initial upstream content", clone.stdout)

        # Out-of-band commit on the upstream.
        new_sha = self._push_to_upstream_oob("second commit")
        self.assertNotEqual(initial_sha, new_sha)

        # ls-remote via gate (re-fetch should pick up the new sha).
        ls = subprocess.run(
            ["docker", "run", "--rm",
             "--network", self.internal_net,
             "--entrypoint", "sh",
             CLIENT_IMAGE,
             "-c", f"git ls-remote git://{self.gate_name}/foo.git refs/heads/main"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(0, ls.returncode, f"ls-remote failed: {ls.stderr!r}")
        gate_sha = ls.stdout.split()[0]
        self.assertEqual(
            new_sha, gate_sha,
            "ls-remote via gate must reflect the upstream's out-of-band update; "
            "if this assertion fails, the access-hook is not refreshing on every "
            "upload-pack and the gate is serving stale data",
        )

    @unittest.skipIf(
        os.environ.get("GITEA_ACTIONS") == "true",
        "skipped under act_runner: docker socket mount topology breaks "
        "in-process visibility of networks created on the host daemon",
    )
    def test_push_through_gate_lands_on_upstream(self):
        """A clean (no-gitleaks-hit) push through the gate lands on
        the upstream's bare repo — pre-receive phase 2 forwards
        the accepted refs."""
        # Make a commit through the gate. The script clones via gate
        # (so the commit will be a child of upstream's current main).
        push_script = textwrap.dedent(f"""
            set -e
            cd /tmp
            git clone -q git://{self.gate_name}/foo.git r
            cd r
            git config user.email client@example
            git config user.name client
            echo "client-side commit" > NEW.md
            git add NEW.md
            git commit -q -m "client commit"
            git rev-parse HEAD
            git push origin main 2>&1
        """).strip()
        push = subprocess.run(
            ["docker", "run", "--rm",
             "--network", self.internal_net,
             "--entrypoint", "sh",
             CLIENT_IMAGE,
             "-c", push_script],
            capture_output=True, text=True, timeout=120, check=False,
        )
        self.assertEqual(
            0, push.returncode,
            f"push via gate failed: stdout={push.stdout!r} "
            f"stderr={push.stderr!r}",
        )
        client_sha = push.stdout.splitlines()[0].strip()
        self.assertEqual(
            client_sha, self._upstream_main_sha(),
            "push via gate must land on upstream's bare repo; "
            "if this fails the pre-receive forward phase is broken or the "
            "upstream credential is misconfigured",
        )


if __name__ == "__main__":
    unittest.main()

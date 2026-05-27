"""SmolmachinesBottle — running-instance handle (PRD 0023 chunk 2d).

Routes `exec_claude` / `exec` / `cp_in` through `smolvm machine
exec` / `smolvm machine cp`. The handle is yielded by `launch`
and torn down via the surrounding ExitStack on context exit;
`close` is a no-op idempotent alias so the BottleBackend ABC's
context-manager contract is satisfied.

User context: `smolvm machine exec` runs commands as root in the
VM, but the agent image's USER is `node` and claude-code refuses
to run as root with `--dangerously-skip-permissions`. Both
`exec_claude` and `exec` wrap commands in `runuser -l node -c`
so they execute as the node user (and pick up node's $HOME /
$USER from the login shell) — matches the docker backend's
default-USER behavior."""

from __future__ import annotations

import shlex
import subprocess

from .. import Bottle, ExecResult
from . import smolvm as _smolvm


class SmolmachinesBottle(Bottle):
    """Handle returned by `SmolmachinesBottleBackend.launch`. The
    underlying VM lifecycle (create / start / stop / delete) lives
    on the launch ExitStack — this class only routes runtime
    operations to the right `smolvm machine ...` subcommand."""

    def __init__(self, machine_name: str, *, prompt_path: str | None = None) -> None:
        self.name = machine_name
        # In-VM path to the agent's prompt file. None when the
        # agent declared no prompt (file still exists; we just
        # don't pass --append-system-prompt-file).
        self._prompt_path = prompt_path

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int:
        """Run `claude` interactively inside the VM as the `node`
        user. Inherits the operator's terminal (stdin / stdout /
        stderr) so the session feels native. Blocks until claude
        exits; returns the in-VM exit code.

        We bypass the captured-output `machine_exec` helper here
        because that one wraps stdout/stderr in pipes — fine for
        scripted exec, wrong for an interactive shell. Drop down
        to `subprocess.run` with the TTY inherited.

        `runuser -l node -c` runs the inner command under node's
        login shell so $HOME / $USER are set. Without that switch
        claude bails on `--dangerously-skip-permissions cannot be
        used with root/sudo privileges`."""
        flags = ["smolvm", "machine", "exec", "--name", self.name]
        if tty:
            flags += ["-i", "-t"]
        claude_argv = ["claude"]
        if self._prompt_path:
            claude_argv += ["--append-system-prompt-file", self._prompt_path]
        claude_argv += argv
        # shlex-quote each piece so flags / paths with shell-special
        # chars survive the runuser -c shell parse.
        inner = " ".join(shlex.quote(p) for p in claude_argv)
        flags += ["--", "runuser", "-l", "node", "-c", f"exec {inner}"]
        result = subprocess.run(flags, check=False)
        return result.returncode

    def exec(self, script: str) -> ExecResult:
        """Run a POSIX shell script as the `node` user and capture
        the result. Matches the docker backend's `exec`, which
        defaults to the image's USER (also node) — so test
        helpers / provision shell-outs run with the same identity
        on both backends."""
        r = _smolvm.machine_exec(
            self.name,
            ["runuser", "-l", "node", "-c", script],
        )
        return ExecResult(
            returncode=r.returncode,
            stdout=r.stdout,
            stderr=r.stderr,
        )

    def cp_in(self, host_path: str, container_path: str) -> None:
        """Copy a host path into the guest at `container_path`."""
        _smolvm.machine_cp(host_path, f"{self.name}:{container_path}")

    def close(self) -> None:
        # Real teardown lives on the launch ExitStack; this is just
        # the idempotent alias the BottleBackend ABC expects.
        pass

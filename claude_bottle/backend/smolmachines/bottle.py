"""SmolmachinesBottle — running-instance handle (PRD 0023 chunk 2d).

Routes `exec_claude` / `exec` / `cp_in` through `smolvm machine
exec` / `smolvm machine cp`. The handle is yielded by `launch`
and torn down via the surrounding ExitStack on context exit;
`close` is a no-op idempotent alias so the BottleBackend ABC's
context-manager contract is satisfied."""

from __future__ import annotations

import subprocess
import sys

from .. import Bottle, ExecResult
from . import smolvm as _smolvm


class SmolmachinesBottle(Bottle):
    """Handle returned by `SmolmachinesBottleBackend.launch`. The
    underlying VM lifecycle (create / start / stop / delete) lives
    on the launch ExitStack — this class only routes runtime
    operations to the right `smolvm machine ...` subcommand."""

    def __init__(self, machine_name: str) -> None:
        self.name = machine_name

    def exec_claude(self, argv: list[str], *, tty: bool = True) -> int:
        """Run `claude` interactively inside the VM. Inherits the
        operator's terminal (stdin / stdout / stderr) so the
        session feels native. Blocks until claude exits; returns
        the in-VM exit code.

        We bypass the captured-output `machine_exec` helper here
        because that one wraps stdout/stderr in pipes — fine for
        scripted exec, wrong for an interactive shell. Drop down
        to `subprocess.run` with the TTY inherited."""
        flags = ["smolvm", "machine", "exec", "--name", self.name]
        if tty:
            flags += ["-i", "-t"]
        flags += ["--", "claude", *argv]
        result = subprocess.run(flags, check=False)
        return result.returncode

    def exec(self, script: str) -> ExecResult:
        """Run a POSIX shell script and capture the result. The
        script runs under `/bin/sh -c`, matching what the docker
        backend's `exec` does — callers can write shell-y test
        helpers without worrying about argv splitting."""
        r = _smolvm.machine_exec(
            self.name,
            ["/bin/sh", "-c", script],
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

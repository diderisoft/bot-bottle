"""Thin subprocess wrapper around the `smolvm` CLI (PRD 0023).

One thin Python function per smolvm subcommand the launch flow
needs. Two design choices worth flagging:

  - **No daemon, no SDK.** smolvm 0.8.0 ships a `smolvm serve`
    HTTP API as the long-term-clean integration target. The
    project's stdlib-first ethos + the lower-overhead CLI calls
    push v1 to shell out via `subprocess.run`. If a future
    smolvm release makes `serve` mandatory (or significantly
    faster), revisit.

  - **Two return shapes.** `SmolvmRunResult` (returncode + stdout
    + stderr captured) is returned by `machine_exec` because the
    caller cares about the in-VM command's exit status, and by
    test helpers that introspect output. The other calls
    (`machine_start`, `machine_stop`, `pack_create`, etc.) raise
    `SmolvmError` on non-zero exit — failure to start a VM is
    fatal to the launch flow, not something callers want to
    branch on.

The wrapper is unit-tested with `subprocess.run` mocked; the
integration smoke test (chunk 2d) exercises against a real
smolvm binary."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence



_SMOLVM = "smolvm"


@dataclass(frozen=True)
class SmolvmRunResult:
    """Captured result of an in-VM command. Mirrors the structure
    `Bottle.exec` returns so callers can hand it straight through."""
    returncode: int
    stdout: str
    stderr: str


class SmolvmError(RuntimeError):
    """Raised when a smolvm subprocess returns non-zero on a path
    where the caller has no useful branch to take (start failed,
    pack failed, etc.). Carries the captured stderr for the
    operator-facing log line."""

    def __init__(self, argv: Sequence[str], result: subprocess.CompletedProcess[str]):
        self.argv = list(argv)
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr
        cmd = " ".join(self.argv)
        super().__init__(
            f"{cmd!r} failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def _smolvm(*args: str, env: Mapping[str, str] | None = None,
            check: bool = True) -> subprocess.CompletedProcess[str]:
    """One subprocess call into the smolvm CLI. `check=True`
    raises SmolvmError on non-zero; `check=False` returns the
    CompletedProcess for the caller to inspect."""
    argv = [_SMOLVM, *args]
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=dict(env) if env is not None else None,
        check=False,
    )
    if check and result.returncode != 0:
        raise SmolvmError(argv, result)
    return result


# --- Pack ----------------------------------------------------------------


def pack_create(image: str, output: Path) -> None:
    """`smolvm pack create --image <image> -o <output>`. Converts
    an OCI image into a self-contained `.smolmachine` artifact
    smolvm can boot via `machine create --from`. Idempotent on the
    smolvm side — re-running with the same image+output rebuilds
    from layer cache."""
    _smolvm("pack", "create", "--image", image, "-o", str(output))


def pack_create_from_vm(name: str, output: Path) -> None:
    """`smolvm pack create --from-vm <name> -o <output>`.

    Snapshots an existing persistent VM into a pack artifact. As
    with `pack_create`, smolvm writes a launcher at `output` and the
    bootable sidecar at `output.smolmachine`.
    """
    _smolvm("pack", "create", "--from-vm", name, "-o", str(output))


# --- Machine lifecycle ---------------------------------------------------


def machine_create(
    name: str,
    *,
    image: str | None = None,
    from_path: Path | None = None,
    allow_cidrs: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> None:
    """`smolvm machine create NAME [--image IMG | --from PATH]
    [--allow-cidr CIDR ...] [-e K=V ...]`. NAME is positional
    (the CLI's exception to the `--name` pattern other
    subcommands use).

    `image` (registry ref like `alpine:latest`) and `from_path`
    (a `.smolmachine` artifact) are mutually exclusive — one or
    the other tells smolvm what to boot. The wrapper doesn't
    enforce exclusivity; smolvm errors clearly enough.

    `allow_cidrs` and `env` are passed as CLI flags instead of a
    Smolfile because `--from` and `--smolfile` are themselves
    mutually exclusive in smolvm 0.8.0 — and we want `--from`'s
    no-pull-at-start property. The flag form gives the same
    result without the Smolfile complication.

    `--net` is sent explicitly when `allow_cidrs` is non-empty.
    smolvm 0.8.0's docs say `--allow-cidr` implies `--net`, but
    empirically the implication only fires when no `--from` is
    set — `--from PATH --allow-cidr X/32` silently produces a
    machine with `network: false` and no routes in the guest, so
    the agent can't reach the bundle's pinned IP."""
    args: list[str] = ["machine", "create"]
    if image is not None:
        args += ["--image", image]
    if from_path is not None:
        args += ["--from", str(from_path)]
    if allow_cidrs:
        args.append("--net")
    for cidr in allow_cidrs:
        args += ["--allow-cidr", cidr]
    if env:
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
    args.append(name)
    _smolvm(*args)


def machine_is_running(name: str) -> bool:
    """Return True if the named VM is in the 'running' state."""
    result = _smolvm("machine", "ls", "--json", check=False)
    if result.returncode != 0:
        return False
    try:
        machines = json.loads(result.stdout or "[]")
    except ValueError:
        return False
    return any(
        isinstance(m, dict) and m.get("name") == name and m.get("state") == "running"
        for m in machines
    )


def machine_start(name: str) -> None:
    """`smolvm machine start --name NAME`."""
    _smolvm("machine", "start", "--name", name)


def machine_stop(name: str) -> None:
    """`smolvm machine stop --name NAME`. Idempotent against
    already-stopped machines: smolvm prints a notice and exits 0
    in that case, so no special handling here."""
    _smolvm("machine", "stop", "--name", name)


def machine_delete(name: str) -> None:
    """`smolvm machine delete -f NAME`. NAME is positional. `-f`
    skips the interactive confirmation — required for
    non-interactive teardown."""
    _smolvm("machine", "delete", "-f", name)


def machine_exec(
    name: str,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    workdir: str | None = None,
    timeout: str | None = None,
) -> SmolvmRunResult:
    """`smolvm machine exec --name NAME [-w DIR] [--timeout DUR]
    [-e K=V ...] -- ARGV...`. Returns the captured result rather
    than raising — callers (including `Bottle.exec`) care about
    the in-VM command's exit code, not just whether smolvm ran.

    `env` here is in-VM env vars (`-e K=V`), not the host
    subprocess env — smolvm's own argv carries them through the
    VMM."""
    flags: list[str] = ["machine", "exec", "--name", name]
    if workdir is not None:
        flags += ["-w", workdir]
    if timeout is not None:
        flags += ["--timeout", timeout]
    if env:
        for k, v in env.items():
            flags += ["-e", f"{k}={v}"]
    # `--` separator before the command. smolvm's CLI requires it
    # so its own flag parser doesn't grab argv items that look
    # like flags.
    flags.append("--")
    flags += list(argv)
    result = _smolvm(*flags, check=False)
    return SmolvmRunResult(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def wait_exec_ready(name: str, *, timeout: float = 5.0) -> None:
    """Poll `machine exec true` until exit 0 or `timeout` elapses.

    Replaces `time.sleep(1.5)` after `machine_start`: libkrun's exec
    channel needs a brief warm-up before back-to-back exec calls are
    safe. Polling exits as soon as the channel is ready and fails
    loudly if the VM never responds."""
    deadline = time.monotonic() + timeout
    delay = 0.1
    while time.monotonic() < deadline:
        r = machine_exec(name, ["true"])
        if r.returncode == 0:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.5)
    argv = ["smolvm", "machine", "exec", "--name", name, "--", "true"]
    raise SmolvmError(
        argv,
        subprocess.CompletedProcess(
            args=argv, returncode=-1, stdout="",
            stderr=f"exec channel not ready after {timeout:.0f}s — VM may have failed to boot.",
        ),
    )


def machine_cp(src: str, dst: str) -> None:
    """`smolvm machine cp SRC DST`. Path syntax: `machine:path` to
    reference a path inside the VM, bare path for the host. Both
    SRC and DST are positional; either side can be machine: or
    bare. Empty path is a no-op (returns immediately without
    invoking smolvm)."""
    if not src or not dst:
        return
    _smolvm("machine", "cp", src, dst)


# --- Discovery -----------------------------------------------------------


def is_available() -> bool:
    """True iff `smolvm` is on PATH. Used by the integration test
    suite's skip-guards."""
    return shutil.which(_SMOLVM) is not None

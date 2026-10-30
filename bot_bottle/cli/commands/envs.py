"""envs: the environments on this host (`BOT_BOTTLE_ENV`).

  bot-bottle envs                         list them
  bot-bottle envs rm NAME [--force] [--dry-run]

`live` is the default install (`~/.bot-bottle`). A named environment (`dev`,
`ci-<run>`) has its own state root, resource names and firecracker pool
slice, and registers itself the first time a command runs in it.

`rm NAME` tears NAME down: the bottles its host controller holds, each
available backend's leftover bottles, orchestrator, gateway, containers,
networks and volumes (images are shared caches and stay), its host controller
and autostart unit. Then it deletes NAME's state root and firecracker run
directory and frees its pool slice. If any teardown step fails the state is
kept, so it can be retried; `--force` deletes it anyway. `live` can't be
removed this way (`bot-bottle uninstall` removes the install).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ... import environment
from ...environment import ENV_VAR, LIVE, InvalidEnvironmentError
from ..constants import PROG

# The hidden subcommand `rm` runs inside the environment it removes: names are
# fixed per process, so the teardown has to run as that environment.
TEARDOWN = "_teardown"
# The directory holding the `bot_bottle` package this CLI runs.
_PACKAGE_PARENT = Path(environment.__file__).resolve().parents[1]


@dataclass(frozen=True)
class EnvRow:
    """One environment, as `envs` lists it."""

    name: str
    root: str
    slot: int
    active: bool
    hostctl: bool
    infra: tuple[str, ...] = ()


@dataclass(frozen=True)
class Probe:
    """What `envs` asks the host, injectable for tests: whether a host
    controller answers from a state root, the running container names (docker
    and macOS), and whether an environment's firecracker infra VMs run."""

    hostctl: Callable[[Path], bool]
    containers: Callable[[], Sequence[str]]
    firecracker_infra: Callable[[str], bool]


def _firecracker_cache() -> Path:
    # Not `paths.host_firecracker_cache_dir`, which creates it.
    return Path(os.environ.get("BOT_BOTTLE_FC_CACHE")
                or Path.home() / ".cache" / "bot-bottle" / "firecracker")


def firecracker_env_dir(name: str) -> Path:
    """`name`'s firecracker run and infra directory (`util.env_dir`)."""
    if name == LIVE:
        return _firecracker_cache()
    return _firecracker_cache() / "envs" / name


def _hostctl_running(root: Path) -> bool:
    # pylint: disable-next=import-outside-toplevel
    from ...control.hostctl import runner
    state = runner.read_state(root / "hostctl")
    return state is not None and runner.probe(state)


def _lines(argv: list[str]) -> list[str]:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return out.stdout.split() if out.returncode == 0 else []


def _running_containers() -> list[str]:
    names: list[str] = []
    if shutil.which("docker"):
        names += _lines(["docker", "ps", "--format", "{{.Names}}"])
    if shutil.which("container"):
        names += _lines(["container", "list", "--quiet"])
    return names


def _firecracker_infra_running(name: str) -> bool:
    infra = firecracker_env_dir(name) / "infra"
    for role in ("orchestrator", "gateway"):
        try:
            pid = int((infra / role / "vm.pid").read_text().strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            continue
        return True
    return False


HOST_PROBE = Probe(hostctl=_hostctl_running, containers=_running_containers,
                   firecracker_infra=_firecracker_infra_running)


def collect(probe: Probe = HOST_PROBE, *, home: Path | None = None,
            environ: Mapping[str, str] | None = None) -> list[EnvRow]:
    """Live, then every registered environment, with what of each runs."""
    env = os.environ if environ is None else environ
    active = environment.active_name(env)
    live_root = (Path(env["BOT_BOTTLE_ROOT"]) if active == LIVE and env.get("BOT_BOTTLE_ROOT")
                 else environment.default_root(LIVE, home))
    entries = [(LIVE, str(live_root), 0)]
    entries += [(r.name, r.root, r.slot) for r in environment.registered(home)]
    containers = list(probe.containers())
    rows = []
    for name, root, slot in entries:
        prefix = environment.resource_prefix(name)
        infra = []
        count = sum(1 for c in containers if c.startswith(prefix))
        if count:
            infra.append(f"{count} container{'s' if count != 1 else ''}")
        if probe.firecracker_infra(name):
            infra.append("firecracker VMs")
        rows.append(EnvRow(name=name, root=root, slot=slot, active=name == active,
                           hostctl=probe.hostctl(Path(root)), infra=tuple(infra)))
    return rows


@dataclass(frozen=True)
class RmPlan:
    """What `envs rm` does: run `teardown` with `env` (as the environment),
    then delete `remove` and forget the registration. `kept` says what it
    leaves and why."""

    name: str
    root: Path
    teardown: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    remove: tuple[Path, ...]
    kept: tuple[str, ...] = ()


def plan_rm(name: str, *, home: Path | None = None, python: str = sys.executable,
            environ: Mapping[str, str] | None = None) -> RmPlan:
    """The plan for removing `name`. Raises InvalidEnvironmentError for a bad
    name or `live`."""
    environment.validate(name)
    if name == LIVE:
        raise InvalidEnvironmentError(
            f"live is the default install and can't be removed with `envs rm`; "
            f"`{PROG} uninstall` removes bot-bottle")
    base = os.environ if environ is None else environ
    reg = environment.read(name, home)
    own_dir = environment.envs_dir(home) / name
    root = Path(reg.root) if reg is not None else own_dir
    env = dict(base)
    env[ENV_VAR] = name
    env["BOT_BOTTLE_ROOT"] = str(root)
    # Import this same package (`-P` keeps the caller's directory off the path,
    # #706), installed or run from a checkout.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_PACKAGE_PARENT), base.get("PYTHONPATH", "")) if p)
    remove: list[Path] = [own_dir]
    kept: list[str] = []
    live_root = environment.default_root(LIVE, home)
    if _same(root, own_dir) or _inside(root, own_dir):
        pass
    elif _same(root, live_root) or _inside(live_root, root):
        kept.append(f"{root}: it holds the live install")
    else:
        kept.append(f"{root}: set with BOT_BOTTLE_ROOT, outside "
                    f"{environment.envs_dir(home)}; delete it yourself")
    remove.append(firecracker_env_dir(name) if home is None
                  else home / ".cache" / "bot-bottle" / "firecracker" / "envs" / name)
    teardown = (python, "-P", "-m", "bot_bottle.cli", "envs", TEARDOWN)
    return RmPlan(name=name, root=root, teardown=teardown, env=env,
                  remove=tuple(remove), kept=tuple(kept))


def _same(a: Path, b: Path) -> bool:
    return a.resolve() == b.resolve()


def _inside(child: Path, parent: Path) -> bool:
    return child.resolve().is_relative_to(parent.resolve())


def _print_rows(rows: list[EnvRow]) -> None:
    # pylint: disable-next=import-outside-toplevel
    from ..table import print_table
    print_table(
        ("", "NAME", "ROOT", "SLICE", "HOST CONTROLLER", "INFRA"),
        [("*" if r.active else "", r.name, r.root,
          "0" if r.name == LIVE else (str(r.slot) if r.slot else ""),
          "running" if r.hostctl else "stopped", ", ".join(r.infra))
         for r in rows])


def cmd_envs(argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        sys.stderr.write(__doc__ or "")
        return 0
    if not argv or argv[0] in ("ls", "list"):
        _print_rows(collect())
        return 0
    if argv[0] == "rm":
        return _cmd_rm(argv[1:])
    if argv[0] == TEARDOWN:
        return teardown_active()
    sys.stderr.write(f"{PROG} envs: unknown subcommand {argv[0]!r}\n\n{__doc__}")
    return 2


def _cmd_rm(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{PROG} envs rm",
                                     description="Tear down an environment and delete its state.")
    parser.add_argument("name")
    parser.add_argument("--force", action="store_true",
                        help="delete the state even if part of the teardown failed")
    parser.add_argument("--dry-run", action="store_true", help="show what it would do")
    ns = parser.parse_args(argv)
    try:
        plan = plan_rm(ns.name)
    except InvalidEnvironmentError as e:
        sys.stderr.write(f"{PROG} envs rm: {e}\n")
        return 2
    if ns.dry_run:
        print(f"tear down {plan.name} ({ENV_VAR}={plan.name} BOT_BOTTLE_ROOT={plan.root})")
        for path in plan.remove:
            print(f"delete {path}")
        for note in plan.kept:
            print(f"keep {note}")
        return 0
    return execute_rm(plan, force=ns.force)


def execute_rm(plan: RmPlan, *, force: bool = False,
               run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run) -> int:
    """Run `plan`: the teardown, then (when it worked, or `force`) the
    deletes. 0 when everything went."""
    code = run(list(plan.teardown), env=plan.env, check=False).returncode
    if code != 0 and not force:
        sys.stderr.write(f"{PROG} envs rm: tearing down {plan.name} failed; its state is "
                         f"kept so you can retry, or delete it anyway with --force\n")
        return 1
    for path in plan.remove:
        shutil.rmtree(path, ignore_errors=True)
    environment.unregister(plan.name)
    for note in plan.kept:
        sys.stderr.write(f"{PROG} envs rm: kept {note}\n")
    print(f"{plan.name}: removed")
    return code


def teardown_active() -> int:  # pylint: disable=too-many-branches
    """Tear the active (named) environment down: held bottles, each available
    backend's bottles, infra and resources (not images), then the host
    controller and its autostart unit. Run by `envs rm` as that environment."""
    # pylint: disable=import-outside-toplevel
    from ...backend import EnumerationError, get_bottle_backend, has_backend, known_backend_names
    from ...backend.cleanup_control import CleanupError
    from ...control.hostctl import runner, service_unit
    from ..settings import settings_from_env
    from .cleanup import _teardown_held  # pylint: disable=protected-access

    name = environment.active_name()
    if name == LIVE:
        sys.stderr.write(f"{PROG} envs: refusing to tear down live\n")
        return 2
    failures: list[str] = []
    state = runner.running()
    if state is not None:
        failures += _teardown_held(runner.held_bottles(state))
    settings = settings_from_env()
    for backend_name in known_backend_names():
        if not has_backend(settings, backend_name):
            continue
        backend = get_bottle_backend(settings, backend_name, prompt=False)
        try:
            plan = backend.prepare_cleanup()
            if not plan.empty:
                backend.cleanup(plan)
        except (CleanupError, EnumerationError) as e:
            failures.append(f"{backend_name} bottles: {e}")
        try:
            backend.infra_service().stop()
        except Exception as e:  # pylint: disable=broad-exception-caught
            failures.append(f"{backend_name} orchestrator and gateway: {e}")
        for resource in backend.host_resources(purge=False):
            # Images are shared by every environment.
            if not resource.what.startswith("image ") and not resource.remove():
                failures.append(f"{backend_name}: couldn't remove {resource.what}")
    try:
        runner.stop(force=True)
        service_unit.uninstall()
    except runner.HostControlError as e:
        failures.append(f"host controller: {e}")
    for failure in failures:
        sys.stderr.write(f"{PROG} envs: {failure}\n")
    return 1 if failures else 0


__all__ = ["EnvRow", "Probe", "RmPlan", "cmd_envs", "collect", "execute_rm",
           "firecracker_env_dir", "plan_rm", "teardown_active"]

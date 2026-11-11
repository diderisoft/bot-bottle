"""doctor: check host prerequisites. Fails only without a new-enough Python, a
ready backend, or tmux; the config directory is advisory.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ...agent_provider import get_provider
from ...backend import (
    backend_os_requirement, docker_isolation_summary, is_backend_ready, known_backend_names,
)
from ...contract.yaml_subset import YamlSubsetError, parse_frontmatter
from ...control.client import (
    OrchestratorClient, OrchestratorEndpoint, no_orchestrator_message, recorded_orchestrator,
    stray_orchestrators, stray_removal_hint,
)
from ...environment import ENV_VAR, LIVE, active_name, root_owned_fix, root_owned_state
from .. import openrouter
from ...control.hostctl import runner
from ...control.hostctl.build import RESTART_NEEDED
from ...control.hostctl.sessions import MIN_TMUX_VERSION, tmux_version
from ...paths import bot_bottle_root
from ..constants import PROG
from ..settings import settings_from_env

MIN_PYTHON = (3, 11)


def _ok(label: str, detail: str) -> None:
    print(f"ok:   {label}: {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"warn: {label}: {detail}")


def _fail(label: str, detail: str) -> None:
    print(f"fail: {label}: {detail}")


def _note(label: str, detail: str) -> None:
    print(f"note: {label}: {detail}")


def _check_python() -> bool:
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= MIN_PYTHON:
        _ok("python", detail)
        return True
    _fail("python", f"{detail}; need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer")
    return False


def selected_backend(settings) -> str:  # type: ignore[no-untyped-def]
    """The backend a launch here would use (BOT_BOTTLE_BACKEND, else what's
    detected); "" when none can be picked."""
    # pylint: disable-next=import-outside-toplevel
    from ...backend.selection import auto_backend_name
    with contextlib.suppress(Exception):
        return auto_backend_name(settings)
    return ""


def _probe(settings, name: str) -> tuple[bool, list[str]]:  # type: ignore[no-untyped-def]
    """Whether `name` is ready, and the diagnostics its status() printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        ready = is_backend_ready(settings, name, quiet=False)
    return ready, [line for line in out.getvalue().splitlines() if line.strip()]


def _diagnostics(lines: list[str]) -> None:
    for line in lines:
        print(f"      {line}")


@dataclass
class BackendFindings:
    """What doctor found about backends, for the exit code and `next:`."""

    selected: str = ""
    selected_ready: bool = False
    any_ready: bool = False


def _check_backends(verbose: bool = False) -> BackendFindings:
    """The backend a launch would use is checked in full: its diagnostics show
    when it isn't ready (always with `verbose`). Others get a line, never the
    advice to install them. With no backend selectable, any ready one does."""
    settings = settings_from_env()
    found = BackendFindings(selected=selected_backend(settings))
    ready: list[str] = []
    for name in known_backend_names():
        needs = backend_os_requirement(name)
        if needs:
            _note("backend", f"{name}: skipped ({needs} only)")
            continue
        ok, lines = _probe(settings, name)
        if ok:
            ready.append(name)
        if not found.selected or name == found.selected:
            if ok:
                _ok("backend", f"{name}: ready")
            else:
                _warn("backend", f"{name}: not ready")
            if verbose or not ok:
                _diagnostics(lines)
            continue
        state = "ready" if ok else "not ready"
        _note("backend", f"{name}: {state} (not used here; BOT_BOTTLE_BACKEND={name} "
                         f"to use it)")
        if verbose:
            _diagnostics(lines)
    found.any_ready = bool(ready)
    found.selected_ready = found.selected in ready
    if "docker" in ready and (found.selected == "docker" or ready == ["docker"]):
        _note("backend", docker_isolation_summary())
    if (found.selected_ready if found.selected else found.any_ready):
        return found
    if found.selected:
        _fail("backend", f"{found.selected} isn't ready to run a bottle (what's missing is "
                         f"above); `{PROG} install --backend={found.selected}` sets it up")
    else:
        _fail("backend", "no backend is ready to run a bottle; start Docker, or finish "
                         "Apple Container (macOS) / Firecracker (Linux) setup")
    return found


def _check_tmux() -> bool:
    """Agent sessions run in tmux; 3.1 added `window-size latest`."""
    version = tmux_version()
    if version is None:
        _fail("tmux", "not installed; install tmux 3.1 or newer (brew/apt/nix install tmux)")
        return False
    detail = f"{version[0]}.{version[1]}"
    if version < MIN_TMUX_VERSION:
        _fail("tmux", f"{detail}; need {MIN_TMUX_VERSION[0]}.{MIN_TMUX_VERSION[1]} or newer")
        return False
    _ok("tmux", detail)
    return True


def environment_line() -> str:
    """The active environment and its state root, for doctor and status."""
    name = active_name()
    how = "the default" if name == LIVE else f"{ENV_VAR}={name}"
    return f"{name} ({how}; state in {bot_bottle_root()})"


@dataclass(frozen=True)
class OrchestratorReport:
    """The environment's orchestrator and any other listening, for doctor
    and status."""

    recorded: OrchestratorEndpoint | None
    healthy: bool
    strays: tuple[OrchestratorEndpoint, ...]

    def headline(self) -> str:
        if self.recorded is None:
            return no_orchestrator_message()
        where = f"{self.recorded.backend} at {self.recorded.url}"
        return (f"{where} (environment {active_name()})" if self.healthy
                else f"{where} isn't answering; run `{PROG} up`")

    def warnings(self) -> list[str]:
        name = active_name()
        if self.recorded is None:
            return [f"a {s.backend} orchestrator answers at {s.url}, but none is recorded "
                    f"for environment {name}; `{PROG} up --backend {s.backend}` adopts it"
                    for s in self.strays]
        return [f"another bot-bottle orchestrator ({s.backend}) answers at {s.url}; "
                f"it isn't environment {name}'s, so nothing here uses it. {stray_removal_hint(s)}"
                for s in self.strays]


def orchestrator_report(timeout: float = 2.0) -> OrchestratorReport:
    """Whether the environment's recorded orchestrator answers, and which
    other bot-bottle orchestrators are listening."""
    recorded = recorded_orchestrator()
    healthy = recorded is not None and OrchestratorClient(recorded.url, timeout=timeout).health()
    strays = stray_orchestrators(settings_from_env(), timeout=timeout)
    return OrchestratorReport(recorded, healthy, tuple(strays))


def _check_orchestrator() -> None:
    report = orchestrator_report()
    (_ok if report.healthy else _warn)("orchestrator", report.headline())
    for warning in report.warnings():
        _warn("orchestrator", warning)


def _check_host_controller() -> None:
    """Advisory: a host controller started before an upgrade refuses launches."""
    state = runner.running()
    if state is None:
        _note("host controller", f"not running (`{PROG} up` starts it)")
    elif runner.restart_needed(state):
        _warn("host controller", RESTART_NEEDED)
    else:
        _ok("host controller", f"running at {state.url} (build {state.build_id})")


def _repair_command() -> str:
    """`bot-bottle pair` again, for a host the console refused, or "" when
    it wasn't refused (or no host controller runs the link)."""
    # pylint: disable-next=import-outside-toplevel
    from ..host_state_client import running_pairing
    found = None
    with contextlib.suppress(Exception):
        found = running_pairing()
    if found is None or not found[1].get("unpaired"):
        return ""
    url = found[0].url.rstrip("/")
    return f"`{PROG} pair --console-url {url}`" if url else f"`{PROG} pair`"


def _check_console_link() -> None:
    """Advisory: the console no longer knows this host (account deleted)."""
    if _repair_command():
        _warn("console link", "the console refused this host: it doesn't know it anymore "
                              "(its account was deleted, or the host was removed)")


def _check_config_dir() -> None:
    config = bot_bottle_root()
    stale = root_owned_state()
    if stale:
        _warn("config", f"{len(stale)} path(s) owned by root (left by a run as root); "
                        f"run: {root_owned_fix(stale)}")
    elif config.is_dir():
        _ok("config", str(config))
    else:
        _warn("config", f"{config} does not exist yet (created on first use)")


def _check_providers() -> None:
    """What `setup` would find for each provider and whether its starter
    exists. Advisory; never writes."""
    env = dict(os.environ)
    manifests = bot_bottle_root()
    missing: list[str] = []
    for name in ("claude", "codex", "pi"):
        detection = get_provider(name).detect_host(env)
        written = (manifests / "agents" / f"{name}.md").exists()
        if name != "pi" and not detection.installed:
            continue
        if name != "pi" and not detection.logged_in:
            _warn(name, f"installed but not logged in; run `{detection.login_command}`")
        elif written:
            _ok(name, f"starter in {manifests} ({detection.login_location or 'ready'})")
        else:
            missing.append(name)
            _warn(name, "no starter yet" + (f" (login: {detection.login_location})"
                                             if detection.login_location else ""))
        if name == "pi" and written:
            _check_pi_model(manifests / "agents" / "pi.md")
    if missing:
        print(f"      run `{PROG} setup` to write starters for: {', '.join(missing)}")


def _check_pi_model(agent: Path) -> None:
    """Warn when the pi starter's free OpenRouter model has left the free list."""
    try:
        frontmatter, _ = parse_frontmatter(agent.read_text())
    except (OSError, YamlSubsetError):
        return
    provider = frontmatter.get("agent_provider")
    settings = provider.get("settings") if isinstance(provider, dict) else None
    models = settings.get("models") if isinstance(settings, dict) else None
    if not isinstance(settings, dict) or settings.get("provider") != "openrouter" \
            or not isinstance(models, list) or not models:
        return
    model = str(models[0])
    if not model.endswith(":free"):
        return
    try:
        still_free = openrouter.listed(model)
    except openrouter.OpenRouterError:
        return  # offline
    if not still_free:
        _warn("pi", f"{model} is no longer a free OpenRouter model; "
                    f"run `{PROG} setup --refresh-model`")


def _has_agents() -> bool:
    """Whether this host has an agent to start (the architect doesn't count)."""
    agents = bot_bottle_root() / "agents"
    return any(p.stem != "bottle-architect" for p in agents.glob("*.md")) \
        if agents.is_dir() else False


def next_step(found: BackendFindings) -> str:  # pylint: disable=too-many-return-statements
    """The one thing to do next on this host, most pressing first."""
    state = runner.running()
    if state is not None and runner.restart_needed(state):
        return (f"`{PROG} up --restart` (the host controller runs older code; "
                f"running bottles keep running)")
    if found.selected and not found.selected_ready:
        return (f"`{PROG} install --backend={found.selected}` "
                f"(it prints any sudo steps it needs)")
    if not found.selected and not found.any_ready:
        return ("set up a backend: Docker, or Apple Container (macOS) / Firecracker "
                f"(Linux), then `{PROG} install`")
    if repair := _repair_command():
        return f"{repair} (the console no longer knows this host)"
    if not _has_agents():
        return f"`{PROG} setup`"
    if state is None:
        return f"`{PROG} up`, then run `{PROG}`"
    return f"run `{PROG}`"


def next_block(step: str, *, color: bool) -> str:
    """`step` (from `next_step`) set apart: its first `command` alone on a
    line, bold on a terminal, with the rest of the sentence under it."""
    parts = step.split("`")
    if len(parts) < 3:
        return f"\nNext: {step}\n"
    command = parts[1]
    rest = "`".join(parts[2:]).strip(" ,")
    if color:
        command = f"\x1b[1;36m{command}\x1b[0m"
        rest = f"\x1b[2m{rest}\x1b[0m" if rest else ""
    lines = ["", "Next, run:", "", f"    {command}", ""]
    if rest:
        lines += [f"  {rest}", ""]
    return "\n".join(lines)


def _print_next(step: str) -> None:
    """The one next step: a plain `next:` line off a terminal (scripts read
    it), set apart on one."""
    if sys.stdout.isatty():
        print(next_block(step, color=not os.environ.get("NO_COLOR")))
    else:
        print(f"next: {step}")


def cmd_doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} doctor",
        description="Check host prerequisites for running bot-bottle.",
    )
    parser.add_argument("--backend", choices=known_backend_names(), default=None,
                        help="check only this backend's readiness, exiting non-zero "
                             "unless it's ready (what CI gates on)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="show every backend's diagnostics, not only the one in use")
    parser.add_argument("--next", action="store_true",
                        help="print only the next step for this host, one line")
    ns = parser.parse_args(argv)
    if ns.next:
        with contextlib.redirect_stdout(io.StringIO()):
            found = _check_backends()
        print(next_step(found))
        return 0
    if ns.backend:
        # pylint: disable-next=import-outside-toplevel
        from ...backend import get_bottle_backend
        sys.stderr.write(f"environment: {environment_line()}\n")
        return get_bottle_backend(settings_from_env(), ns.backend).status()

    _note("environment", environment_line())
    # Hard requirements set the exit code.
    python = _check_python()
    found = _check_backends(ns.verbose)
    backend = found.selected_ready if found.selected else found.any_ready
    required = [python, backend, _check_tmux()]
    _check_config_dir()
    _check_orchestrator()
    _check_host_controller()
    _check_console_link()
    _check_providers()
    _print_next(next_step(found))
    return 0 if all(required) else 1

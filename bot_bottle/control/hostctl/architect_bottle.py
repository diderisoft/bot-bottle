"""The bottle architect's warm bottle (PRD 0117).

`bottle-architect` runs inside the `bottle-architect` bottle (`suggest/
bottles/`): its model API and nothing else. Booting a bottle takes far
longer than the model call, so the host controller keeps one running:
`warm()` boots it in the background (`bot-bottle start` asks as soon as it
starts, so the boot overlaps the harness picker and the task prompt), and it
stays up for `Settings.operation.architect_idle_seconds`
(`BOT_BOTTLE_ARCHITECT_IDLE`, default 600) after its last suggestion, so back-to-back starts
and console suggestions reuse it.

Each suggestion (`run`) is a fresh headless harness call inside the VM, so
tasks never share a conversation; the prompt's static part (the agent's
system prompt, provisioned as its prompt file) comes first, where a harness
that caches prompts (claude, codex) reuses it. The answer is the call's
stdout, parsed and validated by `suggest`. Any failure raises
`ArchitectError`, and the suggester falls back to its rules path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...contract.hostctl import Settings
from ...lib import log
from ...suggest.architect import TIMEOUT_SECONDS, Architect, ArchitectError
from ...suggest.starter import ARCHITECT_AGENT

if TYPE_CHECKING:
    from ...backend import BottleBackend
    from .audit_events import HostAudit

# The built-in bottle it runs in, kept out of every library listing.
ARCHITECT_BOTTLES_DIR = Path(__file__).resolve().parents[2] / "suggest" / "bottles"
ARCHITECT_BOTTLE = "bottle-architect"

# Booting includes getting images, so a first boot can take a while.
BOOT_SECONDS = 600.0
# How long stopping a timed-out call's process in the bottle may take.
STOP_SECONDS = 15.0
# Stops every process whose command line carries the call's mark, and their
# descendants (the agent's tools): TERM, then KILL. The bottle has no ps or
# pkill. It goes on stdin (`private`), so this shell's own command line
# doesn't carry the mark.
_STOP_MARKED = """m={mark}
pids=""
for d in /proc/[0-9]*; do
  grep -qaF -- "$m" "$d/cmdline" 2>/dev/null && pids="$pids ${{d#/proc/}}"
done
[ -n "$pids" ] || exit 0
added=1
while [ -n "$added" ]; do
  added=""
  for d in /proc/[0-9]*; do
    p=${{d#/proc/}}
    case " $pids " in *" $p "*) continue ;; esac
    pp=$(sed 's/.*) //' "$d/stat" 2>/dev/null | cut -d' ' -f2)
    case " $pids " in *" $pp "*) pids="$pids $p"; added=1 ;; esac
  done
done
kill -TERM $pids 2>/dev/null
sleep 2
kill -KILL $pids 2>/dev/null
exit 0
"""


def model_args(model: str) -> list[str]:
    """The harness args that pick `model` (claude, codex and pi take `--model`)."""
    return ["--model", model] if model else []


def _default_backend(settings: Settings) -> "BottleBackend[Any, Any]":
    from ...backend import get_bottle_backend  # pylint: disable=import-outside-toplevel
    return get_bottle_backend(settings, prompt=False)


class ArchitectBottle:  # pylint: disable=too-many-instance-attributes
    """One warm `bottle-architect` bottle, booted on demand."""

    def __init__(self, *, host_root: Path,
                 backend_for: Callable[[Settings], "BottleBackend[Any, Any]"] = _default_backend,
                 secrets_file: Path | None = None, audit: "HostAudit | None" = None,
                 idle: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        # `idle` fixes the idle time (tests); else each warm or suggestion's
        # settings set it.
        self._host_root = host_root
        self._backend_for = backend_for
        self._secrets_file = secrets_file
        self._audit = audit
        self._fixed_idle = idle
        self._idle = float(Settings().operation.architect_idle_seconds) if idle is None else idle
        self._clock = clock
        self._cond = threading.Condition()
        self._state = "down"  # down | booting | ready
        self._error = ""
        self._bottle: Any = None
        self._plan: Any = None
        self._busy = 0
        self._last_used = 0.0
        self._stopping = False
        self._thread: threading.Thread | None = None
        # Each boot's number: a torn-down holder mustn't touch a newer boot.
        self._generation = 0

    # --- the warm bottle ---

    def status(self) -> dict[str, object]:
        with self._cond:
            return {"state": self._state, "error": self._error, "idle_seconds": self._idle}

    def warm(self, settings: Settings | None = None) -> None:
        """Boot the bottle in the background unless it's up or coming up."""
        with self._cond:
            self._start_locked(settings or Settings())

    def stop(self) -> None:
        """Tear the bottle down (after any suggestion in flight)."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=BOOT_SECONDS)

    def _start_locked(self, settings: Settings) -> None:
        if self._fixed_idle is None:
            self._idle = float(settings.operation.architect_idle_seconds)
        if self._state != "down":
            return
        self._state, self._error, self._stopping = "booting", "", False
        self._last_used = self._clock()
        self._generation += 1
        self._thread = threading.Thread(target=self._hold, args=(settings, self._generation),
                                        daemon=True,
                                        name="hostctl-bottle-architect")
        self._thread.start()

    def _spec(self, workspace: str) -> Any:  # a BottleSpec
        # pylint: disable=import-outside-toplevel
        from ...backend import BottleSpec
        from ...manifest import ManifestIndex
        index = ManifestIndex(bottles={}, agents={}, home_md=self._host_root, cwd_md=None,
                              extra_bottle_dirs=(ARCHITECT_BOTTLES_DIR,))
        return BottleSpec(manifest=index, agent_name=ARCHITECT_AGENT, copy_cwd=False,
                          user_cwd=workspace, label=ARCHITECT_AGENT,
                          bottle_names=(ARCHITECT_BOTTLE,), headless=True,
                          image_policy="prefer-cached")

    def _hold(self, settings: Settings, generation: int) -> None:
        """The holder thread: boot, serve until idle or stopped, tear down."""
        # pylint: disable=import-outside-toplevel
        from ...bottle_state import settle_state
        from ...paths import host_secrets_path
        from ...secret_source import FileSecrets
        from .audit_events import digest_manifest

        stage = Path(tempfile.mkdtemp(prefix="bot-bottle-stage."))
        workspace = tempfile.mkdtemp(prefix="bottle-architect.")
        identity = ""
        try:
            backend = self._backend_for(settings)
            plan = backend.prepare(self._spec(workspace), stage_dir=stage,
                                   secrets=FileSecrets(self._secrets_file or host_secrets_path()))
            identity = str(getattr(plan, "slug", ""))
            with backend.launch(plan) as bottle:
                activation = bottle.registry_id or str(uuid.uuid4())
                policy = digest_manifest(plan.manifest)
                if self._audit is not None:
                    self._audit.bottled_agent_start(
                        bottle=identity, activation=activation, manifest_digest=policy,
                        backend=backend.name, agent=ARCHITECT_AGENT)
                with self._cond:
                    self._bottle, self._plan, self._state = bottle, plan, "ready"
                    self._last_used = self._clock()
                    self._cond.notify_all()
                    while not self._done_locked():
                        self._cond.wait(timeout=self._wait_locked())
                    self._state = "down"
                    self._bottle = self._plan = None
                if self._audit is not None:
                    self._audit.bottled_agent_exit(
                        bottle=identity, activation=activation, manifest_digest=policy,
                        exit_code=0, stop_requested=True)
                log.info(f"bottle-architect bottle {identity}: idle, removing it")
        except BaseException as e:  # pylint: disable=broad-exception-caught
            with self._cond:
                if self._generation == generation:
                    self._error = str(e) or type(e).__name__
            log.warn(f"bottle-architect bottle: {str(e) or type(e).__name__}")
        finally:
            with self._cond:
                if self._generation == generation:
                    self._state = "down"
                    self._bottle = self._plan = None
                self._cond.notify_all()
            if identity:
                settle_state(identity)
            shutil.rmtree(stage, ignore_errors=True)
            shutil.rmtree(workspace, ignore_errors=True)

    def _done_locked(self) -> bool:
        if self._busy:
            return False
        return self._stopping or self._clock() - self._last_used >= self._idle

    def _wait_locked(self) -> float:
        return max(0.05, self._idle - (self._clock() - self._last_used))

    # --- a suggestion ---

    def run(self, architect: Architect, prompt: str, settings: Settings | None = None) -> str:
        """The architect's answer to `prompt`: a fresh headless call in the
        warm bottle, booting it first if it's down. ArchitectError when it
        can't boot, fails or times out."""
        # pylint: disable-next=import-outside-toplevel
        from .bottles import agent_args
        with self._cond:
            self._start_locked(settings or Settings())
            # Busy from here, so a bottle that just booted waits for this call.
            self._busy += 1
            deadline = self._clock() + BOOT_SECONDS
            while self._state == "booting" and self._clock() < deadline:
                self._cond.wait(timeout=min(deadline - self._clock(), 1.0))
            if self._state != "ready":
                self._busy -= 1
                self._cond.notify_all()
                if self._state == "booting":
                    raise ArchitectError(f"its bottle didn't boot within {BOOT_SECONDS:.0f}s")
                raise ArchitectError(f"its bottle didn't start: {self._error or 'unknown'}")
            bottle, plan = self._bottle, self._plan
        try:
            if plan.agent_provision.harness != architect.harness:
                with self._cond:
                    self._stopping = True  # the next suggestion boots the new harness
                raise ArchitectError(f"its bottle runs {plan.agent_provision.harness}, but "
                                     f"{architect.path} now names {architect.harness}; "
                                     "the next suggestion boots one that does")
            # A mark only this call's process carries, to stop it if it times out.
            mark = f"bot-bottle-architect-call-{uuid.uuid4().hex}"
            argv = bottle.agent_argv(agent_args(
                architect.harness,
                (*plan.agent_provision.startup_args, *model_args(architect.model)),
                f"{prompt}\n\n<!-- {mark} -->", headless=True), tty=False)
            try:
                # The proxy behind argv's bare `--env` names comes from here;
                # without it the agent has no route to its model API.
                result = subprocess.run(argv, capture_output=True, text=True, check=False,
                                        timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
                                        env={**os.environ, **bottle.agent_env()})
            except subprocess.TimeoutExpired as e:
                # Killing `container exec`/`ssh` here leaves the agent running
                # in the bottle; stop it there too.
                _stop_marked(bottle, mark)
                raise ArchitectError(f"{architect.harness} didn't answer within "
                                     f"{TIMEOUT_SECONDS}s") from e
            except OSError as e:
                raise ArchitectError(f"{architect.harness} didn't run: {e}") from e
            if result.returncode != 0 or not result.stdout.strip():
                raise ArchitectError(f"{architect.harness} failed in its bottle "
                                     f"(exit {result.returncode}); is it logged in on this host?")
            return result.stdout.strip()
        finally:
            with self._cond:
                self._busy -= 1
                self._last_used = self._clock()
                self._cond.notify_all()


def _stop_marked(bottle: Any, mark: str) -> None:
    """Stop the timed-out call's process in `bottle`; a failure only logs."""
    try:
        result = bottle.exec(_STOP_MARKED.format(mark=mark), user="root", timeout=STOP_SECONDS)
    except Exception as e:  # pylint: disable=broad-exception-caught
        log.warn(f"bottle-architect: couldn't stop the timed-out call: {e}")
        return
    if result.returncode != 0:
        log.warn(f"bottle-architect: stopping the timed-out call exited {result.returncode}")


_current: ArchitectBottle | None = None


def install(architect: ArchitectBottle | None) -> None:
    """The host controller's warm architect, for its routes and the console link."""
    global _current  # pylint: disable=global-statement
    _current = architect


def current() -> ArchitectBottle | None:
    return _current


def runner(settings: Settings | None = None) -> "Callable[[Architect, str], str] | None":
    """The suggester's `run` for this host controller's warm bottle, or None
    outside one."""
    architect = _current
    if architect is None:
        return None
    return lambda a, prompt: architect.run(a, prompt, settings)


__all__ = ["ARCHITECT_BOTTLE", "ARCHITECT_BOTTLES_DIR", "ArchitectBottle", "current", "install",
           "model_args", "runner"]

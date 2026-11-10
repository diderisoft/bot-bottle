"""bot-bottle setup: write starter agents for the providers on this host.

Claude and Codex starters reuse an installed, logged-in CLI's login
(`login: host`). The pi starter uses your pi config when it can,
else a free OpenRouter model with tool support, signing in through the browser.
Keys go in `~/.bot-bottle/secrets` (0600).

Existing files are kept unless --force. At the end, setup offers `bot-bottle
pair`.

Usage:
  bot-bottle setup [--force] [--refresh-model] [--no-browser] [--no-pair]
                   [--console-url URL]

Flags:
  --force           overwrite existing starter files
  --refresh-model   re-pick the pi starter's free model (only if setup wrote it)
  --no-browser      sign in to OpenRouter by pasting a code (SSH sessions)
  --no-pair         don't offer to pair with a phone
  --console-url URL console to pair with (or BB_CONSOLE_URL; else
                    https://bot-bottle.app)
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ...agent_provider import HostDetection, get_provider
from ...contrib.pi import starter as pi_starter
from ...control.hostctl import pairing
from . import login
from ...errors import SecretsFileError
from ...lib import log, prompt
from ...manifest.versions import record_or_warn
from ...paths import bot_bottle_root, host_secrets_path
from ...secret_source import write_secret
from ...starter import STARTER_MARKER
from ..constants import PROG
from .. import openrouter

PROVIDERS = ("claude", "codex", "pi")
FREE_LIMIT_NOTE = (
    "free OpenRouter models allow about 50 requests a day (1,000 once the account "
    "has bought $10 of credits); an agent can use that up in one session"
)


@dataclasses.dataclass
class Setup:
    """One run's choices and I/O, so tests can drive it."""

    home: Path
    host_env: Mapping[str, str]
    interactive: bool
    force: bool = False
    refresh_model: bool = False
    browser: bool = True
    say: Callable[[str], None] = print
    ask: Callable[[str], str] = input
    # A yes/no answer to a question; None asks `ask` with `[Y/n]`.
    yes_no: Callable[[str], bool] | None = None
    sign_in: Callable[..., str] = openrouter.sign_in
    lookup: Callable[[], list[openrouter.FreeModel]] = openrouter.free_tool_models
    probe: Callable[[str, str], bool] = openrouter.probe

    @property
    def manifests(self) -> Path:
        return bot_bottle_root(self.home)

    def confirm(self, question: str) -> bool:
        if not self.interactive:
            return False
        if self.yes_no is not None:
            return self.yes_no(question)
        return self.ask(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")

    def write(self, kind: str, name: str, text: str, *, replace: bool = False) -> None:
        path = self.manifests / kind / f"{name}.md"
        if path.exists() and not (self.force or replace):
            self.say(f"  kept {path} (already exists)")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.say(f"  wrote {path}")
        record_or_warn(f"setup: write {kind}/{name}", root=self.manifests)

    def starter_exists(self, name: str) -> bool:
        return (self.manifests / "agents" / f"{name}.md").exists()

    # --- providers ---

    def run(self) -> int:
        ready: list[str] = []  # each harness's starter agent, in order
        for name in PROVIDERS:
            provider = get_provider(name)
            detection = provider.detect_host(self.host_env)
            if name == "pi":
                detection = self.pi_detection(detection)
                if detection is None:
                    continue
            elif not detection.installed:
                continue
            elif not detection.logged_in:
                self.say(f"warn: {name} is installed but not logged in; run "
                         f"`{detection.login_command}`, then `{PROG} setup`")
                continue
            self.say(f"{name}: {detection.login_location or 'ready'}")
            # The agent carries the provider, so pi's model refresh rewrites it.
            agent = provider.create_default_agent_manifest(detection)
            self.write("agents", name, agent, replace=name == "pi" and self.refresh_model)
            ready.append(agent)
        if ready:
            self._write_bottle_architect(ready[0])
        return 0

    def _write_bottle_architect(self, agent: str) -> None:
        """Write the bottle-architect agent starter on the harness of `agent`,
        the first one set up here. The suggester reads this file for its
        prompt, harness and model, and runs it in its own bottle."""
        # pylint: disable-next=import-outside-toplevel
        from ...suggest.starter import ARCHITECT_AGENT, starter_for
        self.write("agents", ARCHITECT_AGENT, starter_for(agent))

    def pi_detection(  # pylint: disable=too-many-return-statements  # one per outcome
            self, detection: HostDetection) -> HostDetection | None:
        """pi's model source: the host's pi config, else free OpenRouter. None
        when there's nothing to write."""
        if detection.logged_in:
            return detection
        local = pi_starter.read_local_model(self.host_env)
        if local is not None and local.literal_key and self.confirm(
                f"pi: copy the {local.provider} key from ~/.pi/agent/models.json into "
                f"{host_secrets_path()} for bottles to use?"):
            self.store(local.key_name, pi_starter.read_local_literal_key(self.host_env))
            return dataclasses.replace(detection, logged_in=True)
        if self.starter_exists("pi") and not (self.force or self.refresh_model):
            self.say("pi: starter already written")
            return None
        if self.refresh_model and not self.written_by_setup("pi"):
            self.say(f"pi: {self.manifests / 'agents' / 'pi.md'} wasn't written by setup; "
                     "leaving its model alone")
            return None
        if not pi_starter.has_secret(pi_starter.OPENROUTER_KEY_NAME, self.host_env):
            if not self.interactive:
                self.say(f"pi: skipped; run `{PROG} setup` in a terminal to sign in to "
                         "OpenRouter (free) for the pi starter")
                return None
            if not self.confirm("pi: sign in to OpenRouter (free) to run pi on a free model?"):
                return None
            try:
                key = self.sign_in(browser=self.browser, say=self.say, ask=self.ask)
            except openrouter.OpenRouterError as e:
                self.say(f"warn: pi: {e}")
                return None
            self.store(pi_starter.OPENROUTER_KEY_NAME, key)
        key = pi_starter.secret_value(pi_starter.OPENROUTER_KEY_NAME, self.host_env)
        try:
            self.say("pi: checking which free OpenRouter models answer right now")
            model = openrouter.pick_working_model(self.lookup(), key, check=self.probe)
        except openrouter.OpenRouterError as e:
            self.say(f"warn: pi: {e}")
            return None
        self.say(f"note: {FREE_LIMIT_NOTE}")
        return dataclasses.replace(
            detection, logged_in=True, login_location=f"OpenRouter ({model.id})",
            starter_settings=pi_starter.starter_settings(
                "openrouter", pi_starter.OPENROUTER_BASE_URL, model.id,
                pi_starter.OPENROUTER_KEY_NAME, context_window=model.context_length,
                max_tokens=model.max_completion_tokens, reasoning=model.reasoning))

    def written_by_setup(self, name: str) -> bool:
        try:
            return STARTER_MARKER in (self.manifests / "agents" / f"{name}.md").read_text()
        except OSError:
            return False

    def store(self, name: str, value: str) -> None:
        write_secret(host_secrets_path(), name, value)
        self.say(f"  saved {name} in {host_secrets_path()}")


def _offer_pair(setup: Setup, console_url: str) -> None:
    if not setup.interactive or not console_url:
        return
    try:
        pairing.load_console()
        return  # already paired or logged in
    except pairing.RemoteError:
        pass
    if setup.confirm("Pair this host with your phone now?"):
        from .pair import cmd_pair  # pylint: disable=import-outside-toplevel
        with prompt.outside():  # its QR code needs the whole terminal
            cmd_pair(["--console-url", console_url])
    else:
        setup.say(f"pair later with `{PROG} pair`")


def _on_a_view() -> dict[str, Any]:
    """Setup's I/O when a full-screen view runs it (`lib.prompt`): its lines
    to the view's sink and its questions asked there. Nothing otherwise."""
    asker = prompt.current()
    if asker is None:
        return {}

    def ask(question: str) -> str:
        return asker.ask(question.strip().rstrip(":"))

    def yes_no(question: str) -> bool:
        return asker.confirm(question, default=True)

    return {"interactive": True, "say": log.write_line, "ask": ask, "yes_no": yes_no}


def cmd_setup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{PROG} setup", description=(__doc__ or "").split("\n", maxsplit=1)[0])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--refresh-model", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-pair", action="store_true")
    parser.add_argument("--console-url",
                        default=os.environ.get("BB_CONSOLE_URL") or login.DEFAULT_CONSOLE_URL)
    args = parser.parse_args(argv)
    io: dict[str, Any] = {"interactive": sys.stdin.isatty(), **_on_a_view()}
    setup = Setup(
        home=Path(os.environ.get("HOME") or Path.home()),
        host_env=dict(os.environ),
        force=args.force,
        refresh_model=args.refresh_model,
        browser=not args.no_browser,
        **io,
    )
    try:
        status = setup.run()
    except SecretsFileError as e:
        log.write_line(f"{PROG} setup: {e}")
        return 1
    if not args.no_pair:
        _offer_pair(setup, args.console_url.rstrip("/"))
    return status

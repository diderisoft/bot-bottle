#!/usr/bin/env python3
"""A repeatable bot-bottle demo, driven through the real CLI in tmux.

    python3 scripts/demo/demo.py setup      # once per host: the demo environment
    python3 scripts/demo/demo.py play       # the demo; run it as often as you like
    python3 scripts/demo/demo.py play --phone --record --console URL
    python3 scripts/demo/demo.py watch      # join the tmux session play drives
    python3 scripts/demo/demo.py teardown   # stop it; --purge removes the environment

The demo runs in its own environment (`BOT_BOTTLE_ENV=demo`), so it never
touches your live install, and on a fresh copy of `fixture/` (a tiny Node
library with one failing test, and a `./main` bottle granting one API and a
push to an upstream that doesn't exist) every time. `play` drives a tmux
session the way a person would: `bot-bottle start <agent>`, the task pasted,
Enter on the architect's proposal. Once the agent is done it checks the tests
in the bottle, then shows the boundary from the agent's own terminal (`!`
runs a plain command): a host the bottle doesn't allow, the API it does, a
token sent to that API (refused by the egress scan), and the token committed
and pushed (refused by git-gate's gitleaks). It waits for each screen before
the next step and stops with the screen saved when one doesn't come.

With `--phone`, the console the environment is paired with (`setup --phone
--console URL`) is filmed at phone size too, and after the blocked host the
agent asks for it (supervise's `egress-allow`), the phone taps Allow, and the
same request goes through. With `--record` as well, the two recordings are
put side by side, the terminal on a laptop and the console on a phone.

`setup --phone` needs nobody's passkey or account: it pairs the demo
environment as a new, anonymous console account whose only sign-in is a
passkey on the phone's virtual authenticator (saved, 0600, in
~/.cache/bot-bottle-demo/phone/passkey.json, so later runs sign in again
unattended). That account stays on the console after `teardown --purge`:
the console has no way to delete an account or a host yet.

Stdlib only. Needs tmux, and the `pi` starter in the demo environment, which
`setup` makes (signing in to a free OpenRouter model). Recording needs vhs
and ffmpeg; the phone needs node and Chrome (`setup --phone` installs
Playwright under ~/.cache/bot-bottle-demo, outside the repo).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shlex
import shutil
import string
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixture"
ENV_NAME = "demo"
STATE = Path.home() / ".bot-bottle-envs" / ENV_NAME
WORK = Path.home() / ".cache" / "bot-bottle-demo"
PROJECT = WORK / "slugs"
UPSTREAM = "ssh://git@upstream.invalid/slugs.git"
BIN = WORK / "bin"
LOGS = WORK / "logs"
RECORDINGS = WORK / "recordings"
SOCKET = "bb-demo"
SESSION = "demo"
SIZE = (120, 36)
# The phone: Playwright (installed by `setup --phone`, not a bot-bottle
# dependency) filming the console the demo environment is paired with.
PHONE_DIR = WORK / "phone"
PHONE_JS = Path(__file__).resolve().parent / "phone.cjs"
# The phone's own passkey (phone.cjs pair), for the demo's own console account.
PASSKEY = PHONE_DIR / "passkey.json"
CONSOLE_ENV = "BOT_BOTTLE_DEMO_CONSOLE"
PLAYWRIGHT = "1.64.1"

AGENTS = ("claude", "pi")
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
TASK = ("One test in slugify.test.js fails. Fix slugify.js so `npm test` passes, "
        "show me the test output, then commit the fix.")

# What each screen shows once it's ready for the next key (the guided start's
# own footers, `cli/commands/guided_view.py`).
TASK_READY = r"enter suggest a bottle"
REVIEW_READY = r"enter run · s save as"
PROMPT = r"^\$\s*$"
# bot-bottle's status line under the agent's terminal, once it's running.
AGENT_READY = r"· {agent} agent"
# The tests, run in the bottle once the agent is done, and the summary lines
# Node's test runner (`node --test`) prints. The typed command shows
# `(pass|fail)`, so it never matches these itself.
CHECK = 'npm test 2>&1 | grep -E "^# (pass|fail)"'
TESTS_RAN = r"^\s*# fail \d+"
TESTS_PASS = r"^\s*# fail 0\b"
# The supervise beat: the agent asks for the blocked host, the phone allows
# it, and the same request goes through.
# "New request, from me": the `!` command just before it comes with Claude
# Code's caveat not to respond to those messages, and the prompt joins the
# same turn; without the opener Claude took the prompt as covered by it and
# answered with nothing.
ASK = ("New request, from me: I need https://example.com, which the bottle just "
       "blocked. Request access with the supervise egress-allow tool and tell me "
       "what it returns. Don't fetch it yourself.")
ASKED_HOST = "example.com"
ALLOWED = ('curl -sS -o /dev/null -w "example.com: %{http_code}\\n" https://example.com',
           r"example\.com: 200")


class DemoError(RuntimeError):
    pass


def say(line: str) -> None:
    print(f"demo: {line}", flush=True)


# --- the CLI the demo runs -------------------------------------------------

def demo_env() -> dict[str, str]:
    env = dict(os.environ)
    env["BOT_BOTTLE_ENV"] = ENV_NAME
    env.pop("BOT_BOTTLE_ROOT", None)
    # A key from the caller's shell would win over the starters' own logins.
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        env.pop(name, None)
    env["PATH"] = f"{BIN}{os.pathsep}{env.get('PATH', '')}"
    return env


def write_shim() -> None:
    """`bot-bottle` on the session's PATH runs this checkout, so the demo shows
    the code it ships with (`BOT_BOTTLE_DEMO_CLI` names another command)."""
    BIN.mkdir(parents=True, exist_ok=True)
    cli = os.environ.get("BOT_BOTTLE_DEMO_CLI") or (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(REPO / 'cli.py'))}")
    shim = BIN / "bot-bottle"
    shim.write_text(f'#!/bin/sh\nexec {cli} "$@"\n', encoding="utf-8")
    shim.chmod(0o755)


def bb(*args: str, check: bool = True, capture: bool = False,
       answer: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the demo's `bot-bottle`; `answer` is typed to what it asks."""
    write_shim()
    try:
        return subprocess.run([str(BIN / "bot-bottle"), *args], env=demo_env(), check=check,
                              text=True, capture_output=capture, input=answer)
    except subprocess.CalledProcessError as e:
        raise DemoError(f"`bot-bottle {' '.join(args)}` failed (exit {e.returncode})") from e


# --- the project the agent works on ----------------------------------------

def fresh_project() -> None:
    """A new copy of the fixture with one commit, the same bytes every run."""
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    shutil.copytree(FIXTURE, PROJECT)
    # `./main` names the fake key by its home path; a static key path is used
    # as written (no `~` expansion), so give it this host's real one.
    bottle = PROJECT / ".bot-bottle" / "bottles" / "main.md"
    bottle.write_text(bottle.read_text(encoding="utf-8").replace(
        "~/.cache/bot-bottle-demo/fake-key", str(WORK / "fake-key")), encoding="utf-8")
    stamp = "2026-01-01T00:00:00Z"
    env = dict(os.environ, GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="demo@example.invalid",
               GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="demo@example.invalid",
               GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "slugify, with a failing test"],
                # Repo-local, so the copy in the bottle commits as "demo".
                ["git", "config", "user.name", "demo"],
                ["git", "config", "user.email", "demo@example.invalid"],
                # The upstream `./main` grants: pushes go through git-gate.
                ["git", "remote", "add", "origin", UPSTREAM]):
        subprocess.run(cmd, cwd=PROJECT, env=env, check=True)


def fake_key() -> None:
    """The SSH key `./main` names for its upstream. Never used: the upstream
    doesn't exist, and the gate refuses the demo's push before dialling it."""
    if (WORK / "fake-key").exists():
        return
    WORK.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                    "bot-bottle demo (unused)", "-f", str(WORK / "fake-key")], check=True)


def fake_token() -> str:
    """A GitHub-token-shaped string, the same every run and never real: what
    the demo tries to send out. Made here, not written in the repo, so the
    repo's own secret scan has nothing to flag."""
    rng = random.Random(1039)
    alphabet = string.ascii_letters + string.digits
    return "ghp_" + "".join(rng.choice(alphabet) for _ in range(36))


def trust_project() -> None:
    """Trust the fixture's `./main` off camera; its bytes are the same every
    run, so after the first run this changes nothing."""
    out = subprocess.run([str(BIN / "bot-bottle"), "trust", "./main", "--yes"], cwd=PROJECT,
                         env=demo_env(), text=True, capture_output=True, check=False)
    if out.returncode != 0:
        raise DemoError("couldn't trust the fixture's ./main: "
                        + (out.stderr.strip().splitlines() or ["no output"])[-1])


def pin_model(model: str) -> None:
    """Run the demo's pi starter on one model. `setup` writes it with
    `openrouter/free`, which picks a different free model per request, and
    some of those don't call tools; one model makes every run the same."""
    path = STATE / "agents" / "pi.md"
    text = path.read_text(encoding="utf-8")
    pinned, n = re.subn(r"(?m)^(\s*models:\s*\n)(?:\s*-\s*\S+\s*\n)+",
                        lambda m: f"{m.group(1)}      - {model}\n", text, count=1)
    if n != 1:
        raise DemoError(f"no `models:` list in {path} to pin {model} in")
    if pinned != text:
        path.write_text(pinned, encoding="utf-8")


def warm_architect() -> None:
    """Ask for this run's suggestion once, off camera. The cleanup stops the
    architect's bottle with the rest, and a cold one spends ~25s booting on
    screen; warm, the on-screen ask takes ~3s."""
    say("warming the architect")
    started = time.monotonic()
    out = None
    # While its bottle boots, a suggestion falls back to the rules path; ask
    # again until the architect itself answers.
    while time.monotonic() - started < 180:
        out = bb("bottles", "suggest", TASK, "--cwd", str(PROJECT), "--json",
                 check=False, capture=True)
        if out.returncode == 0 and '"source": "architect"' in out.stdout:
            say(f"architect warm ({time.monotonic() - started:.0f}s)")
            return
        time.sleep(5)
    last = out.stderr.strip().splitlines() if out else []
    raise DemoError("the architect didn't answer a suggestion in 180s: "
                    + (last or ["no output"])[-1])


def reset_state(agent: str, model: str) -> None:
    """Forget what the last run left behind in the demo environment: any
    bottle saved from a proposal. The starters, secrets and infra stay, so a
    run doesn't sign in or build again. The remembered agent (host DB) can
    stay too: `play` names its agent."""
    if agent == "pi":
        pin_model(model)
    saved = STATE / "bottles"
    if saved.exists():
        shutil.rmtree(saved)


# --- tmux ------------------------------------------------------------------

def tmux(*args: str, capture: bool = False) -> str:
    out = subprocess.run(["tmux", "-L", SOCKET, *args], check=True, text=True,
                         capture_output=capture)
    return out.stdout if capture else ""


def session_alive() -> bool:
    return subprocess.run(["tmux", "-L", SOCKET, "has-session", "-t", SESSION],
                          capture_output=True, check=False).returncode == 0


class Terminal:
    """The session play types into, read back the way a person would see it."""

    def __init__(self, pace: float) -> None:
        self.pace = pace
        # Stretches of waiting (seconds from when filming starts), which the
        # trimmed copy squeezes. `t0` is set when the recorder attaches.
        self.t0: float | None = None
        self.t0_wall = 0.0  # the same moment, wall clock: the phone's video lines up on it
        self.dead: list[tuple[float, float]] = []
        self._dead_from: float | None = None

    def dead_from(self) -> None:
        """A wait starts: what follows until `dead_to` is mostly unchanging."""
        if self.t0 is not None:
            self._dead_from = time.monotonic() - self.t0

    def dead_to(self) -> None:
        if self.t0 is not None and self._dead_from is not None:
            self.dead.append((self._dead_from, time.monotonic() - self.t0))
            self._dead_from = None

    def open(self) -> None:
        if session_alive():
            tmux("kill-session", "-t", SESSION)
        cols, rows = SIZE
        # The server starts from demo_env(), so the shell inherits it; a bare
        # bash with a plain prompt, so every run looks the same. PS1 is set on
        # bash's own command line: some tmux builds don't pass `-e PS1=…` on.
        subprocess.run(["tmux", "-L", SOCKET, "-f", "/dev/null", "new-session", "-d",
                        "-s", SESSION, "-x", str(cols), "-y", str(rows), "-c", str(PROJECT),
                        "-e", "TERM=xterm-256color",
                        "env PS1='$ ' bash --noprofile --norc"], env=demo_env(), check=True)
        tmux("set-option", "-t", SESSION, "status", "off")

    def screen(self) -> str:
        return tmux("capture-pane", "-p", "-t", SESSION, capture=True)

    def wait(self, pattern: str, timeout: float, what: str) -> str:
        deadline = time.monotonic() + timeout
        rx = re.compile(pattern, re.MULTILINE)
        while time.monotonic() < deadline:
            text = self.screen()
            if rx.search(text):
                return text
            time.sleep(0.5)
        raise DemoError(f"waited {timeout:.0f}s for {what}; "
                        f"the screen is in {self.save('timeout')}")

    def settle(self, idle: float, timeout: float, what: str) -> str:
        """The screen once it has stopped changing for `idle` seconds: an agent
        waiting for its next message."""
        deadline = time.monotonic() + timeout
        last, since = self.screen(), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(1)
            now = self.screen()
            if now != last:
                last, since = now, time.monotonic()
            elif time.monotonic() - since >= idle:
                return now
        raise DemoError(f"waited {timeout:.0f}s for {what}; "
                        f"the screen is in {self.save('timeout')}")

    def type(self, text: str) -> None:
        for ch in text:
            # A bare `;` is tmux's command separator; `\;` is the character.
            tmux("send-keys", "-t", SESSION, "-l", "\\;" if ch == ";" else ch)
            time.sleep(self.pace)

    def key(self, *keys: str) -> None:
        tmux("send-keys", "-t", SESSION, *keys)

    def run(self, command: str) -> None:
        self.type(command)
        self.key("Enter")

    def shell(self, command: str) -> None:
        """`command` in the agent's shell mode: `!` typed, the command pasted."""
        self.type("!")
        time.sleep(0.3)
        self.paste(command)
        time.sleep(0.5)
        self.key("Enter")

    def paste(self, text: str) -> None:
        """`text` as one bracketed paste, the way a copied prompt arrives."""
        tmux("set-buffer", "-b", "demo", text)
        tmux("paste-buffer", "-p", "-d", "-b", "demo", "-t", SESSION)

    def wait_new(self, pattern: str, timeout: float, what: str, command: str) -> str:
        """Like `wait`, for `pattern` in what follows `command` on the screen:
        earlier beats' output stays on screen. (Counting matches on the whole
        screen missed one when an older match scrolled off as it came.)"""
        rx = re.compile(pattern, re.MULTILINE)
        start = command[:30]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.screen()
            at = text.rfind(start)
            if at >= 0 and rx.search(text, _past(text, at, command)):
                return text
            time.sleep(0.5)
        raise DemoError(f"waited {timeout:.0f}s for {what}; "
                        f"the screen is in {self.save('timeout')}")

    def save(self, tag: str) -> Path:
        LOGS.mkdir(parents=True, exist_ok=True)
        path = LOGS / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.txt"
        path.write_text(self.screen(), encoding="utf-8")
        return path


def _past(text: str, at: int, command: str) -> int:
    """Where `command`'s echo, starting at `at`, ends: the echo wraps a long
    command onto indented lines, so count its non-space characters."""
    left = sum(not c.isspace() for c in command)
    i = at
    while i < len(text) and left:
        left -= not text[i].isspace()
        i += 1
    return i


# --- recording -------------------------------------------------------------

TAPE = """\
Output "{out}.mp4"
Set Shell "bash"
Set FontSize 16
Set Width 1240
Set Height 760
Set Padding 20
Set TypingSpeed 0ms
Hide
Type "tmux -L {socket} attach -r -t {session}"
Enter
Sleep 1s
Show
Wait+Screen@30m /detached/
"""


class Recorder:
    """VHS filming the session read-only while play drives it. It attaches
    before the first keystroke and stops when play detaches it."""

    def __init__(self, agent: str) -> None:
        if not shutil.which("vhs"):
            raise DemoError("vhs isn't on PATH (brew install vhs)")
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        self.out = RECORDINGS / f"demo-{agent}"
        self.tape = RECORDINGS / f"demo-{agent}.tape"
        self.proc: subprocess.Popen[bytes] | None = None
        # The phone's recording, when there's one, and when it started
        # relative to this one (seconds): the composite puts them side by side.
        self.phone_video: Path | None = None
        self.phone_offset = 0.0

    def start(self, term: "Terminal") -> None:
        # The window keeps play's size whatever size VHS's terminal is.
        tmux("set-option", "-t", SESSION, "window-size", "manual")
        self.tape.write_text(TAPE.format(out=self.out, socket=SOCKET, session=SESSION),
                             encoding="utf-8")
        LOGS.mkdir(parents=True, exist_ok=True)
        log = (LOGS / "vhs.log").open("wb")  # pylint: disable=consider-using-with  # VHS outlives this call
        self.proc = subprocess.Popen(  # pylint: disable=consider-using-with  # stop() waits on it
            ["vhs", str(self.tape)], stdout=log, stderr=log)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if tmux("list-clients", "-t", SESSION, capture=True).strip():
                time.sleep(1)  # VHS shows its screen a second after attaching
                term.t0, term.t0_wall = time.monotonic(), time.time()
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.5)
        raise DemoError(f"VHS didn't attach; see {LOGS / 'vhs.log'}")

    def stop(self, linger: float, dead: list[tuple[float, float]]) -> None:
        if self.proc is None:
            return
        time.sleep(linger)  # the last screen, long enough to read
        subprocess.run(["tmux", "-L", SOCKET, "detach-client", "-s", SESSION],
                       capture_output=True, check=False)
        try:
            self.proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise DemoError(f"VHS didn't finish; see {LOGS / 'vhs.log'}") from None
        if self.proc.returncode != 0:
            raise DemoError(f"VHS failed; see {LOGS / 'vhs.log'}")
        # No full-length gif from VHS: its palette pass over a whole take
        # (minutes of frames) ran ffmpeg to ~20 GB. The trimmed copy gets one.
        say(f"recorded: {self.out}.mp4")
        marks = self.out.with_suffix(".dead.json")
        marks.write_text(json.dumps(dead), encoding="utf-8")
        trim(self.out, dead)
        if self.phone_video:
            both = Path(f"{self.out}-phone")
            composite(Path(f"{self.out}.mp4"), self.phone_video, self.phone_offset, both)
            trim(both, dead)


# The trimmed copy: in each dead stretch, frames that barely differ from the
# last one kept are dropped (a spinner, a ticking timer), so the agent's
# output stays and the waiting goes. 8x8 blocks; a frame is a duplicate when
# under FRAC of them changed by more than LO.
DECIMATE = "mpdecimate=hi=64*255:lo=64*6:frac=0.004"
# Kept on each side of a dead stretch, so its edges aren't clipped.
MARGIN = 0.5


def trim(out: Path, dead: list[tuple[float, float]]) -> None:
    """`<out>-trimmed.mp4` and `.gif`: the recording with its dead stretches
    squeezed. Everything outside them plays as filmed."""
    spans = [(a + MARGIN, b - MARGIN) for a, b in sorted(dead) if b - a > 2 * MARGIN + 1]
    parts, cursor = [], 0.0
    for a, b in spans:
        parts += [(cursor, a, False), (a, b, True)]
        cursor = b
    parts.append((cursor, None, False))
    chains, labels = [], []
    for i, (a, b, squeeze) in enumerate(parts):
        end = f":end={b:.3f}" if b is not None else ""
        chain = f"[0:v]trim=start={a:.3f}{end},setpts=PTS-STARTPTS"
        if squeeze:
            chain += f",{DECIMATE},setpts=N/FRAME_RATE/TB"
        chains.append(f"{chain}[p{i}]")
        labels.append(f"[p{i}]")
    graph = ";".join(chains) + f";{''.join(labels)}concat=n={len(labels)}:v=1:a=0[v]"
    trimmed = Path(f"{out}-trimmed")
    run = [["ffmpeg", "-y", "-v", "error", "-i", f"{out}.mp4", "-filter_complex", graph,
            "-map", "[v]", "-pix_fmt", "yuv420p", f"{trimmed}.mp4"],
           ["ffmpeg", "-y", "-v", "error", "-i", f"{trimmed}.mp4", "-filter_complex",
            "fps=12,scale='min(iw,1280)':-2:flags=lanczos,"
            "split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse",
            f"{trimmed}.gif"]]
    if not shutil.which("ffmpeg"):
        raise DemoError("ffmpeg isn't on PATH (brew install ffmpeg); the full recording is kept")
    for argv in run:
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise DemoError(f"trimming failed: {done.stderr.strip()[-300:]}")
    say(f"trimmed: {trimmed}.mp4 and {trimmed}.gif")


def trim_command(args: argparse.Namespace) -> int:
    """Trim an earlier recording again (after tuning DECIMATE, say)."""
    out = RECORDINGS / f"demo-{args.agent}"
    marks = out.with_suffix(".dead.json")
    if not marks.exists():
        raise DemoError(f"no recording marks at {marks}; `play --record` makes them")
    trim(out, [tuple(m) for m in json.loads(marks.read_text(encoding="utf-8"))])
    return 0


# --- the phone -------------------------------------------------------------

def console_url(given: str | None) -> str:
    url = given or os.environ.get(CONSOLE_ENV)
    if not url:
        raise DemoError(f"no console: pass --console URL or set {CONSOLE_ENV}")
    return url.rstrip("/")


def node_env() -> dict[str, str]:
    return dict(os.environ, BB_PHONE_DIR=str(PHONE_DIR))


def phone_tools() -> None:
    """Playwright under PHONE_DIR, for phone.cjs. It drives the Chrome you
    have installed, so no browser download."""
    if not shutil.which("node") or not shutil.which("npm"):
        raise DemoError("the phone needs node and npm on PATH")
    installed = PHONE_DIR / "node_modules" / "playwright" / "package.json"
    if installed.exists() and json.loads(installed.read_text())["version"] == PLAYWRIGHT:
        return
    say(f"installing Playwright in {PHONE_DIR}")
    PHONE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["npm", "install", "--prefix", str(PHONE_DIR), "--no-audit", "--no-fund",
                    f"playwright@{PLAYWRIGHT}"],
                   env=dict(os.environ, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1"), check=True)


class Phone:
    """The console at phone size, filmed while play runs (phone.cjs film).
    Each call sends one command and waits for its answer."""

    def __init__(self, url: str) -> None:
        phone_tools()
        self.url = url
        self.videos = WORK / "phone-video"
        shutil.rmtree(self.videos, ignore_errors=True)
        LOGS.mkdir(parents=True, exist_ok=True)
        log = (LOGS / "phone.log").open("wb")  # pylint: disable=consider-using-with  # node outlives this call
        self.proc = subprocess.Popen(  # pylint: disable=consider-using-with  # stop() waits on it
            ["node", str(PHONE_JS), "film", url, str(self.videos)], env=node_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
        ready = self._read()
        if "ready" not in ready:
            raise DemoError(f"the phone didn't start; see {LOGS / 'phone.log'}")
        # When its video starts (wall clock): the composite lines the two up.
        self.started = float(ready["started"])

    def _read(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise DemoError(f"the phone stopped; see {LOGS / 'phone.log'}")
        return json.loads(line)

    def do(self, command: str, **args: object) -> dict[str, Any]:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"do": command, **args}) + "\n")
        self.proc.stdin.flush()
        return self._read()

    def open_agent(self, agent: str) -> None:
        """The page of this run's bottled agent (its id from `bots`)."""
        rows = bb("bots", "--running", capture=True).stdout.splitlines()
        ids = [r.split("\t")[0] for r in rows if r.split("\t")[1:2] == [agent]]
        if not ids:
            raise DemoError(f"no running {agent} in `bot-bottle bots` for the phone to open")
        out = self.do("open_agent", id=ids[0], label=agent)
        if not out.get("ok"):
            raise DemoError(f"the phone couldn't open the agent: {out.get('error')}")

    def approve(self, text: str, timeout: float) -> bool:
        """Tap Allow on the card mentioning `text`; False when none came."""
        out = self.do("approve", text=text, timeout=int(timeout * 1000))
        if not out.get("ok"):
            say(f"the phone saw no card: {out.get('error')}")
        return bool(out.get("ok"))

    def stop(self) -> Path | None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps({"stop": True}) + "\n")
            self.proc.stdin.flush()
            video = self._read().get("video")
            self.proc.stdin.close()
            self.proc.wait(timeout=60)
        except (DemoError, OSError, subprocess.TimeoutExpired, ValueError) as e:
            self.proc.kill()
            say(f"the phone didn't stop cleanly ({e}); see {LOGS / 'phone.log'}")
            return None
        return Path(video) if video else None


# The composite: the terminal on a laptop, the phone on a phone, side by
# side at 1920x1080. The outline is drawn once (phone.cjs frame) with the
# screens cut out, and laid over the two recordings.
CANVAS = (1920, 1080)
LAPTOP_SCREEN = (140, 180, 1116, 684)   # x, y, w, h: VHS's 1240x760 at 0.9
PHONE_SCREEN = (1440, 118, 390, 844)    # the phone's viewport, in CSS px
FRAME = """<!doctype html><html><body style="margin:0;background:transparent">
<svg style="display:block" xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">
 <defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="#1b1e24"/><stop offset="1" stop-color="#0d0f13"/></linearGradient></defs>
 <path fill="url(#bg)" fill-rule="evenodd" d="M0 0H{W}V{H}H0Z {ls} {ps}"/>
 <path fill="#2b2f36" stroke="#4a505a" stroke-width="2" fill-rule="evenodd"
  d="{lid} {ls}"/>
 <path fill="#3a3f47" stroke="#4a505a" stroke-width="2"
  d="M{bx0} {by0}H{bx1}L{bx2} {by1}Q{bx2} {by2} {bx3} {by2}H{bx4}Q{bx5} {by2} {bx5} {by1}Z"/>
 <rect x="{nx}" y="{by0}" width="160" height="8" rx="4" fill="#23262c"/>
 <path fill="#16181c" stroke="#4a505a" stroke-width="3" fill-rule="evenodd" d="{body} {ps}"/>
 <rect x="{ix}" y="{iy}" width="110" height="30" rx="15" fill="#000"/>
</svg></body></html>"""


def _rect(x: float, y: float, w: float, h: float, r: float) -> str:
    """An SVG path for a rounded rectangle."""
    return (f"M{x + r} {y}H{x + w - r}Q{x + w} {y} {x + w} {y + r}V{y + h - r}"
            f"Q{x + w} {y + h} {x + w - r} {y + h}H{x + r}Q{x} {y + h} {x} {y + h - r}"
            f"V{y + r}Q{x} {y} {x + r} {y}Z")


def frame_png() -> Path:
    W, H = CANVAS
    lx, ly, lw, lh = LAPTOP_SCREEN
    px, py, pw, ph = PHONE_SCREEN
    by0 = ly + lh + 30
    html = FRAME.format(
        W=W, H=H, ls=_rect(lx, ly, lw, lh, 4), ps=_rect(px, py, pw, ph, 44),
        lid=_rect(lx - 30, ly - 30, lw + 60, lh + 60, 22),
        bx0=lx - 50, by0=by0, bx1=lx + lw + 50, bx2=lx + lw + 80, by1=by0 + 22, by2=by0 + 34,
        bx3=lx + lw + 64, bx4=lx - 64, bx5=lx - 80, nx=lx + lw / 2 - 80,
        body=_rect(px - 16, py - 16, pw + 32, ph + 32, 58), ix=px + pw / 2 - 55, iy=py + 12)
    source, png = WORK / "frame.html", WORK / "frame.png"
    source.write_text(html, encoding="utf-8")
    done = subprocess.run(["node", str(PHONE_JS), "frame", str(source), str(png), str(W), str(H)],
                          env=node_env(), capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise DemoError(f"drawing the outline failed: {done.stderr.strip()[-300:]}")
    return png


def composite(terminal: Path, phone: Path, offset: float, out: Path) -> None:
    """`<out>.mp4`: `terminal` (an mp4 from VHS) on the laptop and `phone`
    (Playwright's webm) on the phone. `offset` is when the phone's video
    started, in seconds after the terminal's."""
    W, H = CANVAS
    lx, ly, lw, lh = LAPTOP_SCREEN
    px, py, pw, ph = PHONE_SCREEN
    shift = (f"trim=start={-offset:.3f},setpts=PTS-STARTPTS" if offset < 0
             else f"setpts=PTS-STARTPTS,tpad=start_duration={offset:.3f}:start_mode=clone")
    graph = (f"color=c=#0d0f13:s={W}x{H}:r=30[bg];"
             f"[0:v]fps=30,scale={lw}:{lh}[t];"
             f"[1:v]fps=30,{shift},scale={pw}:{ph}:flags=lanczos[p];"
             f"[bg][t]overlay={lx}:{ly}:shortest=1[a];"
             f"[a][p]overlay={px}:{py}:eof_action=repeat[b];"
             f"[b][2:v]overlay=0:0:shortest=1,format=yuv420p[v]")
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(terminal), "-i", str(phone),
         "-loop", "1", "-i", str(frame_png()), "-filter_complex", graph, "-map", "[v]",
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", f"{out}.mp4"],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise DemoError(f"the composite failed: {done.stderr.strip()[-300:]}")
    say(f"composite: {out}.mp4")


# --- commands --------------------------------------------------------------

def setup(args: argparse.Namespace) -> int:
    """Once per host. Each step is skipped when it's already done."""
    if not shutil.which("tmux"):
        raise DemoError("tmux isn't on PATH")
    say(f"environment `{ENV_NAME}`, state in {STATE}")
    doctor = bb("doctor", check=False, capture=True)
    report = doctor.stdout + doctor.stderr
    if "host firewall: NOT installed" in report:
        say("the backend needs its one-time host setup for this environment:")
        bb("install", "--backend=macos-container", check=False)
        say("run the sudo lines above, then `setup` again")
        return 1
    if not all((STATE / "agents" / f"{a}.md").exists() for a in AGENTS):
        say("writing the starters (pi signs in to OpenRouter when it asks)")
        bb("setup")
    missing = [a for a in AGENTS if not (STATE / "agents" / f"{a}.md").exists()]
    if missing:
        raise DemoError(f"setup didn't write a starter for {', '.join(missing)}")
    say("starting the controller, orchestrator and gateway")
    bb("up", *(["--build"] if _from_source() else []))
    if args.phone:
        setup_phone(console_url(args.console))
    say("ready: `python3 scripts/demo/demo.py play" + (" --phone`" if args.phone else "`"))
    return 0


_PAIR_URL = re.compile(r"^\s*or open (\S+)\s*$")
_PAIR_CODE = re.compile(r"enter this code on your phone:\s*(\S+)")


def setup_phone(url: str) -> None:
    """Pair the demo environment with the console as a new account that is
    just the phone's own passkey: `bot-bottle pair` prints the pairing URL
    and the confirm code, and phone.cjs takes the pair page's new-account
    path with a virtual authenticator. Nobody's own passkey is asked for."""
    phone_tools()
    if console_linked() and PASSKEY.exists():
        say("the demo environment is already paired, with the phone's own passkey")
        return
    if console_linked():
        raise DemoError("the demo environment is paired with a console, but not with the "
                        "phone's own passkey: `teardown --purge`, then `setup --phone` again")
    # A fresh profile: a session left from another account would pair the
    # host into that account instead of the demo's own.
    shutil.rmtree(PHONE_DIR / "profile", ignore_errors=True)
    say(f"pairing the demo environment with {url}, as a new passkey account")
    LOGS.mkdir(parents=True, exist_ok=True)
    write_shim()
    with (LOGS / "pair.log").open("w") as log, subprocess.Popen(
            [str(BIN / "bot-bottle"), "pair", "--console-url", url, "--label", "bot-bottle demo"],
            env=demo_env(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.PIPE,
            text=True) as pairing:
        assert pairing.stderr is not None
        pair_url = code = ""
        for line in pairing.stderr:
            log.write(line)
            log.flush()
            if m := _PAIR_URL.match(line):
                pair_url = m.group(1)
            if m := _PAIR_CODE.search(line):
                code = m.group(1)
            if pair_url and code:
                break
        if not (pair_url and code):
            pairing.wait()
            log.flush()
            said = (LOGS / "pair.log").read_text().strip().splitlines()
            raise DemoError("`bot-bottle pair` printed no pairing URL and code"
                            + (f": {said[-1]}" if said else f"; see {LOGS / 'pair.log'}"))
        phone = subprocess.run(["node", str(PHONE_JS), "pair", url, pair_url, code],
                               env=node_env(), capture_output=True, text=True, check=False,
                               timeout=180)
        log.write(phone.stderr)
        if phone.returncode != 0:
            pairing.kill()
            raise DemoError(f"the phone couldn't pair: {phone.stderr.strip()[-300:]}")
        log.write(pairing.stderr.read())  # what pair says once it's approved
        if pairing.wait(timeout=120) != 0:
            raise DemoError(f"`bot-bottle pair` failed; see {LOGS / 'pair.log'}")
    say("paired: the console has a new account for the demo, signed in on the phone")


def console_linked() -> bool:
    """`status` says `console link to <console>: on|connected…` once paired,
    `console link: off` before, and `…: refused, not paired anymore` once the
    console forgot the host (its account was deleted): that one pairs again."""
    status = bb("status", check=False, capture=True).stdout
    return bool(re.search(r"^console link\b[^:\n]*: (?!off|refused)", status, re.MULTILINE))


def _from_source() -> bool:
    """A checkout has no published infra images; build them (install.md)."""
    return not os.environ.get("BOT_BOTTLE_DEMO_CLI")


def play(args: argparse.Namespace) -> int:
    agent = args.agent
    if not (STATE / "agents" / f"{agent}.md").exists():
        raise DemoError(f"the demo environment has no {agent} starter: run `setup` first")
    if session_alive():
        tmux("kill-session", "-t", SESSION)
    say("stopping the last run's bottled agents")
    bb("cleanup", "bots", check=False, capture=True, answer="y\n")
    reset_state(agent, args.model)
    fake_key()
    fresh_project()
    write_shim()
    trust_project()
    warm_architect()
    write_shim()
    term = Terminal(args.pace)
    term.open()
    say(f"watch it: python3 {Path(__file__).relative_to(REPO)} watch")
    term.wait(PROMPT, 10, "the shell")
    phone = None
    if args.phone:
        if not console_linked():
            raise DemoError("the demo environment isn't linked to a console: `setup --phone`")
        phone = Phone(console_url(args.console))
    recorder = Recorder(agent) if args.record else None
    try:
        if recorder:
            recorder.start(term)
        return _drive(term, agent, args, phone)
    finally:
        if phone:
            video = phone.stop()
            if recorder and video:
                recorder.phone_video = video
                recorder.phone_offset = phone.started - term.t0_wall
        if recorder:
            recorder.stop(args.linger, term.dead)


def _drive(term: Terminal, agent: str, args: argparse.Namespace, phone: Phone | None) -> int:
    term.run(f"bot-bottle start {agent} --cwd")
    term.wait(TASK_READY, 120, "the task step")
    time.sleep(1)
    term.paste(TASK)
    time.sleep(1.5)  # the task, read before it's sent
    term.key("Enter")
    term.dead_from()
    term.wait(REVIEW_READY, 300, "the architect's proposal")
    term.dead_to()
    time.sleep(args.linger)  # let the proposal be read
    say(f"the proposal: {term.save('proposal')}")
    term.key("Enter")
    say("launching; the agent takes over the screen")
    # The launch, the agent's work and the quiet after it: on the trimmed
    # copy, only what changes the screen is kept.
    term.dead_from()
    term.wait(AGENT_READY.format(agent=agent), 300, "the agent's terminal")
    if phone:
        phone.open_agent(agent)
    term.settle(args.idle, args.agent_timeout, "the agent to finish")
    term.dead_to()
    # The check: `!` (pi's and Claude Code's shell prefix) runs the tests in
    # the bottle as a plain command, so the result is the workspace's, not
    # what the model says.
    term.shell(CHECK)
    try:
        text = term.wait(TESTS_RAN, 60, "the test run")
    finally:
        path = term.save("final")
        say(f"last screen: {path}")
    if not re.search(TESTS_PASS, text, re.MULTILINE):
        raise DemoError(f"the agent stopped with the tests still failing ({path})")
    say("the tests pass in the bottle")
    for i, (what, command, expect) in enumerate(beats()):
        time.sleep(args.beat)
        say(what)
        term.shell(command)
        term.wait_new(expect, 90, what, command)
        if i == 0 and phone:
            _supervise(term, phone, args)
    time.sleep(args.beat)
    say(f"done; last screen: {term.save('beats')}")
    return 0


def _supervise(term: Terminal, phone: Phone, args: argparse.Namespace) -> None:
    """The blocked host, asked for: the agent requests it (supervise's
    `egress-allow`), the phone taps Allow, and the request goes through."""
    say(f"the agent asks for {ASKED_HOST}; the phone allows it")
    for attempt in (1, 2):
        time.sleep(args.beat)
        term.paste(ASK)
        time.sleep(1.5)
        term.key("Enter")
        if phone.approve(ASKED_HOST, 120):
            break
        if attempt == 2:
            raise DemoError(f"no approval card for {ASKED_HOST} reached the phone; "
                            f"the screen is in {term.save('supervise')}")
    term.settle(5, 120, "the agent to take the answer")
    time.sleep(args.beat)
    command, expect = ALLOWED
    say(f"{ASKED_HOST}, allowed")
    term.shell(command)
    term.wait_new(expect, 90, f"{ASKED_HOST}, allowed", command)


def beats() -> list[tuple[str, str, str]]:
    """What the demo tries from inside the bottle once the work is done, each
    a plain command (`!`), and what the screen shows when the bottle stops
    it: (what, command, regex for its output)."""
    token = fake_token()
    return [
        ("a host the bottle doesn't allow",
         "curl -sS https://example.com",
         r"403|[Bb]locked|not allowed|denied"),
        ("the API the bottle allows",
         'curl -sS -o /dev/null -w "httpbin.org: %{http_code}\\n" https://httpbin.org/get',
         r"httpbin\.org: 200"),
        ("a token sent to the allowed API",
         f'curl -sS -X POST https://httpbin.org/post -d "token={token}"',
         r"403|[Bb]locked|DLP|refused"),
        ("a token committed and pushed",
         f"printf 'export const token = \"%s\";\\n' {token} > config.js && git add config.js"
         ' && git commit -qm "add config" && git push origin HEAD',
         r"[Rr]ejected|gitleaks|secret"),
    ]


def watch(_args: argparse.Namespace) -> int:
    if not session_alive():
        raise DemoError("no demo session; `play` starts one")
    os.execvp("tmux", ["tmux", "-L", SOCKET, "attach", "-r", "-t", SESSION])


def teardown(args: argparse.Namespace) -> int:
    if session_alive():
        tmux("kill-session", "-t", SESSION)
    bb("cleanup", "bots", check=False, answer="y\n")
    bb("down", check=False)
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    if args.purge:
        bb("envs", "rm", ENV_NAME, check=False)
        shutil.rmtree(WORK, ignore_errors=True)
    say("torn down" + (" and purged" if args.purge else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    summary = (__doc__ or "").split("\n\n", maxsplit=1)[0]
    parser = argparse.ArgumentParser(prog="demo.py", description=summary)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="once per host: the demo environment and its starters")
    s.add_argument("--phone", action="store_true",
                   help="also pair with a console, as the demo's own passkey account")
    p = sub.add_parser("play", help="run the demo")
    p.add_argument("--agent", choices=AGENTS, default="claude",
                   help="the starter to run: Claude Code, or pi on a free OpenRouter model")
    p.add_argument("--model", default=MODEL, help="the free OpenRouter model pi runs on (pi only)")
    p.add_argument("--record", action="store_true",
                   help="film it with VHS: an mp4 (and a trimmed mp4 and gif) under ~/.cache/bot-bottle-demo")
    p.add_argument("--pace", type=float, default=0.03, help="seconds per typed character")
    p.add_argument("--linger", type=float, default=4.0, help="seconds on the proposal")
    p.add_argument("--beat", type=float, default=2.0,
                   help="seconds between the egress and git-gate beats")
    p.add_argument("--agent-timeout", type=float, default=600.0)
    p.add_argument("--phone", action="store_true",
                   help="film the console at phone size too; the agent asks for the blocked "
                        "host and the phone allows it (with --record, a laptop-and-phone video)")
    p.add_argument("--idle", type=float, default=30.0,
                   help="seconds the agent's screen stays still before it counts as done")
    for parser_ in (s, p):
        parser_.add_argument("--console", help=f"the console's URL (or {CONSOLE_ENV})")
    sub.add_parser("watch", help="join the session play drives (read-only)")
    tr = sub.add_parser("trim", help="squeeze the dead time out of the last recording again")
    tr.add_argument("--agent", choices=AGENTS, default="claude")
    t = sub.add_parser("teardown", help="stop the demo")
    t.add_argument("--purge", action="store_true", help="also remove the demo environment")
    args = parser.parse_args(argv)
    handler = {"setup": setup, "play": play, "watch": watch, "teardown": teardown,
               "trim": trim_command}[args.command]
    try:
        return handler(args)
    except DemoError as e:
        print(f"demo: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

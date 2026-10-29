"""bot-bottle pair: link this host to your phone.

Prints a QR code and a confirm code. Scan it, type the code and approve with a
passkey; the first pairing creates an account that is just that passkey, and
later hosts join it. Then remote access is turned on.

Usage:
  bot-bottle pair [--console-url URL] [--label LABEL]

Flags:
  --console-url URL   Console to pair with (or BB_CONSOLE_URL, or the console
                      this host last logged in to)
  --label LABEL       Host name shown on the phone (default: hostname)

`bb login` does the same without a QR code, for headless setups.
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Any

from ...control.hostctl import remote
from ...lib import qrcodegen
from . import login
from .remote import cmd_remote

_QUIET_ZONE = 2  # modules of white border


def render_qr(text: str, ansi: bool = True) -> str:
    """`text` as a QR code in half-block characters, two module rows per line.
    With `ansi`, drawn black on white regardless of the terminal's colors,
    since many scanners can't read inverted codes."""
    qr = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
    lo, hi = -_QUIET_ZONE, qr.get_size() + _QUIET_ZONE
    glyphs = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
    lines = []
    for y in range(lo, hi, 2):
        row = "".join(glyphs[(qr.get_module(x, y), qr.get_module(x, y + 1))]
                      for x in range(lo, hi))
        lines.append(f"\x1b[30;47m{row}\x1b[0m" if ansi else row)
    return "\n".join(lines)


def _console_url(argv: list[str]) -> str:
    url = login.flag(argv, "--console-url") or os.environ.get("BB_CONSOLE_URL", "")
    if not url:
        try:
            url = str(remote.load_console().get("url", ""))
        except remote.RemoteError:
            url = ""
    return url.rstrip("/")


def _show(console_url: str, ansi: bool):
    def show(resp: dict[str, Any]) -> None:
        url = resp.get("pair_url") or f"{console_url}/pair?code={resp['user_code']}"
        owned = ("This machine is already paired: sign in on your phone with the "
                 "passkey for its account.\n\n" if resp.get("owned") else "")
        sys.stderr.write(
            "\nScan this with your phone:\n\n"
            f"{render_qr(url, ansi)}\n\n"
            f"  or open {url}\n\n"
            f"{owned}"
            f"Then enter this code on your phone:  {resp['confirm_code']}\n\n"
        )
    return show


def cmd_pair(argv: list[str]) -> int:
    if "--help" in argv or "-h" in argv:
        sys.stderr.write(__doc__ or "")
        return 0
    console_url = _console_url(argv)
    if not console_url:
        sys.stderr.write("bot-bottle pair: --console-url or BB_CONSOLE_URL is required\n")
        return 1
    try:
        node_id = remote.node_id()  # remote access needs iroh; check before pairing
    except remote.RemoteError as e:
        sys.stderr.write(f"bot-bottle pair: {e}\n")
        return 1
    label = login.flag(argv, "--label") or socket.gethostname()
    # The node id tells the console which machine this is: a re-pair can then
    # only be approved by the host's owner (the console's pairing PRD).
    status = login.run_device_flow(console_url, label, _show(console_url, sys.stderr.isatty()),
                                   confirm=True, prog="bot-bottle pair", node_id=node_id)
    if status != 0:
        return status
    return cmd_remote(["enable"])

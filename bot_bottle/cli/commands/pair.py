"""bot-bottle pair — link this host to your phone.

Prints a QR code and a short confirm code. Scan the QR code with your phone,
type the confirm code, and approve with a passkey (Face ID, Touch ID, …). The
first pairing makes an account that is just that passkey; pairing another
host from the same phone adds it to the same account. Then remote access is
turned on, so the phone can reach this host's agents.

Usage:
  bot-bottle pair [--console-url URL] [--label LABEL]

Flags:
  --console-url URL   Console to pair with (or BB_CONSOLE_URL, or the console
                      this host last logged in to)
  --label LABEL       Host name shown on the phone (default: hostname)

`bb login` does the same with an existing console account and no QR code,
for headless and CI setups.
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

_QUIET_ZONE = 2  # modules of white border; scanners need some


def render_qr(text: str, ansi: bool = True) -> str:
    """``text`` as a QR code made of half-block characters, two rows of
    modules per line. With ``ansi``, it's drawn black on white whatever the
    terminal's colors are, since many phone scanners can't read inverted codes."""
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
        sys.stderr.write(
            "\nScan this with your phone:\n\n"
            f"{render_qr(url, ansi)}\n\n"
            f"  or open {url}\n\n"
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
        remote.node_id()  # remote access needs iroh; find out before pairing
    except remote.RemoteError as e:
        sys.stderr.write(f"bot-bottle pair: {e}\n")
        return 1
    label = login.flag(argv, "--label") or socket.gethostname()
    status = login.run_device_flow(console_url, label, _show(console_url, sys.stderr.isatty()),
                                   confirm=True, prog="bot-bottle pair")
    if status != 0:
        return status
    return cmd_remote(["enable"])

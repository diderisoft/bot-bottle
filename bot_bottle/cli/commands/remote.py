"""The host's console link, behind `bot-bottle pair on` / `pair off` and the
link lines of `bot-bottle status` (no longer a command of its own).

  on (enable)   bind this host's iroh node id with the console and start the
                link (ADR 0007)
  off (disable) stop the link

Turning it on needs a pairing (`bot-bottle pair`) and the iroh bindings
(`pip install 'bot-bottle[remote]'`; not on Intel Macs). The host controller
keeps the pairing and picks up the change within seconds.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from ...control.hostctl import pairing
from ...control.hostctl.runner import HostControlError
from ..constants import PROG
from ..host_state_client import pairing as current_pairing, running_pairing, set_pairing


def _fail(message: str) -> int:
    sys.stderr.write(f"{PROG} pair: {message}\n")
    return 1


def _request(method: str, url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _console_detail(error: urllib.error.HTTPError) -> str:
    """The console's own explanation of a refusal (FastAPI's `detail`), if any."""
    try:
        detail = json.loads(error.read()).get("detail")
    except (OSError, ValueError, AttributeError):
        return ""
    return detail if isinstance(detail, str) else ""


def _bind(paired: pairing.Pairing, node_id: str) -> dict[str, Any]:
    """`PUT /api/v1/hosts/iroh` with the host's access token, refreshing it
    once if expired."""
    url = paired.url.rstrip("/")
    access, refresh = paired.tokens()
    try:
        return _request("PUT", f"{url}/api/v1/hosts/iroh", {"node_id": node_id}, access)
    except urllib.error.HTTPError as e:
        if e.code != 401 or not refresh:
            raise
    refreshed = _request("POST", f"{url}/api/v1/hosts/token/refresh",
                         {"refresh_token": refresh})
    access = str(refreshed["access_token"])
    set_pairing(access_token=access)
    return _request("PUT", f"{url}/api/v1/hosts/iroh", {"node_id": node_id}, access)


def _enable() -> int:
    """Bind this host's node id with the console and turn the link on. The
    host controller keeps the pairing; a refusal from it fails too."""
    try:
        return _bind_and_enable()
    except HostControlError as e:
        return _fail(getattr(e, "error", "") or str(e))


def _bind_and_enable() -> int:
    try:
        paired = current_pairing()[0].require()
        node_id = pairing.node_id()
    except pairing.RemoteError as e:
        return _fail(str(e))
    try:
        bound = _bind(paired, node_id)
    except urllib.error.HTTPError as e:
        detail = {401: "the console rejected this host's token; run `bot-bottle pair` again",
                  409: _console_detail(e) or "another host on the console already holds "
                                             "this machine's node id"}
        return _fail(detail.get(e.code, f"the console refused the binding (HTTP {e.code})"))
    except (OSError, ValueError) as e:
        return _fail(f"couldn't reach the console at {paired.url}: {e}")
    console_id = str(bound.get("console_node_id", ""))
    ticket = str(bound.get("console_ticket", ""))
    if not console_id or not ticket:
        return _fail("the console didn't return its iroh identity; is its iroh node running?")
    pinned = paired.console_node_id
    if pinned and pinned != console_id:
        return _fail(f"the console's node id changed since pairing (pinned {pinned[:10]}, "
                     f"now {console_id[:10]}); run `bot-bottle pair` again if that's expected")
    # Re-pairing this machine folds the new pairing into its original host on
    # the console; that's the host this machine is now.
    host_id = str(bound.get("host_id", "")) or paired.host_id
    if host_id != paired.host_id and paired.host_id:
        print("this machine was already paired: kept its original host on the console")
    set_pairing(console_node_id=console_id, console_ticket=ticket, host_id=host_id,
                enabled=True)
    print(f"remote access enabled: this host is {node_id[:10]}… on {paired.url}")
    print("the host controller connects within a few seconds "
          f"(`{PROG} status` to check)")
    return 0


def _disable() -> int:
    try:
        set_pairing(enabled=False)
    except HostControlError as e:
        return _fail(getattr(e, "error", "") or str(e))
    print("remote access disabled")
    return 0


def link_line() -> str:
    """One line on the console link, for `bot-bottle status`. It asks a
    running host controller and starts none: the link runs only there."""
    try:
        found = running_pairing()
    except HostControlError as e:
        return f"console link: unknown ({getattr(e, 'error', '') or e})"
    if found is None:
        return "console link: the host controller isn't running, so neither is the link"
    config, status = found[0].config, found[1]
    if not config.enabled:
        return f"console link: off (`{PROG} pair` to pair, `{PROG} pair on` to turn it on)"
    console = f" to {config.console_node_id[:10]}…" if config.console_node_id else ""
    if not status:
        return f"console link{console}: on, no report yet"
    age = int(time.time() - float(status.get("updated", 0)))
    state = ("connected" if status.get("connected")
             else "refused, not paired anymore" if status.get("unpaired") else "not connected")
    error = (f" ({status['error']})" if status.get("error") and not status.get("connected")
             else "")
    return f"console link{console}: {state}{error}, {age}s ago"


def _status() -> int:
    print(link_line())
    return 0


def cmd_remote(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    handlers = {"enable": _enable, "disable": _disable, "status": _status}
    if action not in handlers:
        sys.stderr.write(__doc__ or "")
        return 0 if action in ("-h", "--help") else 2
    return handlers[action]()

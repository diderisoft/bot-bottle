"""bot-bottle remote: let the console you logged in to reach this host.

  bot-bottle remote enable    bind this host's iroh node id with the console
                              and start the link (ADR 0007)
  bot-bottle remote disable   stop the link
  bot-bottle remote status    show whether the link is enabled and connected

`enable` needs `bot-bottle login` and the iroh bindings (`pip install
'bot-bottle[remote]'`; not on Intel Macs). The host controller picks up the
change within seconds.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from ...control.hostctl import remote
from ..constants import PROG


def _fail(message: str) -> int:
    sys.stderr.write(f"{PROG} remote: {message}\n")
    return 1


def _request(method: str, url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _bind(console: dict[str, Any], node_id: str) -> dict[str, Any]:
    """`PUT /api/v1/hosts/iroh` with the host's access token, refreshing it
    once if expired."""
    url = console["url"].rstrip("/")
    try:
        return _request("PUT", f"{url}/api/v1/hosts/iroh", {"node_id": node_id},
                        console["access_token"])
    except urllib.error.HTTPError as e:
        if e.code != 401 or not console.get("refresh_token"):
            raise
    refreshed = _request("POST", f"{url}/api/v1/hosts/token/refresh",
                         {"refresh_token": console["refresh_token"]})
    console["access_token"] = refreshed["access_token"]
    remote.save_console(console)
    return _request("PUT", f"{url}/api/v1/hosts/iroh", {"node_id": node_id},
                    console["access_token"])


def _enable() -> int:
    try:
        console = remote.load_console()
        node_id = remote.node_id()
    except remote.RemoteError as e:
        return _fail(str(e))
    try:
        bound = _bind(console, node_id)
    except urllib.error.HTTPError as e:
        detail = {401: "the console rejected this host's token; run `bot-bottle login` again",
                  409: "another host on the console already holds this node id"}
        return _fail(detail.get(e.code, f"the console refused the binding (HTTP {e.code})"))
    except (OSError, ValueError) as e:
        return _fail(f"couldn't reach the console at {console['url']}: {e}")
    console_id = str(bound.get("console_node_id", ""))
    ticket = str(bound.get("console_ticket", ""))
    if not console_id or not ticket:
        return _fail("the console didn't return its iroh identity; is its iroh node running?")
    pinned = str(console.get("console_node_id", ""))
    if pinned and pinned != console_id:
        return _fail(f"the console's node id changed since login (pinned {pinned[:10]}, "
                     f"now {console_id[:10]}); run `bot-bottle login` again if that's expected")
    if not pinned:
        console["console_node_id"] = console_id
    console["console_ticket"] = ticket
    remote.save_console(console)
    remote.save_config(remote.RemoteConfig(
        enabled=True, console_node_id=console_id, console_ticket=ticket))
    print(f"remote access enabled: this host is {node_id[:10]}… on {console['url']}")
    print("the host controller connects within a few seconds "
          f"(`{PROG} remote status` to check)")
    return 0


def _disable() -> int:
    config = remote.load_config()
    remote.save_config(remote.RemoteConfig(
        enabled=False, console_node_id=config.console_node_id,
        console_ticket=config.console_ticket))
    print("remote access disabled")
    return 0


def _status() -> int:
    config = remote.load_config()
    print(f"remote access: {'enabled' if config.enabled else 'disabled'}")
    if config.console_node_id:
        print(f"console: {config.console_node_id[:10]}…")
    status = remote.read_status()
    if config.enabled and status:
        age = int(time.time() - float(status.get("updated", 0)))
        state = "connected" if status.get("connected") else "not connected"
        error = (f" ({status['error']})"
                 if status.get("error") and not status.get("connected") else "")
        print(f"link: {state}{error}, {age}s ago")
    elif config.enabled:
        print("link: no report yet; is the host controller running?")
    return 0


def cmd_remote(argv: list[str]) -> int:
    action = argv[0] if argv else ""
    handlers = {"enable": _enable, "disable": _disable, "status": _status}
    if action not in handlers:
        sys.stderr.write(__doc__ or "")
        return 0 if action in ("-h", "--help") else 2
    return handlers[action]()

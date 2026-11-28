"""Unit: the console link's relay (`link_relay`): a console that runs its own
relay names it, and the link makes it its home relay; otherwise iroh's."""

from __future__ import annotations

import http.server
import json
import threading
import unittest
from typing import Any

from bot_bottle.control.hostctl import link_relay


class _Console:
    """A console answering GET /api/v1/iroh/relay with `body` (and `status`)."""

    def __init__(self, body: Any, status: int = 200) -> None:
        reply = body if isinstance(body, bytes) else json.dumps(body).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server's name)
                self.server.paths.append(self.path)  # type: ignore[attr-defined]
                self.send_response(status)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.paths = []  # type: ignore[attr-defined]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ConsoleRelay(unittest.TestCase):
    def ask(self, body: Any, status: int = 200, path: str = "") -> str:
        console = _Console(body, status)
        self.addCleanup(console.close)
        relay = link_relay.console_relay(console.url + path, timeout=2)
        self.assertEqual([link_relay.RELAY_PATH], console.server.paths)  # type: ignore[attr-defined]
        return relay

    def test_the_consoles_relay(self) -> None:
        self.assertEqual("https://relay.example/",
                         self.ask({"relay_url": "https://relay.example/"}))
        self.assertEqual("https://relay.example/",
                         self.ask({"relay_url": "https://relay.example/"}, path="/"))

    def test_irohs_when_the_console_names_none_or_cant_say(self) -> None:
        for body, status in (({"relay_url": None}, 200), ({}, 200), (b"not json", 200),
                             ({"relay_url": "ftp://x"}, 200), ({"relay_url": 5}, 200),
                             ({"detail": "Not Found"}, 404), ([], 200)):
            with self.subTest(body=body, status=status):
                self.assertEqual("", self.ask(body, status))

    def test_no_console_to_ask(self) -> None:
        self.assertEqual("", link_relay.console_relay(""))
        self.assertEqual("", link_relay.console_relay("file:///etc/passwd"))
        self.assertEqual("", link_relay.console_relay("http://127.0.0.1:9", timeout=1))


class _Iroh:
    """The parts of the iroh module `endpoint_options` uses."""

    class RelayMode:
        @staticmethod
        def custom_from_urls(urls: list[str]) -> tuple[str, tuple[str, ...]]:
            return ("custom", tuple(urls))

    @staticmethod
    def preset_n0() -> str:
        return "n0"

    @staticmethod
    def EndpointOptions(**kw: Any) -> dict[str, Any]:  # noqa: N802 (iroh's name)
        return kw


class EndpointOptions(unittest.TestCase):
    def test_the_relay_is_the_only_one(self) -> None:
        options = link_relay.endpoint_options(_Iroh, "https://relay.example/", b"k" * 32)
        self.assertEqual(("custom", ("https://relay.example/",)), options["relay_mode"])
        self.assertEqual(("n0", b"k" * 32, []),
                         (options["preset"], options["secret_key"], options["alpns"]))

    def test_irohs_without_one(self) -> None:
        options = link_relay.endpoint_options(_Iroh, "", b"k" * 32)
        self.assertNotIn("relay_mode", options)


if __name__ == "__main__":
    unittest.main()

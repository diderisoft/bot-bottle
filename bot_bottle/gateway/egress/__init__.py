"""Gateway-side (data-plane) egress service: the mitmproxy addon and its
pure decision core, DLP detectors, and route/DLP config parsing.

These are the long-running data-plane pieces (loaded by mitmdump inside the
`bot-bottle-gateway` image), distinct from the host-side `bot_bottle.egress`
service that renders routes and prepares env. Import the concrete modules
directly (`from bot_bottle.gateway.egress.addon_core import ...`) — this
package deliberately does no eager work so leaf imports stay cheap.
"""

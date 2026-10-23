"""Gateway-side (data-plane) supervise service: the supervise daemon's HTTP
server.

The host-side supervise control lives in `bot_bottle.orchestrator.supervisor`;
this is the in-gateway daemon the agents reach. Import the concrete module
directly (`from bot_bottle.gateway.supervisor.server import ...`).
"""

"""Gateway-side (data-plane) git-gate service: pure host-side hook rendering
(PRD 0008) and the smart-HTTP backend that fronts git-gate repos.

The host-side git-gate service (provisioning, preflight) lives in
`bot_bottle.git_gate`; this is the data-plane counterpart. Import the concrete
modules directly (`from bot_bottle.gateway.git_gate.render import ...`).
"""

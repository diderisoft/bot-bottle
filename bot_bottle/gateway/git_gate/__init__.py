"""Gateway-side (data-plane) git-gate service: the smart-HTTP backend that
fronts git-gate repos.

The host side lives in `bot_bottle.git_gate` (provisioning, preflight) and
`bot_bottle.control.git_gate_render` (config and hook rendering).
"""

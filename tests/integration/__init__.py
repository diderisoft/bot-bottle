"""Integration-test package init.

The suite brings the orchestrator and gateway up and launches bottles in this
process, as the host controller does for real (`bot-bottle up` and launches
run there): bring-up records infrastructure state and a launch records its
bottled agent, and only the host controller writes either (ADR 0020). So the
test process is the host controller and uses the host DB's stores, under the
active environment's root.
"""

from bot_bottle import host_access as _host_access

_host_access.use_stores()

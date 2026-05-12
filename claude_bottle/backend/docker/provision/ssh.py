"""Set up SSH inside a running Docker bottle.

This is the most involved provisioner. The end state in the container:
  - ~/.ssh/config + ~/.ssh/known_hosts owned by node, mode 600
  - ssh-agent running as root with each key loaded; agent socket at
    /run/claude-bottle-agent.sock
  - socat forwarder (also root) bridging the agent socket to
    /run/claude-bottle-agent-public.sock (mode 666) so node can talk
    to the agent despite ssh-agent's SO_PEERCRED UID match
  - on-disk key files deleted after `ssh-add`; the bytes only live in
    the agent process's memory thereafter

See the `provision_ssh` docstring for the full isolation rationale."""

from __future__ import annotations

import os
import subprocess

from ....log import die, info
from ....util import expand_tilde
from .. import util as docker_mod
from ..bottle_plan import DockerBottlePlan
from ..ssh_gate import ssh_gate_host


def provision_ssh(plan: DockerBottlePlan, target: str) -> None:
    """Set up SSH in the container so node can authenticate using
    each entry's key without the key file being readable by node.
    No-op when the bottle has no SSH entries.

    Isolation strategy:
      - Keys live at /root/.claude-bottle-keys/ (mode 700,
        root-owned). /root is mode 700 in node:22-slim, so node
        (uid 1000) can't even traverse in.
      - ssh-agent runs as root, listening on
        /run/claude-bottle-agent.sock. Each key is loaded with
        ssh-add, then deleted; the bytes now live only in the
        agent process's memory.
      - ssh-agent's SO_PEERCRED-based UID match rejects every
        connection whose peer euid is neither 0 nor the agent's.
        To bridge that, a root-owned socat forwarder listens on
        /run/claude-bottle-agent-public.sock (mode 666) and
        proxies bytes to the real agent socket.
      - node can't ptrace root-owned agent or socat, so
        /proc/<pid>/mem is off-limits and key bytes never leave
        root-owned memory.
      - ~/.ssh/config in node's home points each Host at the
        public socket via IdentityAgent.

    Why an in-container agent (not bind-mounted from host):
    Docker Desktop on macOS does not forward Unix-domain socket
    connect() across the VM boundary — connect() returns
    ENOTSUP. Running ssh-agent inside the container sidesteps
    that entirely.

    Limitation: keys must be passphrase-less. ssh-add prompts on
    /dev/tty for passphrases, but our docker exec has no TTY."""
    bottle = plan.spec.manifest.bottle_for(plan.spec.agent_name)
    if not bottle.ssh:
        return

    container = target
    gate_target = ssh_gate_host(plan.slug)
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_ssh = f"{container_home}/.ssh"
    agent_socket = "/run/claude-bottle-agent.sock"
    public_socket = "/run/claude-bottle-agent-public.sock"
    keys_dir = "/root/.claude-bottle-keys"

    # Per-entry listen ports come off the gate plan (PRD 0007).
    # Indexed by the bottle.ssh entry's Host alias so each ssh_config
    # block knows which port its forwarder lives on.
    upstreams_by_alias = {u.bottle_host_alias: u for u in plan.gate_plan.upstreams}
    if set(upstreams_by_alias) != {e.Host for e in bottle.ssh}:
        die(
            "ssh-gate upstream table is out of sync with bottle.ssh; "
            "this is an internal bug"
        )

    # ~/.ssh for node (700, owned by node).
    docker_mod.docker_exec_root(container, ["mkdir", "-p", container_ssh])
    docker_mod.docker_exec_root(container, ["chown", "node:node", container_ssh])
    docker_mod.docker_exec_root(container, ["chmod", "700", container_ssh])

    # /root/.claude-bottle-keys for root (700, root-owned).
    docker_mod.docker_exec_root(container, ["mkdir", "-p", keys_dir])
    docker_mod.docker_exec_root(container, ["chown", "root:root", keys_dir])
    docker_mod.docker_exec_root(container, ["chmod", "700", keys_dir])

    config_file = plan.stage_dir / "ssh_config"
    known_hosts_file = plan.stage_dir / "ssh_known_hosts"
    config_file.write_text("")
    config_file.chmod(0o600)
    known_hosts_file.write_text("")
    known_hosts_file.chmod(0o600)

    container_key_paths: list[str] = []
    for entry in bottle.ssh:
        name = entry.Host
        key = expand_tilde(entry.IdentityFile)
        hostname = entry.Hostname
        user = entry.User
        known_host_key = entry.KnownHostKey
        upstream = upstreams_by_alias[name]
        listen_port = upstream.listen_port

        key_basename = os.path.basename(key)
        container_key_path = f"{keys_dir}/{key_basename}"

        info(f"copying ssh key for '{name}' -> {container} (root-only staging)")
        subprocess.run(
            ["docker", "cp", key, f"{container}:{container_key_path}"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        docker_mod.docker_exec_root(container, ["chown", "root:root", container_key_path])
        docker_mod.docker_exec_root(container, ["chmod", "600", container_key_path])

        container_key_paths.append(container_key_path)

        # Each Host block points at the gate container + its
        # per-entry listen port. HostKeyAlias makes ssh validate
        # the host key against `hostname` (the real upstream
        # name) instead of the gate container; CheckHostIP=no
        # skips the resolved-IP lookup, which would also point at
        # the gate.
        block = (
            f"Host {name}\n"
            f"  HostName {gate_target}\n"
            f"  User {user}\n"
            f"  Port {listen_port}\n"
            f"  IdentityAgent {public_socket}\n"
            f"  HostKeyAlias {hostname}\n"
            f"  CheckHostIP no\n"
            f"\n"
        )
        with config_file.open("a") as f:
            f.write(block)

        if known_host_key:
            # HostKeyAlias makes ssh look up known_hosts under
            # `hostname` (the upstream's real name / IP literal),
            # not the gate container. One unambiguous entry per
            # ssh entry.
            with known_hosts_file.open("a") as f:
                f.write(f"{hostname} {known_host_key}\n")

    # Boot the agent, load each key, delete the key files, then
    # start the root-owned socat forwarder. One docker exec so the
    # whole sequence is atomic.
    info(f"starting in-container ssh-agent at {agent_socket} (forwarded via {public_socket})")
    setup_lines = [
        "set -eu",
        f"ssh-agent -a {agent_socket} >/dev/null",
    ]
    for kp in container_key_paths:
        setup_lines.append(f"SSH_AUTH_SOCK={agent_socket} ssh-add {kp}")
        setup_lines.append(f"rm -f {kp}")
    setup_lines.append(f"rmdir {keys_dir} 2>/dev/null || true")
    # Forwarder: socat (uid 0) connects to the agent on node's behalf.
    setup_lines.append(
        f"nohup socat UNIX-LISTEN:{public_socket},fork,reuseaddr,mode=666 "
        f"UNIX-CONNECT:{agent_socket} </dev/null >/dev/null 2>&1 &"
    )
    # Wait briefly for the forwarder to bind.
    setup_lines.extend([
        "i=0",
        "while [ $i -lt 20 ]; do",
        f"  [ -S {public_socket} ] && break",
        "  i=$((i + 1))",
        "  sleep 0.1",
        "done",
        f"[ -S {public_socket} ] || {{ echo 'claude-bottle: socat forwarder failed to bind {public_socket}' >&2; exit 1; }}",
    ])
    setup_script = "\n".join(setup_lines) + "\n"
    subprocess.run(
        ["docker", "exec", "-u", "0", container, "sh", "-c", setup_script],
        check=True,
    )

    info(f"writing {container_ssh}/config")
    subprocess.run(
        ["docker", "cp", str(config_file), f"{container}:{container_ssh}/config"],
        stdout=subprocess.DEVNULL,
        check=True,
    )
    docker_mod.docker_exec_root(container, ["chown", "node:node", f"{container_ssh}/config"])
    docker_mod.docker_exec_root(container, ["chmod", "600", f"{container_ssh}/config"])

    if known_hosts_file.stat().st_size > 0:
        info(f"writing {container_ssh}/known_hosts")
        subprocess.run(
            ["docker", "cp", str(known_hosts_file), f"{container}:{container_ssh}/known_hosts"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        docker_mod.docker_exec_root(container, ["chown", "node:node", f"{container_ssh}/known_hosts"])
        docker_mod.docker_exec_root(container, ["chmod", "600", f"{container_ssh}/known_hosts"])

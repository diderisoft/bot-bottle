"""SSH helpers. Validates ssh entries from claude-bottle.json, then sets
up SSH inside the container via a root-owned ssh-agent so the `node`
user can use the keys for SSH but cannot read the key bytes.

Why an in-container agent (not bind-mounted from host): Docker Desktop
on macOS does not forward Unix-domain socket connect() across the VM
boundary — connect() returns ENOTSUP. Running ssh-agent inside the
container sidesteps that entirely.

Isolation:
  - Keys live at /root/.claude-bottle-keys/ (mode 700, root-owned).
    /root is mode 700 in node:22-slim, so node (uid 1000) can't even
    traverse in.
  - ssh-agent runs as root, listening on /run/claude-bottle-agent.sock.
    Each key is loaded with ssh-add, then deleted; the bytes now live
    only in the agent process's memory.
  - ssh-agent's SO_PEERCRED-based UID match rejects every connection
    whose peer euid is neither 0 nor the agent's. To bridge that, a
    root-owned socat forwarder listens on
    /run/claude-bottle-agent-public.sock (mode 666) and proxies bytes
    to the real agent socket.
  - node can't ptrace root-owned agent or socat, so /proc/<pid>/mem is
    off-limits and key bytes never leave root-owned memory.
  - ~/.ssh/config in node's home points each Host at the public socket
    via IdentityAgent.

Limitation: keys must be passphrase-less. ssh-add prompts on /dev/tty
for passphrases, but our docker exec has no TTY.

Each ssh entry has keys: Host, IdentityFile, Hostname, User, Port
(required); KnownHostKey (optional).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .log import die, info


def ssh_validate_entries(entries: list[dict[str, Any]]) -> None:
    """Each entry must have Host + IdentityFile, and the IdentityFile
    must exist on the host (after expanding leading ~)."""
    for entry in entries:
        name = entry.get("Host", "")
        key = entry.get("IdentityFile", "")
        if not name:
            die(f"ssh entry missing required field 'Host': {entry}")
        if not key:
            die(f"ssh entry '{name}' missing required field 'IdentityFile'")
        key = _expand_tilde(key)
        if not os.path.isfile(key):
            die(f"ssh key file not found for host '{name}': {key}")


def ssh_setup(
    container: str,
    stage_dir: Path,
    proxy_host_port: str,
    entries: list[dict[str, Any]],
) -> None:
    """Set up SSH in the container so node can authenticate using each
    entry's key without the key file being readable by node."""
    container_home = os.environ.get("CLAUDE_BOTTLE_CONTAINER_HOME", "/home/node")
    container_ssh = f"{container_home}/.ssh"
    agent_socket = "/run/claude-bottle-agent.sock"
    public_socket = "/run/claude-bottle-agent-public.sock"
    keys_dir = "/root/.claude-bottle-keys"

    # ~/.ssh for node (700, owned by node).
    _docker_exec_root(container, ["mkdir", "-p", container_ssh])
    _docker_exec_root(container, ["chown", "node:node", container_ssh])
    _docker_exec_root(container, ["chmod", "700", container_ssh])

    # /root/.claude-bottle-keys for root (700, root-owned).
    _docker_exec_root(container, ["mkdir", "-p", keys_dir])
    _docker_exec_root(container, ["chown", "root:root", keys_dir])
    _docker_exec_root(container, ["chmod", "700", keys_dir])

    config_file = stage_dir / "ssh_config"
    known_hosts_file = stage_dir / "ssh_known_hosts"
    config_file.write_text("")
    config_file.chmod(0o600)
    known_hosts_file.write_text("")
    known_hosts_file.chmod(0o600)

    proxy_host, _, proxy_port = proxy_host_port.partition(":")

    container_key_paths: list[str] = []
    for entry in entries:
        name = entry["Host"]
        key = _expand_tilde(entry["IdentityFile"])
        hostname = entry["Hostname"]
        user = entry["User"]
        port = str(entry["Port"])
        known_host_key = entry.get("KnownHostKey", "")

        key_basename = os.path.basename(key)
        container_key_path = f"{keys_dir}/{key_basename}"

        info(f"copying ssh key for '{name}' -> {container} (root-only staging)")
        subprocess.run(
            ["docker", "cp", key, f"{container}:{container_key_path}"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        _docker_exec_root(container, ["chown", "root:root", container_key_path])
        _docker_exec_root(container, ["chmod", "600", container_key_path])

        container_key_paths.append(container_key_path)

        # ProxyCommand tunnels SSH through pipelock via HTTP CONNECT.
        # %h / %p expand to this block's HostName / Port. socat's
        # PROXY: mode does CONNECT host:port to the proxy.
        block = (
            f"Host {name}\n"
            f"  HostName {hostname}\n"
            f"  User {user}\n"
            f"  Port {port}\n"
            f"  IdentityAgent {public_socket}\n"
            f"  ProxyCommand socat - PROXY:{proxy_host}:%h:%p,proxyport={proxy_port}\n"
            f"\n"
        )
        with config_file.open("a") as f:
            f.write(block)

        if known_host_key:
            entries_to_write: list[str] = []
            if port == "22":
                entries_to_write.append(f"{name} {known_host_key}\n")
                if hostname != name:
                    entries_to_write.append(f"{hostname} {known_host_key}\n")
            else:
                entries_to_write.append(f"[{name}]:{port} {known_host_key}\n")
                if hostname != name:
                    entries_to_write.append(f"[{hostname}]:{port} {known_host_key}\n")
            with known_hosts_file.open("a") as f:
                for e in entries_to_write:
                    f.write(e)

    # Boot the agent, load each key, delete the key files, then start
    # the root-owned socat forwarder. One docker exec so the whole
    # sequence is atomic.
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
    _docker_exec_root(container, ["chown", "node:node", f"{container_ssh}/config"])
    _docker_exec_root(container, ["chmod", "600", f"{container_ssh}/config"])

    if known_hosts_file.stat().st_size > 0:
        info(f"writing {container_ssh}/known_hosts")
        subprocess.run(
            ["docker", "cp", str(known_hosts_file), f"{container}:{container_ssh}/known_hosts"],
            stdout=subprocess.DEVNULL,
            check=True,
        )
        _docker_exec_root(container, ["chown", "node:node", f"{container_ssh}/known_hosts"])
        _docker_exec_root(container, ["chmod", "600", f"{container_ssh}/known_hosts"])


def _docker_exec_root(container: str, argv: list[str]) -> None:
    subprocess.run(
        ["docker", "exec", "-u", "0", container, *argv],
        stdout=subprocess.DEVNULL,
        check=True,
    )


def _expand_tilde(path: str) -> str:
    if path.startswith("~"):
        home = os.environ.get("HOME", "")
        return home + path[1:]
    return path

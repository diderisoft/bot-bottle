#!/bin/sh
set -eu

uid="$(id -u)"
if [ "$uid" -eq 0 ]; then
    echo "refusing to run the guest container engine as root" >&2
    exit 1
fi

# Check each piece of podman 5's networking: without pasta nothing starts,
# without nft there's no compose bridge, without aardvark-dns nested DNS
# fails.
for command in podman docker fuse-overlayfs pasta nft slirp4netns; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "missing nested-container prerequisite: $command" >&2
        exit 1
    }
done

# No subordinate ranges: one would send podman to newuidmap, which needs
# CAP_SYS_ADMIN here. An empty range keeps the single-UID self-mapping.
if grep -q "^$(id -un):" /etc/subuid 2>/dev/null; then
    echo "unexpected subordinate UID range for $(id -un): podman would" >&2
    echo "require CAP_SYS_ADMIN via newuidmap in this guest" >&2
    exit 1
fi

for device in /dev/fuse /dev/net/tun; do
    [ -r "$device" ] && [ -w "$device" ] || {
        echo "device $device is not readable/writable by $(id -un)" >&2
        exit 1
    }
done

# Short: conmon's attach socket must fit in a 108-byte sun_path (see
# nested_containers.py). Must match AGENT_CA_BUNDLE in backend/util.py (a unit
# test checks).
CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
[ -r "$CA_BUNDLE" ] || {
    echo "gateway CA bundle $CA_BUNDLE is missing or unreadable" >&2
    exit 1
}

# The agent's proxy URL names `bot-bottle-gateway`, which resolves only via
# this bottle's /etc/hosts, not a nested container's. containers.conf
# `hosts_file` doesn't apply to the Docker-compatible API, so resolve it here
# and give nested containers the address.
GATEWAY_NAME="bot-bottle-gateway"
gateway_ip="$(
    awk -v name="$GATEWAY_NAME" '$2 == name { print $1; exit }' /etc/hosts
)"
[ -n "$gateway_ip" ] || {
    echo "no /etc/hosts entry for $GATEWAY_NAME; the gateway address is" >&2
    echo "needed so nested containers can reach the egress proxy" >&2
    exit 1
}

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/bbp}"
config="$HOME/.config/containers"
mkdir -p "$XDG_RUNTIME_DIR" "$config"
chmod 700 "$XDG_RUNTIME_DIR"

# Required: a single-UID mapping has no other owners for layers to chown to.
cat > "$config/storage.conf" <<'CONF'
[storage]
driver="overlay"
[storage.options.overlay]
mount_program="/usr/bin/fuse-overlayfs"
ignore_chown_errors="true"
CONF

# No cgroup delegation reaches this guest, and no journald socket
# (events_logger=file).
#
#   hosts_file   native `podman run` only; the compat API ignores it.
#   volumes/env  the bottle's CA bundle, so TLS through the gateway verifies
#                for curl, wget, python and node.
#
# The proxy URL carries the identity token, which podman already forwards from
# the agent's env; the 0600 file in this disposable VM exposes it to no one
# new.
CA_BUNDLE="$CA_BUNDLE" GATEWAY_NAME="$GATEWAY_NAME" GATEWAY_IP="$gateway_ip" \
CONTAINERS_CONF="$config/containers.conf" python3 - <<'PY'
import os
from pathlib import Path

ca = os.environ["CA_BUNDLE"]
name = os.environ["GATEWAY_NAME"]
ip = os.environ["GATEWAY_IP"]

entries = [
    f"SSL_CERT_FILE={ca}",
    f"CURL_CA_BUNDLE={ca}",
    f"REQUESTS_CA_BUNDLE={ca}",
    f"NODE_EXTRA_CA_CERTS={ca}",
]
# Nested containers don't inherit /etc/hosts: give them the address.
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    value = os.environ.get(var)
    if value:
        entries.append(f"{var}={value.replace(name, ip)}")
# NO_PROXY keeps the name: clients still ask for "bot-bottle-gateway".
for var in ("NO_PROXY", "no_proxy"):
    value = os.environ.get(var)
    if value:
        entries.append(f"{var}={value}")

path = Path(os.environ["CONTAINERS_CONF"])
path.write_text("\n".join([
    "[containers]",
    'cgroups="disabled"',
    # podman copies the host's proxy vars into containers, overriding the env
# below with the unresolvable name. Turn that off.
    "http_proxy=false",
    'hosts_file="/etc/hosts"',
    f'volumes=["{ca}:{ca}:ro"]',
    "env=[",
    *[f'  "{entry}",' for entry in entries],
    "]",
    "[engine]",
    'cgroup_manager="cgroupfs"',
    'events_logger="file"',
    "",
]), encoding="utf-8")
path.chmod(0o600)
PY

# Registry pulls go through the bottle's proxy too; the token-bearing URL is
# already in the agent's env.
GATEWAY_NAME="$GATEWAY_NAME" GATEWAY_IP="$gateway_ip" python3 - <<'PY'
import json
import os
from pathlib import Path

name = os.environ["GATEWAY_NAME"]
ip = os.environ["GATEWAY_IP"]

proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy", "")
# NO_PROXY keeps the name: it is matched against what a client asks for, and
# code inside a nested container still says "bot-bottle-gateway".
no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy", "")
config = {"proxies": {"default": {
    "httpProxy": proxy.replace(name, ip),
    "httpsProxy": proxy.replace(name, ip),
    "noProxy": no_proxy,
}}}
path = Path.home() / ".docker" / "config.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(config), encoding="utf-8")
path.chmod(0o600)
PY

if docker info >/dev/null 2>&1; then
    exit 0
fi

# Also for the podman service's env, used where the Docker CLI's config
# doesn't apply (podman's own pulls, API clients other than the CLI). Never
# echoed: these carry the identity token.
for var in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
    eval "value=\${$var:-}"
    [ -n "$value" ] || continue
    eval "export $var=\"\${value%%$GATEWAY_NAME*}$gateway_ip\${value#*$GATEWAY_NAME}\""
done

log=/tmp/bot-bottle-nested-containers.log
nohup podman system service --time=0 \
    "unix://$XDG_RUNTIME_DIR/podman.sock" \
    >"$log" 2>&1 </dev/null &

#!/bin/sh
set -eu

uid="$(id -u)"
if [ "$uid" -eq 0 ]; then
    echo "refusing to run the guest container engine as root" >&2
    exit 1
fi

# Every piece of podman 5's networking stack is checked here, because each
# one fails at a different and misleading layer if it is absent: no pasta and
# nothing starts at all; no nft and netavark cannot build the bridge every
# compose file expects; no aardvark-dns and DNS inside nested containers fails
# while everything else looks healthy.
for command in podman docker fuse-overlayfs pasta nft slirp4netns; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "missing nested-container prerequisite: $command" >&2
        exit 1
    }
done

# The inverse of the rootless-Docker check, and the whole point of the podman
# variant: a subordinate range would push podman onto newuidmap, which cannot
# write a multi-range uid_map without CAP_SYS_ADMIN in this guest.  An empty
# range keeps it on the single-UID self-mapping an unprivileged process may
# write itself.
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

# Short by necessity, not by accident: conmon's attach socket lives under
# this directory and must fit in a 108-byte sun_path. See nested_containers.py.
# Must stay in step with AGENT_CA_BUNDLE in bot_bottle/backend/util.py; a unit
# test pins the two together.
CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
[ -r "$CA_BUNDLE" ] || {
    echo "gateway CA bundle $CA_BUNDLE is missing or unreadable" >&2
    exit 1
}

# The proxy URL the agent inherits names `bot-bottle-gateway`, which resolves
# only through this bottle's /etc/hosts. A nested container gets its own hosts
# file, so it cannot resolve the name and dies at "Could not resolve proxy".
#
# podman's containers.conf `hosts_file` would fix that, except the
# Docker-compatible API ignores it — it only takes effect for native
# `podman run`, and the agent types `docker`. So the name is resolved *here*
# and the address, not the name, goes into the proxy URL the nested container
# receives. Verified on macOS 26 / podman 5.4.2: with the address in place,
# https://quay.io returns 200 and a non-allowlisted host still gets 403, so
# the egress boundary applies inside nested containers too.
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

# ignore_chown_errors is required, not incidental: with a single-UID mapping
# there is no second UID for image layers to be chowned to, so layers that
# record other owners would otherwise fail to extract.
cat > "$config/storage.conf" <<'CONF'
[storage]
driver="overlay"
[storage.options.overlay]
mount_program="/usr/bin/fuse-overlayfs"
ignore_chown_errors="true"
CONF

# No cgroup delegation reaches this guest, so asking podman to manage cgroups
# fails; events_logger=file avoids the journald socket that is equally absent.
#
# The rest of this config is what lets a nested container reach the network:
#
#   hosts_file   only takes effect for native `podman run` — the
#                Docker-compatible API ignores it, and the agent types
#                `docker`. Kept anyway because it costs nothing and makes
#                podman-native use behave; the compat path is covered by the
#                address-bearing proxy URL below.
#   volumes/env  the gateway TLS-intercepts, so a container that does not
#                trust the bottle's CA bundle gets "unable to get local issuer
#                certificate". Mounting the bundle read-only and pointing the
#                usual env vars at it covers curl, wget, python, and node
#                without distro-specific trust commands.
#
# The proxy URL carries the bottle's identity token. podman already forwards
# that same URL into every nested container from the agent's own environment,
# so writing it to a 0600 file inside this disposable VM hands it to nobody
# new. It is never echoed.
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
# The gateway name resolves only through the bottle's /etc/hosts, which a
# nested container does not inherit, so hand it the address instead.
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    value = os.environ.get(var)
    if value:
        entries.append(f"{var}={value.replace(name, ip)}")
# NO_PROXY keeps the name: it is matched against what a client asks for, and
# code inside a nested container still says "bot-bottle-gateway".
for var in ("NO_PROXY", "no_proxy"):
    value = os.environ.get(var)
    if value:
        entries.append(f"{var}={value}")

path = Path(os.environ["CONTAINERS_CONF"])
path.write_text("\n".join([
    "[containers]",
    'cgroups="disabled"',
    # podman copies the host's proxy vars into every container by default,
    # and that copy *wins* over the env below — putting the unresolvable
    # gateway name back. Turn it off so the address-bearing URLs stand.
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

# Registry pulls egress through the bottle's proxy like everything else. The
# token-bearing proxy URL is already in the agent's environment; persisting it
# inside this disposable VM does not broaden its authority.
python3 - <<'PY'
import json
import os
from pathlib import Path

proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy", "")
no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy", "")
config = {"proxies": {"default": {
    "httpProxy": proxy,
    "httpsProxy": proxy,
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

# The service's own environment is the last word on what a nested container
# gets. podman's Docker-compatible API injects proxy settings from the daemon
# environment *after* containers.conf is applied, so neither the `env` list
# below nor `http_proxy=false` can override it — the same blind spot the
# compat path has for `hosts_file`. Substituting here, before the service
# starts, is therefore the only place the address actually sticks.
#
# Assigned via command substitution, never echoed: these carry the bottle's
# identity token.
for var in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
    eval "value=\${$var:-}"
    [ -n "$value" ] || continue
    eval "export $var=\"\${value%%$GATEWAY_NAME*}$gateway_ip\${value#*$GATEWAY_NAME}\""
done

log=/tmp/bot-bottle-nested-containers.log
nohup podman system service --time=0 \
    "unix://$XDG_RUNTIME_DIR/podman.sock" \
    >"$log" 2>&1 </dev/null &

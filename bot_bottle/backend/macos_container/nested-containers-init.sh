#!/bin/sh
set -eu

uid="$(id -u)"
if [ "$uid" -eq 0 ]; then
    echo "refusing to run the guest container engine as root" >&2
    exit 1
fi

for command in podman docker fuse-overlayfs slirp4netns; do
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
cat > "$config/containers.conf" <<'CONF'
[containers]
cgroups="disabled"
[engine]
cgroup_manager="cgroupfs"
events_logger="file"
CONF

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

log=/tmp/bot-bottle-nested-containers.log
nohup podman system service --time=0 \
    "unix://$XDG_RUNTIME_DIR/podman.sock" \
    >"$log" 2>&1 </dev/null &

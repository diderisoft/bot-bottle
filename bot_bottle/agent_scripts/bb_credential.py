#!/usr/bin/env python3
"""bb-credential — fetch a readable credential from the bot-bottle gateway.

Installed as /usr/local/bin/bb-credential in every agent image. Contacts the
supervisor server (the same host as the egress proxy) to fetch the current
value of a credential declared with `agent_can_read: true`.

Usage
-----
  bb-credential get <label> [field]        raw value of field (default: first)
  bb-credential aws <label>                credential_process JSON for AWS CLI/SDKs
  bb-credential git <label>                git-credential-helper (stdin/stdout)
  bb-credential k8s <label>               ExecCredential JSON for kubectl
  docker-credential-bb get                 Docker credential helper (reads URL from stdin)

The supervisor server URL is derived from the HTTP proxy env (the proxy carries
the identity token as basic-auth credentials), falling back to the EGRESS_HOSTNAME
constant and the SUPERVISE_PORT.

Exit codes: 0 success, 1 credential not found or not readable, 2 usage error.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

_SUPERVISE_PORT = 9100
_EGRESS_HOSTNAME = "egress"
_TIMEOUT = 10


def _gateway_url_and_token() -> tuple[str, str]:
    """The supervisor base URL and identity token from the proxy env."""
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    if proxy:
        parsed = urllib.parse.urlparse(proxy)
        host = parsed.hostname or _EGRESS_HOSTNAME
        token = parsed.password or ""
        return f"http://{host}:{_SUPERVISE_PORT}", token
    return f"http://{_EGRESS_HOSTNAME}:{_SUPERVISE_PORT}", ""


def _fetch(label: str) -> dict[str, object]:
    base_url, token = _gateway_url_and_token()
    url = f"{base_url}/bb-credential/{urllib.parse.quote(label, safe='')}"
    headers: dict[str, str] = {}
    if token:
        headers["x-bot-bottle-identity"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.stderr.write(f"bb-credential: credential {label!r} not found or not readable\n")
            sys.exit(1)
        sys.stderr.write(f"bb-credential: gateway error {e.code} for {label!r}\n")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"bb-credential: could not reach gateway: {e}\n")
        sys.exit(1)


def _cmd_get(args: list[str]) -> None:
    if not args:
        sys.stderr.write("usage: bb-credential get <label> [field]\n")
        sys.exit(2)
    label = args[0]
    field = args[1] if len(args) > 1 else None
    result = _fetch(label)
    values: dict[str, str] = result.get("values", {})  # type: ignore[assignment]
    if field:
        val = values.get(field, "")
        if not val:
            sys.stderr.write(f"bb-credential: no field {field!r} in credential {label!r}\n")
            sys.exit(1)
        print(val)
    else:
        # Print first value
        if not values:
            sys.stderr.write(f"bb-credential: credential {label!r} has no values\n")
            sys.exit(1)
        print(next(iter(values.values())))


def _cmd_aws(args: list[str]) -> None:
    """Output credential_process JSON for the AWS CLI/SDKs."""
    if not args:
        sys.stderr.write("usage: bb-credential aws <label>\n")
        sys.exit(2)
    label = args[0]
    result = _fetch(label)
    values: dict[str, str] = result.get("values", {})  # type: ignore[assignment]
    out: dict[str, object] = {
        "Version": 1,
        "AccessKeyId": values.get("access_key_id", ""),
        "SecretAccessKey": values.get("secret_access_key", ""),
    }
    if "session_token" in values:
        out["SessionToken"] = values["session_token"]
    # expires_at from the fetch result for cache hints
    expires = result.get("expires_at")
    if expires:
        out["Expiration"] = expires
    print(json.dumps(out))


def _cmd_git(args: list[str]) -> None:
    """git-credential-helper protocol: read action from argv or stdin."""
    # git passes the action as the first arg: get, store, erase.
    action = args[0] if args else ""
    if action not in ("get", ""):
        # store/erase are no-ops
        sys.exit(0)

    # Read the credential description from stdin (key=value lines)
    cred_input: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        if "=" in line:
            k, _, v = line.partition("=")
            cred_input[k.strip()] = v.strip()

    label = cred_input.get("username", "") or os.environ.get("BB_CRED_LABEL", "")
    if not label:
        # No label: can't serve; exit without output so git tries next helper.
        sys.exit(0)

    result = _fetch(label)
    values: dict[str, str] = result.get("values", {})  # type: ignore[assignment]
    token = values.get("token", "")
    if not token:
        sys.exit(0)
    print(f"username={label}")
    print(f"password={token}")


def _cmd_k8s(args: list[str]) -> None:
    """ExecCredential JSON for kubectl `exec` credential plugin."""
    if not args:
        sys.stderr.write("usage: bb-credential k8s <label>\n")
        sys.exit(2)
    label = args[0]
    result = _fetch(label)
    values: dict[str, str] = result.get("values", {})  # type: ignore[assignment]
    exec_cred: dict[str, object] = {
        "apiVersion": "client.authentication.k8s.io/v1",
        "kind": "ExecCredential",
        "status": {
            "token": values.get("token", ""),
        },
    }
    expires = result.get("expires_at")
    if expires:
        exec_cred["status"] = {**exec_cred["status"], "expirationTimestamp": expires}  # type: ignore[dict-item]
    print(json.dumps(exec_cred))


def _cmd_docker_get() -> None:
    """docker-credential-bb get: reads server URL from stdin, returns JSON auth."""
    _server = sys.stdin.read().strip()
    label = os.environ.get("BB_CRED_LABEL", "")
    if not label:
        sys.stderr.write("docker-credential-bb: set BB_CRED_LABEL to the credential label\n")
        sys.exit(1)
    result = _fetch(label)
    values: dict[str, str] = result.get("values", {})  # type: ignore[assignment]
    print(json.dumps({
        "ServerURL": _server,
        "Username": label,
        "Secret": values.get("token", ""),
    }))


def main() -> None:
    prog = os.path.basename(sys.argv[0])
    args = sys.argv[1:]

    # docker-credential-bb shim: invoked as `docker-credential-bb get`
    if prog == "docker-credential-bb":
        action = args[0] if args else "get"
        if action == "get":
            _cmd_docker_get()
        else:
            sys.exit(0)
        return

    if not args:
        sys.stderr.write(__doc__ or "")
        sys.exit(2)

    subcmd, *rest = args
    if subcmd == "get":
        _cmd_get(rest)
    elif subcmd == "aws":
        _cmd_aws(rest)
    elif subcmd == "git":
        _cmd_git(rest)
    elif subcmd == "k8s":
        _cmd_k8s(rest)
    else:
        sys.stderr.write(f"bb-credential: unknown subcommand {subcmd!r}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()

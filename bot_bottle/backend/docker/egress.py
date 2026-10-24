"""Docker egress constants and the host-side mitmproxy CA mint."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...contract.hostctl import DEFAULT_EGRESS_PORT
from ...lib.log import die


# The gateway's default egress port; launches use `InfraSettings.egress_port`.
EGRESS_PORT = DEFAULT_EGRESS_PORT

# mitmproxy's CA in the container: one PEM holding cert and key.
EGRESS_CA_IN_CONTAINER = "/home/mitmproxy/.mitmproxy/mitmproxy-ca.pem"


def egress_tls_init(stage_dir: Path) -> tuple[Path, Path]:
    """Mint an egress CA with host `openssl req`. Returns `(mitmproxy_pem,
    cert_only_pem)`: the cert+key concat mitmproxy reads, and the cert alone
    for the agent's trust store.

    Files live under `<stage_dir>/egress-ca/` at 0644 so mitmproxy (uid 1000)
    can read them; the 0700 stage dir protects the key."""
    work = stage_dir / "egress-ca"
    work.mkdir(exist_ok=True)
    key_path = work / "ca-key.pem"
    cert_path = work / "ca.pem"
    cnf_path = work / "ca.cnf"

    # RSA-2048, which mitmproxy's leaf config expects.
    keygen = subprocess.run(
        ["openssl", "genrsa", "-out", str(key_path), "2048"],
        capture_output=True, text=True, check=False,
    )
    if keygen.returncode != 0:
        die(f"egress ca keygen failed: {keygen.stderr.strip()}")
    # The standalone key is never copied or mounted; owner-only.
    key_path.chmod(0o600)

    # `subjectKeyIdentifier=hash` (SHA-1 of the pubkey) matches the AKI
    # mitmproxy puts on leaves; without it chain validation fails.
    cnf_path.write_text(
        "[req]\n"
        "distinguished_name = req_dn\n"
        "prompt = no\n"
        "x509_extensions = v3_ca\n"
        "\n"
        "[req_dn]\n"
        "O = bot-bottle\n"
        "CN = bot-bottle egress CA\n"
        "\n"
        "[v3_ca]\n"
        "basicConstraints = critical, CA:TRUE\n"
        "keyUsage = critical, keyCertSign, cRLSign\n"
        "subjectKeyIdentifier = hash\n"
    )
    cnf_path.chmod(0o644)

    req = subprocess.run(
        ["openssl", "req", "-x509", "-new", "-nodes",
         "-key", str(key_path),
         "-sha256", "-days", "365",
         "-config", str(cnf_path),
         "-out", str(cert_path)],
        capture_output=True, text=True, check=False,
    )
    if req.returncode != 0:
        die(f"egress ca cert generation failed: {req.stderr.strip()}")

    cert_path.chmod(0o644)
    # World-readable for mitmproxy's uid through the mount; the 0700 parent dir
    # restricts it on the host.
    mitm = work / "mitmproxy-ca.pem"
    mitm.write_bytes(cert_path.read_bytes() + key_path.read_bytes())
    mitm.chmod(0o644)
    return (mitm, cert_path)

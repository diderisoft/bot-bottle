"""Roll the gateway's mitmproxy CA over to a new one, in place (PRD 0090).

    python3 -m bot_bottle.gateway.ca_rollover <confdir>

Runs inside the gateway (it needs `cryptography`, which the host CLI doesn't
have). Mints a new CA key, writes the new CA self-signed as
`mitmproxy-ca-cert.pem` (what agents install), and writes `mitmproxy-ca.pem` as
the new key, the new CA cross-signed by the current one, then the current CA.
mitmproxy issues leaves from the first cert's key and hands the file to
OpenSSL as the store it auto-builds the served chain from, so the gateway
serves `leaf → new CA (issued by current) → current CA`: agents that loaded the
current CA still validate, new ones trust the new CA. The current CA has to be
in the file for that chain to complete (without it OpenSSL sends the leaf
alone), and the self-signed new CA must not be, or clients that trust only the
current CA stop at it. Files keep their owner and mode and are replaced
atomically; mitmproxy picks them up on its next start.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

KEY_FILE = "mitmproxy-ca.pem"        # key + issuing cert (+ chain), mitmproxy's CA
CERT_FILE = "mitmproxy-ca-cert.pem"  # the CA agents install
_KEY_BITS = 2048                     # mitmproxy's own CA size
_VALIDITY = dt.timedelta(days=3650)  # mitmproxy's own CA lifetime


def _ca_builder(subject: x509.Name, issuer: x509.Name, *, public_key: rsa.RSAPublicKey,
                issuer_public_key: rsa.RSAPublicKey, not_before: dt.datetime,
                not_after: dt.datetime) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer).public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before).not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_public_key),
                       critical=False)
    )


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def roll_over(confdir: Path, *, now: dt.datetime | None = None) -> x509.Certificate:
    """Replace the CA in `confdir` and return the new (self-signed) CA cert.
    Raises FileNotFoundError when `confdir` has no current CA to cross-sign
    with."""
    now = now or dt.datetime.now(dt.timezone.utc)
    current_key = serialization.load_pem_private_key((confdir / KEY_FILE).read_bytes(), None)
    if not isinstance(current_key, rsa.RSAPrivateKey):
        raise ValueError(f"{KEY_FILE} does not hold an RSA key")
    current = x509.load_pem_x509_certificate((confdir / CERT_FILE).read_bytes())

    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_BITS)
    # Unique per key, so the old and new CA never share a name, however close
    # together two rotations run.
    key_id = x509.SubjectKeyIdentifier.from_public_key(key.public_key()).digest.hex()[:12]
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"bot-bottle gateway CA {now:%Y-%m-%d} {key_id}"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "bot-bottle"),
    ])
    not_before = now - dt.timedelta(days=1)  # tolerate clock skew in agents
    not_after = now + _VALIDITY
    new_ca = _ca_builder(
        subject, subject, public_key=key.public_key(), issuer_public_key=key.public_key(),
        not_before=not_before, not_after=not_after,
    ).sign(key, hashes.SHA256())
    cross = _ca_builder(
        subject, current.subject, public_key=key.public_key(),
        issuer_public_key=current_key.public_key(), not_before=not_before,
        not_after=min(not_after, current.not_valid_after_utc),
    ).sign(current_key, hashes.SHA256())

    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    no_password = serialization.NoEncryption()
    _replace(confdir / KEY_FILE, key_pem + _pem(cross) + _pem(current), private=True)
    _replace(confdir / "mitmproxy-ca.p12", pkcs12.serialize_key_and_certificates(
        b"mitmproxy", key, new_ca, None, no_password), private=True)
    _replace(confdir / CERT_FILE, _pem(new_ca))
    _replace(confdir / "mitmproxy-ca-cert.cer", _pem(new_ca))
    _replace(confdir / "mitmproxy-ca-cert.p12", pkcs12.serialize_key_and_certificates(
        b"mitmproxy", None, new_ca, None, no_password))
    return new_ca


def _replace(path: Path, data: bytes, *, private: bool = False) -> None:
    """Atomically write `path`, keeping the current file's owner and mode (a
    new file is root-only when `private`)."""
    try:
        st: os.stat_result | None = path.stat()
    except FileNotFoundError:
        st = None
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        if st is not None:
            os.fchmod(fd, st.st_mode & 0o777)
            if hasattr(os, "fchown"):
                os.fchown(fd, st.st_uid, st.st_gid)
        elif not private:
            os.fchmod(fd, 0o644)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: python3 -m bot_bottle.gateway.ca_rollover <confdir>\n")
        return 2
    new_ca = roll_over(Path(args[0]))
    fingerprint = new_ca.fingerprint(hashes.SHA256()).hex()
    sys.stdout.write(f"rolled the gateway CA over to sha256:{fingerprint}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

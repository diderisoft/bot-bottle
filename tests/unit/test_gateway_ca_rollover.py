"""PRD 0090: rolling the gateway CA over so running agents keep validating.

The property under test is the TLS one: after a rollover the gateway issues
leaves from the new key and serves what mitmproxy serves, and a client
trusting only the old CA and a client trusting only the new CA must both
complete the handshake. mitmproxy (11.x) doesn't send `mitmproxy-ca.pem`'s
certs as an explicit chain: it loads the file as the server context's verify
store and lets OpenSSL auto-build the chain, so the server here does exactly
that. Checked with OpenSSL through `ssl`; Node, Bun and curl were checked on
the same chains when designing it. One difference: CPython's `ssl` still
sends a partial chain when the file lacks the old CA, where mitmproxy's
pyOpenSSL context sends the leaf alone, so why the old CA is in the file is
pinned by the layout test, not by a handshake.
"""

from __future__ import annotations

import datetime as dt
import os
import ssl
import stat
import tempfile
import unittest
from pathlib import Path

from cryptography import x509  # a dev requirement; the gateway image pins it
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from bot_bottle.gateway import ca_rollover

_NOW = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)


def _name(cn: str) -> "x509.Name":
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _seed_mitmproxy_ca(confdir: Path) -> None:
    """A CA as mitmproxy mints it on first start: key + self-signed cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder().subject_name(_name("mitmproxy"))
            .issuer_name(_name("mitmproxy")).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_NOW - dt.timedelta(days=30))
            .not_valid_after(_NOW + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    (confdir / "mitmproxy-ca.pem").write_bytes(key_pem + pem)
    (confdir / "mitmproxy-ca.pem").chmod(0o600)
    (confdir / "mitmproxy-ca-cert.pem").write_bytes(pem)


def _gateway_serves(confdir: Path, workdir: Path) -> tuple[Path, Path]:
    """Issue a leaf the way mitmproxy does (issuer = first cert's subject,
    signed by the CA key) and return (leaf cert file, leaf key file)."""
    raw = (confdir / "mitmproxy-ca.pem").read_bytes()
    ca_key = serialization.load_pem_private_key(raw, None)
    assert isinstance(ca_key, rsa.RSAPrivateKey)
    ca_certs = x509.load_pem_x509_certificates(raw)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (x509.CertificateBuilder().subject_name(_name("api.example.com"))
            .issuer_name(ca_certs[0].subject).public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_NOW - dt.timedelta(days=1))
            .not_valid_after(_NOW + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.example.com")]), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()), False)
            .sign(ca_key, hashes.SHA256()))
    chain, key_file = workdir / "leaf.pem", workdir / "leaf.key"
    chain.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return chain, key_file


def _handshake(confdir: Path, leaf: Path, leaf_key: Path, trusted: Path) -> str | None:
    """None when a client trusting only `trusted` accepts what the gateway
    serves, else the verification error. In-memory, no sockets."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(leaf, leaf_key)
    server_ctx.load_verify_locations(cafile=str(confdir / "mitmproxy-ca.pem"))  # as mitmproxy
    client_ctx = ssl.create_default_context(cafile=str(trusted))
    client_ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT  # test certs are minimal
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname="api.example.com")
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    done = {"client": False, "server": False}
    for _ in range(20):
        for role, end in (("client", client), ("server", server)):
            if done[role]:
                continue
            try:
                end.do_handshake()
                done[role] = True
            except ssl.SSLWantReadError:
                pass
            except ssl.SSLCertVerificationError as e:
                return e.verify_message
            except ssl.SSLError as e:  # the other side's alert
                return f"{role}: {e.reason}"
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if all(done.values()):
            return None
    return "handshake did not finish"


class TestCaRollover(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.confdir = self.dir / "confdir"
        self.confdir.mkdir()
        _seed_mitmproxy_ca(self.confdir)
        self.old = self.dir / "old.pem"
        self.old.write_bytes((self.confdir / "mitmproxy-ca-cert.pem").read_bytes())

    def _roll(self) -> Path:
        ca_rollover.roll_over(self.confdir, now=_NOW)
        new = self.dir / "new.pem"
        new.write_bytes((self.confdir / "mitmproxy-ca-cert.pem").read_bytes())
        return new

    def test_agents_on_the_old_ca_and_the_new_ca_both_validate(self) -> None:
        new = self._roll()
        leaf, leaf_key = _gateway_serves(self.confdir, self.dir)
        self.assertIsNone(_handshake(self.confdir, leaf, leaf_key, self.old))
        self.assertIsNone(_handshake(self.confdir, leaf, leaf_key, new))

    def test_the_ca_file_holds_the_cross_signed_ca_then_the_old_one(self) -> None:
        # Without the old CA, OpenSSL can't complete the chain and serves the
        # leaf alone; with the self-signed new CA, old-CA clients stop at it.
        new_pem = self._roll().read_bytes()
        raw = (self.confdir / "mitmproxy-ca.pem").read_bytes()
        cross, old_again = x509.load_pem_x509_certificates(raw)
        new_ca = x509.load_pem_x509_certificate(new_pem)
        old_ca = x509.load_pem_x509_certificate(self.old.read_bytes())
        self.assertEqual(old_ca, old_again)
        self.assertEqual(old_ca.subject, cross.issuer)
        self.assertEqual(new_ca.subject, cross.subject)
        self.assertEqual(new_ca.public_key(), cross.public_key())
        self.assertNotEqual(old_ca.subject, new_ca.subject)
        self.assertLessEqual(cross.not_valid_after_utc, old_ca.not_valid_after_utc)
        key = serialization.load_pem_private_key(raw, None)
        self.assertEqual(new_ca.public_key(), key.public_key())

    def test_the_overlap_covers_one_generation(self) -> None:
        # PRD 0090 non-goal: an agent that skipped two rotations isn't covered.
        middle = self._roll()
        newest = self._roll()
        leaf, leaf_key = _gateway_serves(self.confdir, self.dir)
        self.assertIsNone(_handshake(self.confdir, leaf, leaf_key, middle))
        self.assertIsNone(_handshake(self.confdir, leaf, leaf_key, newest))
        self.assertIsNotNone(_handshake(self.confdir, leaf, leaf_key, self.old))

    def test_files_keep_their_mode_and_every_encoding_is_rewritten(self) -> None:
        self._roll()
        mode = stat.S_IMODE((self.confdir / "mitmproxy-ca.pem").stat().st_mode)
        self.assertEqual(0o600, mode)
        for name in ("mitmproxy-ca.p12", "mitmproxy-ca-cert.cer", "mitmproxy-ca-cert.p12"):
            self.assertTrue((self.confdir / name).is_file(), name)
        self.assertEqual([], [p for p in os.listdir(self.confdir) if p.endswith(".tmp")])

    def test_no_current_ca_is_an_error(self) -> None:
        (self.confdir / "mitmproxy-ca.pem").unlink()
        with self.assertRaises(FileNotFoundError):
            ca_rollover.roll_over(self.confdir, now=_NOW)


if __name__ == "__main__":
    unittest.main()

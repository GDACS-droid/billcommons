"""Tests for AIA intermediate recovery.

The behaviour under test is a security trade-off, not a convenience: recovery
exists so that a server which merely FORGOT to send its intermediate becomes
fetchable, and it must not become a way for a server (or a MITM) to nominate
what we trust. Most of these tests exist to pin that distinction -- if they
can't fail when the trust logic changes, they aren't doing their job.
"""
from __future__ import annotations

import datetime
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from billcommons_shared import aia


# ---------------------------------------------------------------------------
# is_missing_issuer_error: the narrowing that keeps real trust failures fatal
# ---------------------------------------------------------------------------

def test_detects_missing_issuer_through_wrapped_cause():
    """httpx wraps ssl errors, so the marker is usually a level or two down
    __cause__ -- a detector that only looks at the top exception would never
    fire in production and the repair would be dead code."""
    inner = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate (_ssl.c:1010)"
    )
    outer = httpx.ConnectError("connect failed")
    outer.__cause__ = inner
    assert aia.is_missing_issuer_error(outer) is True


@pytest.mark.parametrize(
    "message",
    [
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired (_ssl.c:1010)",
        "[SSL: CERTIFICATE_VERIFY_FAILED] self signed certificate (_ssl.c:1010)",
        "[SSL: CERTIFICATE_VERIFY_FAILED] Hostname mismatch, certificate is not valid for 'x'",
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate revoked (_ssl.c:1010)",
        "All connection attempts failed",
    ],
)
def test_other_tls_failures_are_not_repairable(message):
    """Expiry, self-signing, hostname mismatch and revocation are genuine
    trust failures. Treating any of them as "repairable" would turn this
    module into a verification bypass, so the marker match stays narrow."""
    exc = httpx.ConnectError(message)
    assert aia.is_missing_issuer_error(exc) is False


def test_detector_terminates_on_a_cyclic_cause_chain():
    a = httpx.ConnectError("a")
    b = httpx.ConnectError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert aia.is_missing_issuer_error(a) is False


# ---------------------------------------------------------------------------
# certificate-payload parsing: CA repositories serve all three encodings
# ---------------------------------------------------------------------------

def _make_ca(name: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def test_load_certificates_accepts_der_and_pem():
    _, cert = _make_ca("test-root")
    der = cert.public_bytes(serialization.Encoding.DER)
    pem = cert.public_bytes(serialization.Encoding.PEM)
    assert [c.subject for c in aia.load_certificates(der)] == [cert.subject]
    assert [c.subject for c in aia.load_certificates(pem)] == [cert.subject]


def test_load_certificates_returns_empty_on_garbage():
    """An AIA URL that serves an HTML error page must yield nothing rather
    than raising, so one broken CA repository can't crash a crawl."""
    assert aia.load_certificates(b"<html>404 not found</html>") == []


# ---------------------------------------------------------------------------
# the trust gate
# ---------------------------------------------------------------------------

def test_self_signed_issuer_is_never_returned_as_an_intermediate(monkeypatch):
    """A root recovered over the network must never become a trust-anchor
    candidate -- that would let a misconfigured (or hostile) server nominate
    its own anchor and verify anything it liked."""
    leaf_key, _ = _make_ca("leaf-unused")
    root_key, root = _make_ca("attacker-root")

    now = datetime.datetime.now(datetime.timezone.utc)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf.example")]))
        .issuer_name(root.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(root_key, hashes.SHA256())
    )

    monkeypatch.setattr(
        aia, "_peer_leaf_der", lambda *a, **k: leaf.public_bytes(serialization.Encoding.DER)
    )
    monkeypatch.setattr(aia, "_fetch_issuer", lambda client, cert: root)

    assert aia.collect_missing_intermediates("leaf.example") == []


def test_no_repair_when_recovered_chain_does_not_reach_a_shipped_root(monkeypatch):
    """The MITM case. An attacker can serve any leaf and any "intermediate";
    what they cannot do is make it chain to a root in the certifi bundle. If
    independent verification fails, build_repaired_ssl_context must return
    None so the caller leaves the original TLS error in place -- returning a
    context here would be equivalent to verify=False."""
    _, fake_intermediate = _make_ca("attacker-intermediate")
    leaf_key, _ = _make_ca("ignored")
    now = datetime.datetime.now(datetime.timezone.utc)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "victim.example")]))
        .issuer_name(fake_intermediate.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(leaf_key, hashes.SHA256())
    )
    monkeypatch.setattr(
        aia, "_peer_leaf_der", lambda *a, **k: leaf.public_bytes(serialization.Encoding.DER)
    )
    monkeypatch.setattr(
        aia, "collect_missing_intermediates", lambda *a, **k: [fake_intermediate]
    )
    assert aia.build_repaired_ssl_context("victim.example") is None


def test_no_repair_when_nothing_was_recovered(monkeypatch):
    monkeypatch.setattr(aia, "_peer_leaf_der", lambda *a, **k: _make_ca("x")[1].public_bytes(
        serialization.Encoding.DER))
    monkeypatch.setattr(aia, "collect_missing_intermediates", lambda *a, **k: [])
    assert aia.build_repaired_ssl_context("nothing.example") is None


def test_chain_walk_is_depth_capped(monkeypatch):
    """A looping or hostile AIA graph must not drive unbounded fetching."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    # Deliberately NOT self-signed: a self-signed starting cert would end the
    # walk on its first iteration and the cap would never be exercised.
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")]))
        .public_key(other_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(other_key, hashes.SHA256())
    )
    # An "intermediate" whose issuer is always a different name, so the walk
    # never terminates naturally on a self-signed cert.
    def endless(client, current):
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")])
        issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "parent")])
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(other_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(other_key, hashes.SHA256())
        )

    monkeypatch.setattr(
        aia, "_peer_leaf_der", lambda *a, **k: cert.public_bytes(serialization.Encoding.DER)
    )
    monkeypatch.setattr(aia, "_fetch_issuer", endless)
    recovered = aia.collect_missing_intermediates("loop.example")
    assert len(recovered) == aia.MAX_CHAIN_DEPTH


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def test_cache_probes_each_host_once_including_failures(monkeypatch):
    """An unrepairable host has thousands of queued documents; re-probing it
    per document would mean thousands of pointless TLS handshakes."""
    calls: list[str] = []

    def fake_build(host, port=443):
        calls.append(host)
        return None

    monkeypatch.setattr(aia, "build_repaired_ssl_context", fake_build)
    cache = aia.AiaRepairCache()
    assert cache.get("a.example") is None
    assert cache.get("a.example") is None
    assert cache.get("a.example") is None
    assert calls == ["a.example"]
    assert cache.attempted("a.example") is True
    assert cache.attempted("b.example") is False

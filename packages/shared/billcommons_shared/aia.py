"""AIA (Authority Information Access) intermediate-certificate recovery.

Several state legislature web servers are misconfigured: they present only
their leaf certificate and omit the intermediate CA that links it to a
trusted root. Observed on legislature.mi.gov, billstatus.ls.state.ms.us and
www.cga.ct.gov -- between them ~37,000 bill documents that were completely
unfetchable, every attempt failing with
`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`
(`openssl s_client` reports `Verify return code: 21`).

Browsers hide this class of misconfiguration by fetching the missing
intermediate from the URL in the leaf's Authority Information Access
extension ("AIA chasing"), which is why these sites look fine in Chrome and
are unfetchable to Python's SSL stack, which does not chase AIA.

This module recovers the missing intermediate the same way -- WITHOUT
weakening verification. The sharp edge is that Python's `ssl` module has no
API for supplying *untrusted* chain certificates: anything added via
`load_verify_locations` becomes a trust ANCHOR. Naively adding whatever the
server's AIA extension points at would therefore be strictly equivalent to
`verify=False` against an active MITM, who could nominate their own
"intermediate" and have their forged leaf verify against it.

So recovery is gated on an INDEPENDENT verification (`_verify_chain`): the
recovered leaf+intermediates must already chain to a root in the certifi
bundle, with hostname and validity checked, before any of it is trusted. A
fabricated chain fails that gate and the original TLS error is left to
stand. Only a genuine CA intermediate -- one the shipped roots already vouch
for -- is ever installed.

Residual limitation, stated plainly: once installed, that intermediate is a
trust anchor for the returned context, so path-length/name constraints above
it and its revocation status stop being enforced for that one host's client.
The alternative (a maintained hand-pinned bundle per broken host) trades
that for permanent manual upkeep and silent breakage on every CA rotation.
"""
from __future__ import annotations

import socket
import ssl
import threading

import certifi
import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
from cryptography.x509.verification import PolicyBuilder, Store

# The ONLY TLS failure this module treats as repairable. Deliberately narrow:
# an expired cert, a hostname mismatch, a self-signed cert or a revoked cert
# are all real trust failures that must keep failing. "unable to get local
# issuer certificate" is specifically the signature of a server that did not
# send its intermediate -- the one case where the missing piece is genuinely
# published elsewhere and fetching it changes nothing about what we trust.
MISSING_ISSUER_MARKERS = ("unable to get local issuer certificate",)

# Walk at most this far up from the leaf. Real-world broken chains are short
# (leaf -> one missing intermediate -> trusted root); the cap stops a
# malicious or looping AIA graph from driving unbounded fetches.
MAX_CHAIN_DEPTH = 4

CERT_FETCH_TIMEOUT = 15.0
_HANDSHAKE_TIMEOUT = 15.0


def is_missing_issuer_error(exc: BaseException) -> bool:
    """True if `exc`, or anything in its cause/context chain, is a TLS
    verification failure caused by a MISSING INTERMEDIATE.

    httpx wraps `ssl.SSLCertVerificationError` in `httpx.ConnectError`, so the
    interesting text is usually one or two levels down `__cause__`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current)
        if any(marker in text for marker in MISSING_ISSUER_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _peer_leaf_der(host: str, port: int = 443, *, timeout: float = _HANDSHAKE_TIMEOUT) -> bytes:
    """Read the leaf certificate a host presents, WITHOUT verifying it.

    The result is untrusted data -- we parse its AIA extension and nothing
    else is concluded from it. `_verify_chain` is what decides whether any of
    it is real.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ValueError(f"no peer certificate presented by {host}:{port}")
    return der


def _ca_issuer_urls(cert: x509.Certificate) -> list[str]:
    """The caIssuers URLs from `cert`'s AIA extension (ignoring OCSP entries)."""
    try:
        extension = cert.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        )
    except x509.ExtensionNotFound:
        return []
    urls: list[str] = []
    for description in extension.value:
        if description.access_method != AuthorityInformationAccessOID.CA_ISSUERS:
            continue
        location = description.access_location
        if isinstance(location, x509.UniformResourceIdentifier):
            urls.append(location.value)
    return urls


def load_certificates(payload: bytes) -> list[x509.Certificate]:
    """Parse certificates from an AIA response body.

    CA repositories serve caIssuers in any of DER (`application/pkix-cert`),
    PEM, or PKCS#7 (`.p7c`, `application/pkcs7-mime`) -- all three appear in
    the wild, so all three are tried rather than trusting the Content-Type.
    """
    try:
        return [x509.load_der_x509_certificate(payload)]
    except Exception:
        pass
    try:
        certificates = x509.load_pem_x509_certificates(payload)
        if certificates:
            return certificates
    except Exception:
        pass
    for loader in (pkcs7.load_der_pkcs7_certificates, pkcs7.load_pem_pkcs7_certificates):
        try:
            certificates = loader(payload)
            if certificates:
                return certificates
        except Exception:
            continue
    return []


def _certifi_store() -> Store:
    with open(certifi.where(), "rb") as handle:
        return Store(x509.load_pem_x509_certificates(handle.read()))


def _verify_chain(leaf: x509.Certificate, intermediates: list[x509.Certificate], host: str) -> bool:
    """Independently verify `leaf` for `host` using `intermediates` and the
    certifi roots ONLY.

    This is the security gate for the whole module: it is what distinguishes a
    genuine CA intermediate that a misconfigured server merely forgot to send
    from an attacker-nominated one. Returns False on any verification failure
    (untrusted root, bad signature, expiry, hostname mismatch), in which case
    the caller must leave the original TLS error in place.
    """
    try:
        verifier = PolicyBuilder().store(_certifi_store()).build_server_verifier(
            x509.DNSName(host)
        )
        verifier.verify(leaf, intermediates)
        return True
    except Exception:
        return False


def collect_missing_intermediates(
    host: str,
    port: int = 443,
    *,
    client: httpx.Client | None = None,
) -> list[x509.Certificate]:
    """Walk AIA upward from `host`'s leaf, returning the intermediates found.

    Stops at a self-signed certificate WITHOUT returning it: a root recovered
    over the network is never a candidate trust anchor, because that would let
    a server nominate its own. Verification has to terminate at a root we
    already shipped.

    Returns [] when nothing useful is found (no AIA extension, unreachable
    repository, unparseable payload) -- the caller then simply does not repair.
    """
    leaf = x509.load_der_x509_certificate(_peer_leaf_der(host, port))
    recovered: list[x509.Certificate] = []
    current = leaf
    owns_client = client is None
    active = client or httpx.Client(timeout=CERT_FETCH_TIMEOUT, follow_redirects=True)
    try:
        for _ in range(MAX_CHAIN_DEPTH):
            if current.issuer == current.subject:
                break
            issuer = _fetch_issuer(active, current)
            if issuer is None:
                break
            if issuer.subject == issuer.issuer:
                break
            recovered.append(issuer)
            current = issuer
    finally:
        if owns_client:
            active.close()
    return recovered


def _fetch_issuer(client: httpx.Client, cert: x509.Certificate) -> x509.Certificate | None:
    """Download and return the certificate that issued `cert`, or None.

    Plain-HTTP AIA URLs are normal and safe here: a tampered intermediate
    cannot produce a chain that satisfies `_verify_chain`, so transport
    integrity for this fetch is not what the security rests on.
    """
    for url in _ca_issuer_urls(cert):
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        for candidate in load_certificates(response.content):
            if candidate.subject == cert.issuer:
                return candidate
    return None


def build_repaired_ssl_context(host: str, port: int = 443) -> ssl.SSLContext | None:
    """Return an SSL context that can verify `host` despite its missing
    intermediate, or None if the gap is not safely repairable.

    None means "do not retry" -- the caller should let the original TLS error
    stand rather than downgrade verification.
    """
    try:
        leaf = x509.load_der_x509_certificate(_peer_leaf_der(host, port))
    except Exception:
        return None
    try:
        intermediates = collect_missing_intermediates(host, port)
    except Exception:
        return None
    if not intermediates:
        return None
    if not _verify_chain(leaf, intermediates, host):
        return None
    context = ssl.create_default_context(cafile=certifi.where())
    pem = b"".join(cert.public_bytes(Encoding.PEM) for cert in intermediates)
    context.load_verify_locations(cadata=pem.decode("ascii"))
    return context


class AiaRepairCache:
    """Per-host cache of repaired SSL contexts for a long-lived worker.

    A host is probed at most once per process: a successful repair caches the
    context, and a failed one caches None so an unrepairable host does not
    re-probe on every one of its thousands of queued documents.
    """

    def __init__(self) -> None:
        self._contexts: dict[str, ssl.SSLContext | None] = {}
        self._lock = threading.Lock()

    def get(self, host: str, port: int = 443) -> ssl.SSLContext | None:
        key = f"{host}:{port}"
        with self._lock:
            if key in self._contexts:
                return self._contexts[key]
        context = build_repaired_ssl_context(host, port)
        with self._lock:
            self._contexts[key] = context
        return context

    def attempted(self, host: str, port: int = 443) -> bool:
        with self._lock:
            return f"{host}:{port}" in self._contexts

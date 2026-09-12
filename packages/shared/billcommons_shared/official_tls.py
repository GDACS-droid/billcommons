"""Reviewed TLS intermediates for official-source landing-page discovery.

These legislature hosts omit an intermediate from their TLS presentation.  The
bundle is deliberately small and host-specific: it is not a general AIA
chaser and it never accepts certificates supplied by a remote server.  Every
context still starts with certifi's root store, retains hostname verification,
and relies on the normal complete-chain verification path.
"""
from __future__ import annotations

import hashlib
import ssl
from dataclasses import dataclass
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding


_ASSET_DIRECTORY = Path(__file__).with_name("certs") / "official_discovery"


@dataclass(frozen=True)
class ReviewedIntermediate:
    """A source-reviewed, public intermediate certificate asset."""

    filename: str
    der_sha256: str
    subject: str


# Source artifacts were fetched from each leaf certificate's public caIssuers
# repository and reviewed on the dates in certs/official_discovery/README.md.
# A context is returned for these
# exact authority names only.  Adding a host requires a new reviewed asset and
# its DER fingerprint; a certificate presented by a remote server can never
# extend this list.
_REVIEWED_HOSTS: dict[str, ReviewedIntermediate] = {
    "www.ilga.gov": ReviewedIntermediate(
        "sectigo-public-server-ov-r40.pem",
        "8eb2f17d668941c39a7fca0cee127ae0ebaf444610631cca3cd19eab46c5824a",
        "CN=Sectigo Public Server Authentication CA OV R40,O=Sectigo Limited,C=GB",
    ),
    "www.cga.ct.gov": ReviewedIntermediate(
        "godaddy-secure-g2.pem",
        "973a41276ffd01e027a2aad49e34c37846d3e976ff6a620b6712e33832041aa6",
        "CN=Go Daddy Secure Certificate Authority - G2,OU=http://certs.godaddy.com/repository/,O=GoDaddy.com\\, Inc.,L=Scottsdale,ST=Arizona,C=US",
    ),
    "www.legislature.mi.gov": ReviewedIntermediate(
        "digicert-global-g2-2020-ca1.pem",
        "c8025f9fc65fdfc95b3ca8cc7867b9a587b5277973957917463fc813d0b625a9",
        "CN=DigiCert Global G2 TLS RSA SHA256 2020 CA1,O=DigiCert Inc,C=US",
    ),
    "www.legislature.ms.gov": ReviewedIntermediate(
        "globalsign-rsa-ov-ssl-2018.pem",
        "b676ffa3179e8812093a1b5eafee876ae7a6aaf231078dad1bfb21cd2893764a",
        "CN=GlobalSign RSA OV SSL CA 2018,O=GlobalSign nv-sa,C=BE",
    ),
    "www.legislature.ohio.gov": ReviewedIntermediate(
        "sectigo-public-server-ov-r36.pem",
        "6542d176bed50f193c0ce297ae44ecd8a0a86bec2ede682769344059b4e78530",
        "CN=Sectigo Public Server Authentication CA OV R36,O=Sectigo Limited,C=GB",
    ),
    "legislature.vermont.gov": ReviewedIntermediate(
        "globalsign-rsa-ov-ssl-2018.pem",
        "b676ffa3179e8812093a1b5eafee876ae7a6aaf231078dad1bfb21cd2893764a",
        "CN=GlobalSign RSA OV SSL CA 2018,O=GlobalSign nv-sa,C=BE",
    ),
}


def reviewed_context_for_host(hostname: str) -> ssl.SSLContext | None:
    """Return certifi-roots plus the reviewed issuer for one exact host.

    ``None`` means that this host uses the normal system/certifi trust path.
    It does not mean "accept a chain supplied by this host".
    """
    reviewed = _REVIEWED_HOSTS.get(hostname)
    if reviewed is None:
        return None

    asset_path = _ASSET_DIRECTORY / reviewed.filename
    certificate = x509.load_pem_x509_certificate(asset_path.read_bytes())
    der = certificate.public_bytes(Encoding.DER)
    if hashlib.sha256(der).hexdigest() != reviewed.der_sha256:
        raise ValueError("reviewed TLS intermediate fingerprint mismatch")
    if certificate.subject.rfc4514_string() != reviewed.subject:
        raise ValueError("reviewed TLS intermediate subject mismatch")
    try:
        basic_constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    except x509.ExtensionNotFound as exc:
        raise ValueError("reviewed TLS intermediate is not a CA certificate") from exc
    if not basic_constraints.value.ca or certificate.subject == certificate.issuer:
        raise ValueError("reviewed TLS intermediate must be a non-root CA")

    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(asset_path))
    return context

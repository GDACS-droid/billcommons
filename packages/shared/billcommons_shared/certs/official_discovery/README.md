# Official discovery TLS intermediates

These public intermediate certificates are used only for the exact hosts in
`billcommons_shared.official_tls`. They were retrieved and reviewed on
2026-09-08 (Illinois added 2026-09-12) from the `caIssuers` URL embedded in the affected host's leaf
certificate. The files are pinned by the certificate DER SHA-256 value in
that module; do not replace an asset in place when a publisher rotates its
certificate.

| Asset | Reviewed hosts | Primary CA repository URL | DER SHA-256 |
| --- | --- | --- | --- |
| `sectigo-public-server-ov-r40.pem` | `www.ilga.gov` | `http://crt.sectigo.com/SectigoPublicServerAuthenticationCAOVR40.crt` | `8eb2f17d668941c39a7fca0cee127ae0ebaf444610631cca3cd19eab46c5824a` |
| `godaddy-secure-g2.pem` | `www.cga.ct.gov` | `http://certificates.godaddy.com/repository/gdig2.crt` | `973a41276ffd01e027a2aad49e34c37846d3e976ff6a620b6712e33832041aa6` |
| `digicert-global-g2-2020-ca1.pem` | `www.legislature.mi.gov` | `http://cacerts.digicert.com/DigiCertGlobalG2TLSRSASHA2562020CA1-1.crt` | `c8025f9fc65fdfc95b3ca8cc7867b9a587b5277973957917463fc813d0b625a9` |
| `globalsign-rsa-ov-ssl-2018.pem` | `www.legislature.ms.gov`, `legislature.vermont.gov` | `http://secure.globalsign.com/cacert/gsrsaovsslca2018.crt` | `b676ffa3179e8812093a1b5eafee876ae7a6aaf231078dad1bfb21cd2893764a` |
| `sectigo-public-server-ov-r36.pem` | `www.legislature.ohio.gov` | `http://crt.sectigo.com/SectigoPublicServerAuthenticationCAOVR36.crt` | `6542d176bed50f193c0ce297ae44ecd8a0a86bec2ede682769344059b4e78530` |

The TLS context begins with certifi's root store and does not enable OpenSSL
partial-chain validation. Loading the reviewed intermediate lets OpenSSL
complete the chain to its trusted root while retaining normal hostname and
expiry validation.

Illinois's September 12 handshake supplied only its leaf certificate and failed
with OpenSSL verification code 20 (missing local issuer). The R40 intermediate
was retrieved as a bounded, no-redirect public CA artifact. It was verified as
an **untrusted chain component** against certifi's existing roots using
`openssl verify -untrusted ... -verify_hostname www.ilga.gov`; a new live TLS
handshake then passed with hostname and full-chain verification enabled.
No AIA fetching occurs at runtime, no root is added, and partial-chain trust
remains disabled. This exact host does not authorize its apex or subdomains.

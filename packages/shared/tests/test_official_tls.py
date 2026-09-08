"""Tests for the static, host-scoped official discovery TLS bundle."""
from __future__ import annotations

import ssl

import certifi
import pytest

from billcommons_shared import official_tls


EXPECTED_HOSTS = {
    "www.cga.ct.gov": "973a41276ffd01e027a2aad49e34c37846d3e976ff6a620b6712e33832041aa6",
    "www.legislature.mi.gov": "c8025f9fc65fdfc95b3ca8cc7867b9a587b5277973957917463fc813d0b625a9",
    "www.legislature.ms.gov": "b676ffa3179e8812093a1b5eafee876ae7a6aaf231078dad1bfb21cd2893764a",
    "www.legislature.ohio.gov": "6542d176bed50f193c0ce297ae44ecd8a0a86bec2ede682769344059b4e78530",
    "legislature.vermont.gov": "b676ffa3179e8812093a1b5eafee876ae7a6aaf231078dad1bfb21cd2893764a",
}


def test_reviewed_contexts_are_exact_host_scoped_and_keep_root_verification(monkeypatch):
    calls: list[str | None] = []
    real_create_default_context = ssl.create_default_context

    def capture_context(*, cafile=None, **kwargs):
        calls.append(cafile)
        return real_create_default_context(cafile=cafile, **kwargs)

    monkeypatch.setattr(official_tls.ssl, "create_default_context", capture_context)
    for host, expected_hash in EXPECTED_HOSTS.items():
        context = official_tls.reviewed_context_for_host(host)
        assert context is not None
        assert official_tls._REVIEWED_HOSTS[host].der_sha256 == expected_hash
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        partial_chain = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
        assert not partial_chain or not context.verify_flags & partial_chain
    assert calls == [certifi.where()] * len(EXPECTED_HOSTS)


def test_unreviewed_host_cannot_receive_a_bundled_intermediate():
    assert official_tls.reviewed_context_for_host("cga.ct.gov") is None
    assert official_tls.reviewed_context_for_host("www.cga.ct.gov.evil.example") is None
    assert official_tls.reviewed_context_for_host("expired.example") is None


def test_tampered_reviewed_asset_fails_closed(tmp_path, monkeypatch):
    host = "www.cga.ct.gov"
    reviewed = official_tls._REVIEWED_HOSTS[host]
    original = official_tls._ASSET_DIRECTORY / reviewed.filename
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    target = asset_dir / reviewed.filename
    target.write_bytes(b"not a certificate")
    monkeypatch.setattr(official_tls, "_ASSET_DIRECTORY", asset_dir)
    with pytest.raises(ValueError):
        official_tls.reviewed_context_for_host(host)

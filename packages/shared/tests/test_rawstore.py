"""Tests for FilesystemRawStore.

The core guarantee ingestion adapters rely on: identical bytes always
produce the same content-addressed key (so re-fetching an unchanged upstream
document is a no-op / dedupes), and different bytes never collide.
"""
from billcommons_shared.rawstore import FilesystemRawStore


def test_put_is_content_addressed(tmp_path):
    store = FilesystemRawStore(root=tmp_path)
    key1 = store.put(b"hello world")
    key2 = store.put(b"hello world")
    assert key1 == key2, "identical payloads must dedupe to the same key"


def test_different_content_different_key(tmp_path):
    store = FilesystemRawStore(root=tmp_path)
    key1 = store.put(b"payload one")
    key2 = store.put(b"payload two")
    assert key1 != key2


def test_get_roundtrips_bytes(tmp_path):
    store = FilesystemRawStore(root=tmp_path)
    key = store.put(b"some bill text")
    assert store.get(key) == b"some bill text"


def test_exists(tmp_path):
    store = FilesystemRawStore(root=tmp_path)
    assert store.exists("nonexistent") is False
    key = store.put(b"data")
    assert store.exists(key) is True


def test_get_missing_key_raises(tmp_path):
    store = FilesystemRawStore(root=tmp_path)
    try:
        store.get("deadbeef")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError for missing key")

"""Tests for FilesystemRawStore.

The core guarantee ingestion adapters rely on: identical bytes always
produce the same content-addressed key (so re-fetching an unchanged upstream
document is a no-op / dedupes), and different bytes never collide.
"""
import json

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


def test_identical_bytes_do_not_rewrite_first_observation_metadata(tmp_path):
    store = FilesystemRawStore(root=tmp_path)
    key = store.put(b"shared government document", {"source_url": "https://first.example.gov/document"})
    assert store.put(
        b"shared government document",
        {"source_url": "https://second.example.gov/same-bytes"},
    ) == key

    _, meta_path = store._paths(key)
    assert json.loads(meta_path.read_text()) == {
        "source_url": "https://first.example.gov/document"
    }

import threading

import pytest

from slack_channel_export_selfservice_1 import EphemeralStore


def test_put_then_pop_returns_value():
    store = EphemeralStore(ttl_seconds=60)
    store.put("k", "v")
    assert store.pop("k") == "v"


def test_pop_consumes_entry():
    store = EphemeralStore(ttl_seconds=60)
    store.put("k", "v")
    store.pop("k")
    assert store.pop("k") is None


def test_peek_does_not_consume():
    store = EphemeralStore(ttl_seconds=60)
    store.put("k", "v")
    assert store.peek("k") == "v"
    assert store.peek("k") == "v"


def test_entries_expire_after_ttl(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        "slack_channel_export_selfservice_1.time.monotonic",
        lambda: now[0],
    )
    store = EphemeralStore(ttl_seconds=10)
    store.put("k", "v")
    now[0] += 11
    assert store.pop("k") is None
    assert store.peek("k") is None


def test_max_size_evicts_oldest(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(
        "slack_channel_export_selfservice_1.time.monotonic",
        lambda: now[0],
    )
    store = EphemeralStore(ttl_seconds=600, max_size=2)
    store.put("a", 1)
    now[0] += 1
    store.put("b", 2)
    now[0] += 1
    store.put("c", 3)  # should evict "a"
    assert store.peek("a") is None
    assert store.peek("b") == 2
    assert store.peek("c") == 3


def test_concurrent_writes_all_keys_survive():
    """All 800 unique keys should land in the store when no eviction is needed."""
    store = EphemeralStore(ttl_seconds=60, max_size=10000)

    def worker(start):
        for i in range(100):
            store.put(f"k{start}-{i}", i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store._items) == 800


def test_concurrent_writes_with_eviction_respects_cap():
    """Under contention with size-cap eviction, the dict stays bounded."""
    store = EphemeralStore(ttl_seconds=60, max_size=50)

    def worker(start):
        for i in range(100):
            store.put(f"k{start}-{i}", i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store._items) <= 50


def test_discard_silently_succeeds_when_missing():
    store = EphemeralStore(ttl_seconds=60)
    store.discard("never-existed")  # no exception


def test_update_existing_key_does_not_double_count_size():
    store = EphemeralStore(ttl_seconds=60, max_size=2)
    store.put("a", 1)
    store.put("a", 2)  # update, not add
    store.put("b", 10)
    assert store.peek("a") == 2
    assert store.peek("b") == 10


def test_discard_removes_existing_key():
    store = EphemeralStore(ttl_seconds=60)
    store.put("k", "v")
    store.discard("k")
    assert store.peek("k") is None

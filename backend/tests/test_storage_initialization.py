"""Initialization must not inventory transient publication state or active uploads."""

import io
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentsphere import storage as storage_module
from agentsphere.storage import PrivateStorage, StorageBusy, StorageError, new_storage_key


def test_initialization_waits_until_atomic_publication_removes_its_temporary_link(
    tmp_path, monkeypatch
):
    original = PrivateStorage(tmp_path / "private")
    original.initialize()
    restarted = PrivateStorage(original.root)
    key = new_storage_key(uuid4())
    linked, resume, attempted, inventoried = Event(), Event(), Event(), Event()
    link, lock, managed_tree = os.link, storage_module._lock_descriptor, restarted._managed_tree

    def pause_after_link(source, destination, **kwargs):
        link(source, destination, **kwargs)
        linked.set()
        if not resume.wait(10):
            raise RuntimeError("The isolated publication test did not resume.")

    def observe_exclusive_attempt(descriptor, *, exclusive):
        try:
            return lock(descriptor, exclusive=exclusive)
        finally:
            if exclusive:
                attempted.set()

    def observe_inventory(**kwargs):
        inventoried.set()
        return managed_tree(**kwargs)

    monkeypatch.setattr(os, "link", pause_after_link)
    monkeypatch.setattr(storage_module, "_lock_descriptor", observe_exclusive_attempt)
    monkeypatch.setattr(restarted, "_managed_tree", observe_inventory)

    def publish():
        with original.activity():
            return original.put(key, io.BytesIO(b"Evidence"), max_bytes=8)

    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(publish)
        try:
            assert linked.wait(5), "Publication did not reach its atomic hard-link boundary."
            assert (original.root / key).stat().st_nlink == 2
            initialization = pool.submit(restarted.initialize)
            assert attempted.wait(5), "Initialization did not request its exclusive fence."
            assert not inventoried.is_set(), "A live upload was inspected by initialization."
        finally:
            resume.set()
        assert publication.result(timeout=5).size_bytes == 8
        initialization.result(timeout=5)
    assert inventoried.is_set()
    with restarted.open(key) as handle:
        assert handle.read() == b"Evidence"
    assert list((original.root / "tmp").iterdir()) == []


def test_initialization_timeout_does_not_inventory_or_change_the_managed_tree(
    tmp_path, monkeypatch
):
    storage = PrivateStorage(tmp_path / "private")
    storage.initialize()
    restarted = PrivateStorage(storage.root)
    clock = iter((0, 3))
    monkeypatch.setattr(storage_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    def unexpected_inventory(**kwargs):
        pytest.fail("A busy initialization must not inspect active objects.")

    monkeypatch.setattr(restarted, "_managed_tree", unexpected_inventory)
    with storage.activity(), pytest.raises(StorageBusy):
        restarted.initialize()
    assert not restarted._initialized
    assert {path.name for path in storage.root.iterdir()} == {"objects", "tmp"}
    # Failed acquisition must close its own descriptor and leave other locks usable.
    with storage.activity(exclusive=True):
        pass


def test_initialization_body_busy_error_is_not_retried(tmp_path, monkeypatch):
    storage = PrivateStorage(tmp_path / "private")
    calls = []

    def fail(**kwargs):
        calls.append(True)
        raise StorageBusy("Inventory cannot be retried after acquisition.")

    monkeypatch.setattr(storage, "_managed_tree", fail)
    with pytest.raises(StorageBusy):
        storage.initialize()
    assert calls == [True]
    assert not storage._initialized
    with storage.activity(exclusive=True):
        pass


def test_initialization_respects_another_process_upload_fence(tmp_path, monkeypatch):
    storage = PrivateStorage(tmp_path / "private")
    storage.initialize()
    restarted = PrivateStorage(storage.root)
    child_code = """
import sys
from agentsphere.storage import PrivateStorage
with PrivateStorage(sys.argv[1]).activity():
    print('acquired', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(storage.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "acquired"
        clock = iter((0, 3))
        with monkeypatch.context() as patch:
            patch.setattr(storage_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
            with pytest.raises(StorageBusy):
                restarted.initialize()
        assert not restarted._initialized
    finally:
        child.communicate("release\n", timeout=15)
    assert child.returncode == 0
    restarted.initialize()
    assert restarted._initialized


def test_initialization_fence_exit_failure_does_not_publish_ready_storage(tmp_path, monkeypatch):
    storage = PrivateStorage(tmp_path / "private")
    activity = storage.activity

    @contextmanager
    def failed_exit(**kwargs):
        with activity(**kwargs):
            yield
        raise StorageError("Fence exit failed")

    monkeypatch.setattr(storage, "activity", failed_exit)
    with pytest.raises(StorageError, match="Fence exit failed"):
        storage.initialize()
    assert not storage._initialized
    with pytest.raises(StorageError, match="Initialize"):
        storage.put(new_storage_key(uuid4()), io.BytesIO(b"x"), max_bytes=1)
    # The injected failure follows a real, successful descriptor/lock release.
    with activity(exclusive=True):
        pass

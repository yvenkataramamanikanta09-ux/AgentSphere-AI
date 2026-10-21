"""Operating-system locking and bounded inventory tests use unique private roots."""

import io
import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agentsphere import storage as storage_module
from agentsphere.storage import PrivateStorage, StorageBusy, StorageError, new_storage_key


@pytest.fixture
def storage(tmp_path):
    result = PrivateStorage(tmp_path / "private")
    result.initialize()
    return result


CHILD_LOCK = """
import sys
from agentsphere.storage import PrivateStorage, StorageBusy
try:
    with PrivateStorage(sys.argv[1]).activity(exclusive=sys.argv[2] == 'exclusive'):
        print('acquired', flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.readline()
except StorageBusy:
    raise SystemExit(4)
"""


def child_probe(storage, mode):
    return subprocess.run(
        [sys.executable, "-c", CHILD_LOCK, str(storage.root), mode, "probe"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_construction_writes_nothing_and_initialization_provisions_the_sibling_fence(tmp_path):
    storage = PrivateStorage(tmp_path / "private")
    lock = storage.root.with_name(".private.activity.lock")
    assert not storage.root.exists() and not lock.exists()
    storage.initialize()
    assert lock.is_file()
    assert {item.name for item in storage.root.iterdir()} == {"objects", "tmp"}


def test_bounded_initialization_aborts_before_permission_changes(storage, monkeypatch):
    for _ in range(5):
        storage.put(new_storage_key(uuid4()), io.BytesIO(b"Private"), max_bytes=100)
    before = {path: path.read_bytes() for path in storage.root.glob("objects/*/*/original")}

    def unexpected_permission_change(*args, **kwargs):
        pytest.fail("A budget-exhausted preflight must not change permissions")

    restrict_permissions = storage_module._restrict_permissions

    def restrict_only_the_sibling_fence(path, **kwargs):
        if path != storage.root.with_name(".private.activity.lock"):
            unexpected_permission_change()
        restrict_permissions(path, **kwargs)

    monkeypatch.setattr(storage_module, "_restrict_directory", unexpected_permission_change)
    monkeypatch.setattr(storage_module, "_restrict_permissions", restrict_only_the_sibling_fence)
    with pytest.raises(StorageError, match="entry budget"):
        storage.initialize(max_entries=3)
    assert {path: path.read_bytes() for path in before} == before
    assert not storage._initialized


@pytest.mark.parametrize("limit", [0, -1, True])
def test_initialization_rejects_invalid_entry_budget(storage, limit):
    with pytest.raises(ValueError, match="entry budget"):
        storage.initialize(max_entries=limit)


def test_bounded_initialization_accepts_valid_existing_layout(storage):
    key = new_storage_key(uuid4())
    storage.put(key, io.BytesIO(b"Private"), max_bytes=100)
    storage.initialize(max_entries=10)
    with storage.open(key) as original:
        assert original.read() == b"Private"


def test_shared_activity_allows_other_process_uploads_and_blocks_maintenance(storage):
    with storage.activity():
        shared = child_probe(storage, "shared")
        exclusive = child_probe(storage, "exclusive")
    assert shared.returncode == 0, shared.stderr
    assert shared.stdout.strip() == "acquired"
    assert exclusive.returncode == 4, exclusive.stderr
    assert child_probe(storage, "exclusive").returncode == 0


@pytest.mark.parametrize("mode", ["shared", "exclusive"])
def test_another_process_exclusive_activity_blocks_all_local_activity(storage, mode):
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_LOCK, str(storage.root), "exclusive", "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "acquired"
        with pytest.raises(StorageBusy):
            with storage.activity(exclusive=mode == "exclusive"):
                pytest.fail("Conflicting cross-process lock must fail immediately.")
    finally:
        child.communicate("release\n", timeout=15)
    assert child.returncode == 0
    with storage.activity(exclusive=True):
        pass


def test_context_error_releases_lock_and_preserves_permanent_lock_identity(storage):
    with pytest.raises(ValueError, match="caller"):
        with storage.activity():
            lock = storage.root.with_name(".private.activity.lock")
            before = lock.stat()
            raise ValueError("caller error")
    assert lock.exists()
    with storage.activity(exclusive=True):
        after = lock.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert child_probe(storage, "exclusive").returncode == 0


@pytest.mark.parametrize("unexpected", ["directory", "hardlink"])
def test_activity_rejects_unexpected_lock_object_without_removing_it(tmp_path, unexpected):
    storage = PrivateStorage(tmp_path / "private")
    lock = storage.root.with_name(".private.activity.lock")
    if unexpected == "directory":
        lock.mkdir()
    else:
        other = tmp_path / "outside"
        other.write_bytes(b"untouched")
        os.link(other, lock)
    with pytest.raises(StorageError):
        with storage.activity():
            pytest.fail("Unexpected lock object must fail closed.")
    assert lock.exists()
    if unexpected == "hardlink":
        assert other.read_bytes() == b"untouched"


def test_activity_open_failure_is_sanitized(storage, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("sensitive internal lock path")

    monkeypatch.setattr(os, "open", fail)
    with pytest.raises(StorageError) as error:
        with storage.activity():
            pytest.fail("Lock failure cannot allow activity.")
    assert "sensitive internal lock path" not in str(error.value)


def test_existing_activity_file_permissions_are_restricted(storage):
    lock = storage.root.with_name(".private.activity.lock")
    lock.write_bytes(b"")
    if os.name != "nt":
        lock.chmod(0o666)
    with storage.activity():
        if os.name != "nt":
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600
        else:
            result = subprocess.run(
                [str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"), str(lock)],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert "(I)" not in result.stdout


def test_inventory_lists_only_safe_objects_and_private_temporary_files(storage):
    key = new_storage_key(uuid4())
    storage.put(key, io.BytesIO(b"original"), max_bytes=8)
    temporary = storage.root / "tmp" / f"{uuid4().hex}.part"
    temporary.write_bytes(b"old scratch")
    inventory = storage.inventory()
    assert inventory.complete
    assert inventory.scanned == 6
    assert len(inventory.entries) == 2
    original = next(entry for entry in inventory.entries if entry.kind == "object")
    scratch = next(entry for entry in inventory.entries if entry.kind == "temporary")
    assert original.storage_key == key
    assert original.workspace_id.hex == key.split("/")[1]
    assert scratch.storage_key is None and scratch.workspace_id is None
    assert storage.verify_entry(original).read_bytes() == b"original"
    assert storage.verify_entry(scratch) == temporary
    assert original.identity[2] == 8


def test_inventory_budget_counts_directories_and_returns_conservative_partial_result(storage):
    for _ in range(3):
        storage.put(new_storage_key(uuid4()), io.BytesIO(b"x"), max_bytes=1)
    inventory = storage.inventory(max_entries=3)
    assert inventory.scanned == 3
    assert inventory.complete is False
    assert all(path.name == "original" for path in storage.root.glob("objects/*/*/original"))


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_inventory_requires_positive_integer_budget(storage, limit):
    with pytest.raises(ValueError):
        storage.inventory(max_entries=limit)


def test_inventory_requires_explicit_storage_initialization(tmp_path):
    storage = PrivateStorage(tmp_path / "private")
    with pytest.raises(StorageError, match="Initialize"):
        storage.inventory()
    assert not storage.root.exists()


@pytest.mark.parametrize(
    "kind", ["unmanaged-root", "bad-workspace", "hardlinked-original", "bad-temporary"]
)
def test_inventory_rejects_unexpected_namespace_objects(storage, tmp_path, kind):
    if kind == "unmanaged-root":
        target = storage.root / "unrelated"
        target.write_bytes(b"keep")
    elif kind == "bad-workspace":
        target = storage.root / "objects" / "not-a-uuid"
        target.mkdir()
    elif kind == "bad-temporary":
        target = storage.root / "tmp" / "not-a-uuid.part"
        target.write_bytes(b"keep")
    else:
        key = new_storage_key(uuid4())
        storage.put(key, io.BytesIO(b"keep"), max_bytes=4)
        target = storage.object_path(key)
        os.link(target, tmp_path / "outside")
    with pytest.raises(StorageError):
        storage.inventory()
    assert target.exists()


def test_temporary_removal_requires_exclusive_activity_and_unchanged_identity(storage):
    path = storage.root / "tmp" / f"{uuid4().hex}.part"
    path.write_bytes(b"scratch")
    entry = storage.inventory().entries[0]
    with pytest.raises(StorageError, match="exclusive"):
        storage.remove_temporary(entry)
    with storage.activity():
        with pytest.raises(StorageError, match="exclusive"):
            storage.remove_temporary(entry)
    with storage.activity(exclusive=True):
        assert storage.remove_temporary(entry)
    assert not path.exists()


@pytest.mark.parametrize("change", ["replace", "write", "hardlink"])
def test_replaced_or_modified_temporary_is_retained(storage, tmp_path, change):
    path = storage.root / "tmp" / f"{uuid4().hex}.part"
    path.write_bytes(b"scratch")
    entry = storage.inventory().entries[0]
    if change == "replace":
        replacement = storage.root / "tmp" / f"{uuid4().hex}.part"
        replacement.write_bytes(b"replacement")
        os.replace(replacement, path)
    elif change == "write":
        path.write_bytes(b"modified scratch")
    else:
        os.link(path, tmp_path / "outside")
    with storage.activity(exclusive=True), pytest.raises(StorageError):
        storage.remove_temporary(entry)
    assert path.exists()


def test_temporary_removal_rejects_forged_inventory_paths(storage, tmp_path):
    path = storage.root / "tmp" / f"{uuid4().hex}.part"
    path.write_bytes(b"scratch")
    entry = storage.inventory().entries[0]
    with storage.activity(exclusive=True):
        for relative in ("../outside", "tmp/../../outside", str(tmp_path / "outside")):
            with pytest.raises(ValueError):
                storage.remove_temporary(replace(entry, relative_path=relative))
        with pytest.raises(ValueError):
            storage.remove_temporary(replace(entry, kind="object"))
    assert path.read_bytes() == b"scratch"


def test_temporary_removal_error_is_sanitized(storage, monkeypatch):
    path = storage.root / "tmp" / f"{uuid4().hex}.part"
    path.write_bytes(b"scratch")
    entry = storage.inventory().entries[0]

    def fail(*args, **kwargs):
        raise PermissionError("sensitive scratch detail")

    monkeypatch.setattr(Path, "unlink", fail)
    with storage.activity(exclusive=True), pytest.raises(StorageError) as error:
        storage.remove_temporary(entry)
    assert "sensitive scratch detail" not in str(error.value)
    assert path.exists()


def test_lock_mode_and_inventory_kind_are_validated(storage):
    lock = storage.root.with_name(".private.activity.lock")
    before = lock.stat()
    with pytest.raises(ValueError):
        with storage.activity(exclusive="yes"):
            pass
    after = lock.stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert storage_module.StorageBusy is StorageBusy

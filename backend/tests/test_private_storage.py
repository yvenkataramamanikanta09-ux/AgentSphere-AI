"""Every filesystem mutation is restricted to pytest's unique temporary directory."""

import asyncio
import hashlib
import io
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentsphere import storage as storage_module
from agentsphere.config import BACKEND_DIR, Settings
from agentsphere.storage import (
    PrivateStorage,
    StorageConflict,
    StorageError,
    StorageLimitExceeded,
    new_storage_key,
    validate_storage_key,
)


@pytest.fixture
def storage(tmp_path):
    result = PrivateStorage(tmp_path / "private")
    result.initialize()
    return result


def test_configuration_does_not_create_storage(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIVATE_STORAGE_ROOT", raising=False)
    root = tmp_path / "not-created"
    settings = Settings(_env_file=None, private_storage_root=root)
    assert settings.private_storage_root == root
    assert not root.exists()
    assert Settings(_env_file=None).private_storage_root.is_absolute()
    monkeypatch.setenv("PRIVATE_STORAGE_ROOT", str(root))
    assert Settings(_env_file=None).private_storage_root == root


@pytest.mark.parametrize("value", ["", ".", "relative/storage", "../escape", None, 12])
def test_invalid_root_settings(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, private_storage_root=value)


def test_root_rejects_traversal_and_public_or_source_locations(tmp_path):
    for path in (
        str(tmp_path / ".." / "escape"),
        Path(tmp_path.anchor),
        BACKEND_DIR.parent / "frontend" / "public" / "uploads",
        BACKEND_DIR / "src" / "uploads",
        BACKEND_DIR.parent / ".git" / "uploads",
        Path.home(),
    ):
        with pytest.raises(ValueError):
            PrivateStorage(path)


def test_default_key_is_server_uuid_namespace():
    workspace = uuid4()
    first, second = new_storage_key(workspace), new_storage_key(workspace)
    assert first != second
    assert first.split("/")[:2] == ["objects", workspace.hex]
    assert UUID(hex=first.split("/")[2]).version == 4
    assert validate_storage_key(first) == first
    with pytest.raises(ValueError):
        new_storage_key(str(workspace))


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../secret",
        "/etc/passwd",
        r"C:\Windows\win.ini",
        r"objects\a\b\original",
        "objects/../a/original",
        "objects/" + "a" * 32 + "/" + "B" * 32 + "/original",
        "objects/" + "a" * 32 + "/" + "b" * 32 + "/original/..",
        "//host/share",
        None,
    ],
)
def test_path_traversal_and_noncanonical_keys_rejected(storage, key):
    for operation in (storage.object_path, storage.open, storage.delete):
        with pytest.raises(ValueError):
            operation(key)
    with pytest.raises(ValueError):
        storage.put(key, io.BytesIO(b"x"), max_bytes=1)


def test_put_open_delete_and_hash(storage):
    workspace, file_id = uuid4(), uuid4()
    key = new_storage_key(workspace, file_id)
    value = b"hello\n" * 20_000
    result = storage.put(key, io.BytesIO(value), max_bytes=len(value))
    assert result.key == key
    assert result.size_bytes == len(value)
    assert result.sha256 == hashlib.sha256(value).hexdigest()
    assert (
        storage.object_path(key)
        == storage.root / "objects" / workspace.hex / file_id.hex / "original"
    )
    with storage.open(key) as source:
        assert source.read() == value
    assert not list((storage.root / "tmp").iterdir())
    assert storage.delete(key)
    assert not storage.delete(key)
    with pytest.raises(StorageError):
        storage.open(key)


def test_uninitialized_store_cannot_use_unprotected_existing_directories(tmp_path):
    root = tmp_path / "uninitialized"
    (root / "objects").mkdir(parents=True)
    (root / "tmp").mkdir()
    storage = PrivateStorage(root)
    with pytest.raises(StorageError, match="Initialize"):
        storage.put(new_storage_key(uuid4()), io.BytesIO(b"x"), max_bytes=1)


def test_initialized_root_cannot_be_reassigned(storage, tmp_path):
    original_root = storage.root
    with pytest.raises(AttributeError):
        storage.root = tmp_path / "elsewhere"
    assert storage.root == original_root


def test_initialization_refuses_unmanaged_files_without_deleting_them(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    unexpected = root / "unrelated.txt"
    unexpected.write_bytes(b"untouched")
    before = windows_dacl(root) if os.name == "nt" else stat.S_IMODE(root.stat().st_mode)
    with pytest.raises(StorageError, match="Unexpected"):
        PrivateStorage(root).initialize()
    assert unexpected.read_bytes() == b"untouched"
    assert {item.name for item in root.iterdir()} == {"unrelated.txt"}
    after = windows_dacl(root) if os.name == "nt" else stat.S_IMODE(root.stat().st_mode)
    assert after == before


def test_windows_reserved_or_ambiguous_root_components_rejected(tmp_path):
    if os.name == "nt":
        for part in ("CON", "LPT1.txt", "trailing.", "trailing ", "bad:stream", "bad*name"):
            with pytest.raises(ValueError):
                PrivateStorage(tmp_path / part)


def test_nonbinary_source_is_rejected_and_cleaned(storage):
    with pytest.raises(StorageError):
        storage.put(new_storage_key(uuid4()), io.StringIO("text"), max_bytes=10)
    assert not list((storage.root / "tmp").iterdir())


def test_delete_failure_is_sanitized_and_does_not_remove_object(storage, monkeypatch):
    key = new_storage_key(uuid4())
    storage.put(key, io.BytesIO(b"x"), max_bytes=1)
    original_unlink = Path.unlink
    target = storage.object_path(key)

    def fail(path, *args, **kwargs):
        if path == target:
            raise PermissionError("sensitive-delete-detail")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(StorageError) as error:
        storage.delete(key)
    assert "sensitive-delete-detail" not in str(error.value)
    assert target.read_bytes() == b"x"


def test_cannot_overwrite_existing_object(storage):
    key = new_storage_key(uuid4())
    storage.put(key, io.BytesIO(b"first"), max_bytes=10)
    with pytest.raises(StorageConflict):
        storage.put(key, io.BytesIO(b"second"), max_bytes=10)
    with storage.open(key) as handle:
        assert handle.read() == b"first"
    assert not list((storage.root / "tmp").iterdir())


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_limit_must_be_positive_integer(storage, limit):
    with pytest.raises(ValueError):
        storage.put(new_storage_key(uuid4()), io.BytesIO(b"x"), max_bytes=limit)


@pytest.mark.parametrize("content", [b"", b"too large"])
def test_failed_copy_leaves_no_object_or_temporary_file(storage, content):
    key = new_storage_key(uuid4())
    with pytest.raises(StorageError):
        storage.put(key, io.BytesIO(content), max_bytes=3)
    assert not storage.object_path(key).exists()
    assert not list((storage.root / "tmp").iterdir())


def test_size_limit_is_measured_during_reads(storage):
    class Reader:
        reads = 0

        def read(self, size):
            self.reads += 1
            return b"x" * size

    source = Reader()
    with pytest.raises(StorageLimitExceeded):
        storage.put(new_storage_key(uuid4()), source, max_bytes=1)
    assert source.reads == 1


def test_read_failure_is_sanitized_and_partial_write_removed(storage):
    class BrokenReader:
        def read(self, size):
            raise OSError("sensitive-source-path")

    key = new_storage_key(uuid4())
    with pytest.raises(StorageError) as error:
        storage.put(key, BrokenReader(), max_bytes=10)
    assert "sensitive-source-path" not in str(error.value)
    assert not storage.object_path(key).exists()
    assert not list((storage.root / "tmp").iterdir())


@pytest.mark.parametrize("failure", ["fsync", "link"])
def test_disk_or_publish_failure_is_sanitized(storage, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise OSError("sensitive-volume-detail")

    monkeypatch.setattr(os, failure, fail)
    key = new_storage_key(uuid4())
    with pytest.raises(StorageError) as error:
        storage.put(key, io.BytesIO(b"x"), max_bytes=1)
    assert "sensitive-volume-detail" not in str(error.value)
    assert not storage.object_path(key).exists()
    assert not list((storage.root / "tmp").iterdir())


def test_initialization_permission_failure_is_sanitized(tmp_path, monkeypatch):
    def fail(path):
        raise PermissionError("sensitive-acl")

    monkeypatch.setattr(storage_module, "_restrict_directory", fail)
    with pytest.raises(StorageError) as error:
        PrivateStorage(tmp_path / "private").initialize()
    assert "sensitive-acl" not in str(error.value)


def test_initialize_rejects_missing_parent_and_file_in_directory_path(tmp_path):
    with pytest.raises(StorageError):
        PrivateStorage(tmp_path / "missing-parent" / "private").initialize()
    root = tmp_path / "file"
    root.write_bytes(b"untouched")
    with pytest.raises(StorageError):
        PrivateStorage(root).initialize()
    assert root.read_bytes() == b"untouched"


def test_unexpected_file_or_directory_in_object_path(storage):
    workspace = uuid4()
    (storage.root / "objects" / workspace.hex).write_bytes(b"untouched")
    with pytest.raises(StorageError):
        storage.put(new_storage_key(workspace), io.BytesIO(b"x"), max_bytes=1)
    key = new_storage_key(uuid4())
    path = storage.object_path(key)
    path.mkdir(parents=True)
    for operation in (storage.object_path, storage.open, storage.delete):
        with pytest.raises(StorageError):
            operation(key)
    assert path.is_dir()


def test_hardlinked_existing_leaf_rejected(storage, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"untouched")
    key = new_storage_key(uuid4())
    path = storage.object_path(key)
    path.parent.mkdir(parents=True)
    os.link(outside, path)
    for operation in (storage.open, storage.delete):
        with pytest.raises(StorageError):
            operation(key)
    assert outside.read_bytes() == b"untouched"
    assert path.exists()


def test_reparse_flag_rejected_even_for_regular_mode():
    assert storage_module._is_link(
        SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        )
    )


@pytest.mark.parametrize("position", ["root", "parent", "workspace", "temporary"])
def test_directory_links_or_windows_junctions_cannot_escape(tmp_path, position):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "private"
    if position == "parent":
        link = tmp_path / "linked-parent"
        target = outside
        storage = PrivateStorage(link / "private")
    elif position == "root":
        link, target = root, outside
        storage = PrivateStorage(root)
    else:
        storage = PrivateStorage(root)
        storage.initialize()
        if position == "workspace":
            workspace = uuid4()
            link, target = root / "objects" / workspace.hex, outside
        else:
            link, target = root / "tmp", outside
            link.rmdir()
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        if position in {"root", "parent"}:
            with pytest.raises(StorageError):
                storage.initialize()
        else:
            key = new_storage_key(workspace if position == "workspace" else uuid4())
            if position == "temporary":
                with pytest.raises(StorageError):
                    with storage.temporary():
                        pytest.fail("Private scratch files must not follow a junction.")
            with pytest.raises(StorageError):
                storage.put(key, io.BytesIO(b"x"), max_bytes=1)
        assert not list(outside.iterdir())
    finally:
        if os.name == "nt":
            link.rmdir()  # Removes only the verified test junction, never its target.
        else:
            link.unlink()


def test_private_permissions(storage):
    if os.name != "nt":
        assert stat.S_IMODE(storage.root.stat().st_mode) == 0o700
    else:
        # Check the actual DACL, not chmod's emulated read-only flag on Windows.
        result = subprocess.run(
            [str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"), str(storage.root)],
            capture_output=True,
            text=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert "(I)" not in result.stdout
        assert storage_module._windows_user_sid()


def windows_dacl(path):
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    read = security.GetFileSecurityW
    read.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    read.restype = wintypes.BOOL
    size = wintypes.DWORD()
    read(str(path), 4, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    assert read(str(path), 4, buffer, size.value, ctypes.byref(size))
    convert = security.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    output = ctypes.c_void_p()
    assert convert(buffer, 1, 4, ctypes.byref(output), None)
    try:
        return ctypes.wstring_at(output)
    finally:
        kernel.LocalFree(output)


def test_reinitialization_secures_existing_permissive_children(storage):
    key = new_storage_key(uuid4())
    storage.put(key, io.BytesIO(b"preserved"), max_bytes=9)
    path = storage.object_path(key)
    if os.name == "nt":
        for target in (path.parent, path):
            subprocess.run(
                [
                    str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"),
                    str(target),
                    "/inheritance:r",
                    "/grant:r",
                    "*S-1-1-0:(F)",
                ],
                capture_output=True,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert ";;;WD)" in windows_dacl(target)
    else:
        path.parent.chmod(0o777)
        path.chmod(0o666)
    restored = PrivateStorage(storage.root)
    restored.initialize()
    with restored.open(key) as handle:
        assert handle.read() == b"preserved"
    if os.name == "nt":
        for target in (path.parent, path):
            dacl = windows_dacl(target)
            assert dacl.startswith("D:P")
            assert ";;;WD)" not in dacl
            assert dacl.count("(A;") == 3
    else:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_private_temporary_roundtrip_and_cleanup(storage):
    with storage.temporary() as handle:
        paths = list((storage.root / "tmp").iterdir())
        assert len(paths) == 1
        path = paths[0]
        assert UUID(hex=path.stem).version == 4
        assert path.suffix == ".part"
        assert handle.readable() and handle.writable() and handle.seekable()
        handle.write(b"private multipart bytes")
        handle.seek(0)
        assert handle.read() == b"private multipart bytes"
        if os.name == "nt":
            assert ";;;WD)" not in windows_dacl(path)
        else:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert handle.closed
    assert not path.exists()
    assert not list((storage.root / "tmp").iterdir())


def test_temporary_requires_initialization_without_creating_files(tmp_path):
    root = tmp_path / "uninitialized"
    storage = PrivateStorage(root)
    with pytest.raises(StorageError, match="Initialize"):
        with storage.temporary():
            pytest.fail("Uninitialized storage must not yield a scratch file.")
    assert not root.exists()


@pytest.mark.parametrize(
    "error", [ValueError("caller validation failure"), asyncio.CancelledError()]
)
def test_temporary_preserves_caller_error_and_cancellation_after_cleanup(storage, error):
    with pytest.raises(type(error)) as raised:
        with storage.temporary() as handle:
            handle.write(b"partial")
            raise error
    assert raised.value is error
    assert handle.closed
    assert not list((storage.root / "tmp").iterdir())


def test_temporary_caller_can_close_upload_file_early(storage):
    with storage.temporary() as handle:
        handle.write(b"parser complete")
        handle.close()
    assert handle.closed
    assert not list((storage.root / "tmp").iterdir())


def test_temporary_contexts_are_isolated_from_each_other_and_stored_objects(storage):
    key = new_storage_key(uuid4())
    storage.put(key, io.BytesIO(b"original"), max_bytes=8)
    with storage.temporary() as first:
        first.write(b"first")
        with storage.temporary() as second:
            second.write(b"second")
            assert len(list((storage.root / "tmp").iterdir())) == 2
        assert second.closed
        assert len(list((storage.root / "tmp").iterdir())) == 1
        first.seek(0)
        assert first.read() == b"first"
    with storage.open(key) as original:
        assert original.read() == b"original"
    assert not list((storage.root / "tmp").iterdir())


def test_temporary_creation_collision_does_not_remove_existing_file(storage, monkeypatch):
    fixed_id = uuid4()
    existing = storage.root / "tmp" / f"{fixed_id.hex}.part"
    existing.write_bytes(b"unrelated scratch content")
    monkeypatch.setattr(storage_module, "uuid4", lambda: fixed_id)
    with pytest.raises(StorageError):
        with storage.temporary():
            pytest.fail("Exclusive creation must not overwrite an existing scratch file.")
    assert existing.read_bytes() == b"unrelated scratch content"


def test_temporary_rejects_unexpected_temporary_directory_object(storage):
    temporary_directory = storage.root / "tmp"
    temporary_directory.rmdir()
    temporary_directory.write_bytes(b"untouched")
    with pytest.raises(StorageError):
        with storage.temporary():
            pytest.fail("A regular file is not a temporary directory.")
    assert temporary_directory.read_bytes() == b"untouched"


@pytest.mark.parametrize("failure", ["open", "fdopen"])
def test_temporary_setup_failure_is_sanitized_and_owned_file_removed(storage, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise OSError("sensitive-temporary-path")

    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(StorageError) as raised:
        with storage.temporary():
            pytest.fail("Scratch setup must fail.")
    assert "sensitive-temporary-path" not in str(raised.value)
    assert not list((storage.root / "tmp").iterdir())


def test_temporary_write_failure_is_sanitized_and_cleaned(storage):
    with pytest.raises(StorageError) as raised:
        with storage.temporary() as handle:
            handle.write(b"partial")
            raise OSError("sensitive-write-detail")
    assert "sensitive-write-detail" not in str(raised.value)
    assert handle.closed
    assert not list((storage.root / "tmp").iterdir())


def test_temporary_cleanup_failure_is_reported_without_deleting_unrelated_file(
    storage, monkeypatch
):
    unrelated = storage.root / "tmp" / f"{uuid4().hex}.part"
    unrelated.write_bytes(b"untouched")
    unlink = Path.unlink

    def fail_owned(path, *args, **kwargs):
        if path != unrelated:
            raise PermissionError("sensitive-cleanup-detail")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_owned)
    with pytest.raises(StorageError, match="Could not clean") as raised:
        with storage.temporary() as handle:
            handle.write(b"partial")
    assert "sensitive-cleanup-detail" not in str(raised.value)
    assert handle.closed
    assert unrelated.read_bytes() == b"untouched"


def test_temporary_cleanup_does_not_unlink_replacement_object(storage):
    with pytest.raises(StorageError, match="Could not clean"):
        with storage.temporary() as handle:
            temporary = next((storage.root / "tmp").iterdir())
            replacement = storage.root / "tmp" / f"{uuid4().hex}.part"
            replacement.write_bytes(b"replacement must survive")
            handle.close()
            temporary.unlink()
            replacement.rename(temporary)
    assert temporary.read_bytes() == b"replacement must survive"


def test_temporary_cleanup_rejects_unexpected_hardlink(storage, tmp_path):
    outside = tmp_path / "unexpected-link"
    with pytest.raises(StorageError, match="Could not clean"):
        with storage.temporary() as handle:
            handle.write(b"private")
            handle.flush()
            temporary = next((storage.root / "tmp").iterdir())
            os.link(temporary, outside)
    assert outside.read_bytes() == b"private"
    assert temporary.exists()

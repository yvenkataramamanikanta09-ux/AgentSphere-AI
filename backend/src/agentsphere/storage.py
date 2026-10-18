"""Private local objects. No HTTP routes or authorization decisions live here.

Only the application account may modify the root or its parents. Link checks
fail closed, but are not an OS sandbox against another process with that account.
"""

import csv
import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

KEY_PATTERN = re.compile(r"objects/[0-9a-f]{32}/[0-9a-f]{32}/original", re.ASCII)
PROJECT_DIR = Path(__file__).resolve().parents[3]


class StorageError(RuntimeError):
    """Sanitized operational failure; filesystem details stay out of API errors."""


class StorageConflict(StorageError):
    pass


class StorageLimitExceeded(StorageError):
    pass


def validate_storage_root(value: str | Path) -> Path:
    """Validate configuration without creating directories or resolving links."""
    if not isinstance(value, (str, Path)):
        raise ValueError("Private storage root must be a filesystem path.")
    raw = os.fspath(value)
    path = Path(raw)
    if (
        not raw
        or not path.is_absolute()
        or path == Path(path.anchor)
        or raw.startswith(("\\\\", "//"))
        or any(part == ".." for part in re.split(r"[/\\]", raw))
        or any(ord(char) < 32 for char in raw)
    ):
        raise ValueError("Private storage must be an absolute local directory without traversal.")
    protected = (
        PROJECT_DIR / "frontend",
        PROJECT_DIR / "backend" / "src",
        PROJECT_DIR / "backend" / "migrations",
        PROJECT_DIR / "backend" / "tests",
        PROJECT_DIR / ".git",
    )
    if any(path.is_relative_to(item) or item.is_relative_to(path) for item in protected):
        raise ValueError(
            "Private storage must be separate from public, source, and Git directories."
        )
    if Path.home().is_relative_to(path):
        raise ValueError("Private storage cannot be a home directory or its ancestor.")
    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        if any(
            part.endswith((".", " "))
            or any(char in '<>:"|?*' for char in part)
            or part.split(".", 1)[0].upper() in reserved
            for part in path.parts[1:]
        ):
            raise ValueError("Private storage contains an unsafe Windows path component.")
    return path


def new_storage_key(workspace_id: UUID, file_id: UUID | None = None) -> str:
    """Only server UUIDs form paths; client filenames never enter this namespace."""
    if not isinstance(workspace_id, UUID) or (
        file_id is not None and not isinstance(file_id, UUID)
    ):
        raise ValueError("Storage identifiers must be UUID objects.")
    return f"objects/{workspace_id.hex}/{(file_id or uuid4()).hex}/original"


def validate_storage_key(key: str) -> str:
    if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
        raise ValueError("Invalid private storage key.")
    return key


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


@lru_cache(maxsize=1)
def _windows_user_sid() -> str:
    executable = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
    result = subprocess.run(
        [str(executable), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sid = next(csv.reader(result.stdout.splitlines()))[1]
    if re.fullmatch(r"S-1-[0-9]+(?:-[0-9]+)+", sid) is None:
        raise StorageError("Could not establish private storage permissions.")
    return sid


def _restrict_permissions(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    # Protected inheritable DACL: current account, SYSTEM and Administrators only.
    # chmod alone does not provide private Windows ACLs.
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    convert.restype = wintypes.BOOL
    apply = security.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    inheritance = "OICI" if directory else ""
    sddl = "D:P" + "".join(
        f"(A;{inheritance};FA;;;{sid})" for sid in ("SY", "BA", _windows_user_sid())
    )
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise StorageError("Could not establish private storage permissions.")
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise StorageError("Could not establish private storage permissions.")
    finally:
        kernel.LocalFree(descriptor)


def _restrict_directory(path: Path) -> None:
    _restrict_permissions(path, directory=True)


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str


class PrivateStorage:
    """Explicit initialization; constructing Settings or starting FastAPI writes nothing.

    Publishing uses a same-volume hard link for atomic create-without-overwrite,
    then removes the private temporary name. NTFS and normal POSIX filesystems
    support this; unsupported filesystems fail closed.
    """

    def __init__(self, root: str | Path):
        self._root = validate_storage_root(root)
        self._initialized = False

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def _directories(
        path: Path, *, create_below: Path | None = None, allow_missing: bool = False
    ) -> None:
        for entry in (*reversed(path.parents), path):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                if create_below is not None and entry.is_relative_to(create_below):
                    try:
                        entry.mkdir(mode=0o700)
                    except FileExistsError:
                        pass
                    info = entry.lstat()
                elif allow_missing:
                    return
                else:
                    raise StorageError("Private storage directory is unavailable.") from None
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                raise StorageError("Unexpected object in private storage directory path.")

    @staticmethod
    def _regular(path: Path) -> os.stat_result:
        info = path.lstat()
        if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StorageError("Unexpected object in private storage.")
        return info

    def initialize(self) -> None:
        """Provision and secure the managed tree; its parent must already exist.

        Existing objects must follow our layout. No files are overwritten or
        removed. Restrict each child's permissions: Windows checks child ACLs
        independently, so a private parent alone cannot protect restored files.
        """
        self._initialized = False
        try:
            self._directories(self.root.parent)
            self._directories(self.root, create_below=self.root)
            directories, files = self._managed_tree()
            # Validate a mistaken root before changing ACLs/modes or adding
            # managed directories to unrelated existing content.
            _restrict_directory(self.root)
            for name in ("objects", "tmp"):
                directory = self.root / name
                self._directories(directory, create_below=self.root)
                _restrict_directory(directory)
            for directory in directories:
                _restrict_directory(directory)
            for file in files:
                _restrict_permissions(file, directory=False)
            self._initialized = True
        except (OSError, subprocess.SubprocessError, KeyError, IndexError):
            raise StorageError("Could not initialize private storage.") from None

    def _managed_tree(self) -> tuple[list[Path], list[Path]]:
        entries = {item.name for item in self.root.iterdir()}
        if not entries <= {"objects", "tmp"}:
            raise StorageError("Unexpected object in private storage root.")
        directories = []
        files = []
        for name in entries:
            self._directories(self.root / name)
        workspaces = (self.root / "objects").iterdir() if "objects" in entries else ()
        for workspace in workspaces:
            self._directories(workspace)
            if re.fullmatch(r"[0-9a-f]{32}", workspace.name) is None:
                raise StorageError("Unexpected object in private storage namespace.")
            directories.append(workspace)
            for object_directory in workspace.iterdir():
                self._directories(object_directory)
                if re.fullmatch(r"[0-9a-f]{32}", object_directory.name) is None:
                    raise StorageError("Unexpected object in private storage namespace.")
                directories.append(object_directory)
                for original in object_directory.iterdir():
                    if original.name != "original":
                        raise StorageError("Unexpected object in private storage namespace.")
                    self._regular(original)
                    files.append(original)
        temporaries = (self.root / "tmp").iterdir() if "tmp" in entries else ()
        for temporary in temporaries:
            if re.fullmatch(r"[0-9a-f]{32}\.part", temporary.name) is None:
                raise StorageError("Unexpected temporary storage object.")
            self._regular(temporary)
            files.append(temporary)
        return directories, files

    def object_path(self, key: str) -> Path:
        """Internal filesystem path only; never return it in a public schema."""
        validate_storage_key(key)
        if not self._initialized:
            raise StorageError("Initialize private storage before accessing objects.")
        path = self.root.joinpath(*key.split("/"))
        try:
            self._directories(self.root)
            self._directories(path.parent, allow_missing=True)
            try:
                self._regular(path)
            except FileNotFoundError:
                pass
        except OSError:
            raise StorageError("Private storage object is unavailable.") from None
        return path

    @contextmanager
    def temporary(self) -> Iterator[BinaryIO]:
        """Yield an owned private binary scratch file and remove it on every exit.

        The caller enforces request and parsing limits. This helper never uses
        the process-wide temporary directory or publishes a stored object.
        """
        if not self._initialized:
            raise StorageError("Initialize private storage before creating temporary objects.")
        temporary = self.root / "tmp" / f"{uuid4().hex}.part"
        descriptor = None
        handle = None
        identity = None
        created = False
        try:
            self._directories(self.root / "tmp")
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temporary, flags, 0o600)
            created = True
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            current = self._regular(temporary)
            if identity != (current.st_dev, current.st_ino):
                raise StorageError("Private temporary object changed unexpectedly.")
            handle = os.fdopen(descriptor, "w+b")
            descriptor = None  # The binary handle owns the descriptor from here.
            yield handle
        except OSError:
            raise StorageError("Could not access private temporary object.") from None
        finally:
            cleanup_failed = False
            try:
                if handle is not None:
                    handle.close()
                elif descriptor is not None:
                    os.close(descriptor)
            except OSError:
                cleanup_failed = True
            if created:
                try:
                    self._directories(self.root / "tmp")
                    current = self._regular(temporary)
                    if identity is not None and identity != (current.st_dev, current.st_ino):
                        raise StorageError("Private temporary object changed unexpectedly.")
                    temporary.unlink()
                except (OSError, StorageError):
                    cleanup_failed = True
            if cleanup_failed:
                raise StorageError("Could not clean private temporary object.") from None

    def put(self, key: str, source: BinaryIO, *, max_bytes: int) -> StoredObject:
        """Copy bounded bytes to a new immutable object; never replace an existing key."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("A positive byte limit is required.")
        destination = self.object_path(key)
        temporary = self.root / "tmp" / f"{uuid4().hex}.part"
        published = False
        created = False
        try:
            self._directories(self.root / "tmp")
            self._directories(destination.parent, create_below=self.root)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(temporary, flags, 0o600), "wb") as output:
                created = True
                digest = hashlib.sha256()
                total = 0
                while chunk := source.read(64 * 1024):
                    if not isinstance(chunk, bytes):
                        raise StorageError("Private storage requires a binary source.")
                    total += len(chunk)
                    if total > max_bytes:
                        raise StorageLimitExceeded("Private storage byte limit exceeded.")
                    output.write(chunk)
                    digest.update(chunk)
                if total == 0:
                    raise StorageError("Empty private storage objects are not supported.")
                output.flush()
                os.fsync(output.fileno())
            self._directories(destination.parent)
            os.link(temporary, destination, follow_symlinks=False)
            published = True
            temporary.unlink()
            created = False
            return StoredObject(key=key, size_bytes=total, sha256=digest.hexdigest())
        except FileExistsError:
            raise StorageConflict("Private storage key already exists.") from None
        except OSError:
            raise StorageError("Could not write private storage object.") from None
        finally:
            if created:
                # Only names created by this call; never traverse or recursively delete.
                try:
                    if published:
                        destination.unlink(missing_ok=True)
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise StorageError(
                        "Could not clean private storage temporary object."
                    ) from None

    def open(self, key: str) -> BinaryIO:
        """Caller closes the returned handle; authorization belongs in future services."""
        path = self.object_path(key)
        handle = None
        try:
            before = self._regular(path)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            handle = os.fdopen(os.open(path, flags), "rb")
            after = os.fstat(handle.fileno())
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or (
                not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
            ):
                raise StorageError("Private storage object changed unexpectedly.")
            return handle
        except (OSError, StorageError):
            if handle is not None:
                handle.close()
            raise StorageError("Could not read private storage object.") from None

    def delete(self, key: str) -> bool:
        """Idempotent object deletion. This does not delete directories or database rows."""
        path = self.object_path(key)
        try:
            self._regular(path)
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise StorageError("Could not delete private storage object.") from None

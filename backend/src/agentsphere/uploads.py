"""Bounded, private multipart intake. These checks do not establish content safety.

Call only after authentication, CSRF and workspace write authorization. Full
document validation belongs to processing; accepted files remain unvalidated.
"""

import asyncio
import codecs
import re
import stat
import zipfile
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from email import policy
from email.message import Message
from pathlib import PurePosixPath
from typing import BinaryIO

from fastapi import HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from agentsphere.schemas.documents import SUPPORTED_CONTENT_TYPES, DocumentMetadata
from agentsphere.storage import PrivateStorage

REQUEST_OVERHEAD_BYTES = 65_536
HEADER_BYTES = 8_192
BOUNDARY = re.compile(r"[0-9A-Za-z'()+_,./:=?-]{1,70}\Z", re.ASCII)


@dataclass(frozen=True)
class ReceivedUpload:
    file: BinaryIO
    metadata: DocumentMetadata


async def finish_thread_call(function, *args, **kwargs):
    """Join thread work before propagating cancellation and closing its input.

    Cancelling an asyncio await cannot stop a Python thread. Keep the worker
    shielded and join it, including after repeated cancellation requests, so its
    caller can safely close temporary files when this coroutine finally exits.
    """
    task = asyncio.create_task(run_in_threadpool(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


class PrivateUploadFile(UploadFile):
    """Disk-backed parser writes must finish before their private handle closes."""

    async def write(self, data: bytes) -> None:
        if self.size is not None:
            self.size += len(data)
        await finish_thread_call(self.file.write, data)

    async def read(self, size: int = -1) -> bytes:
        return await finish_thread_call(self.file.read, size)

    async def seek(self, offset: int) -> None:
        await finish_thread_call(self.file.seek, offset)

    async def close(self) -> None:
        await finish_thread_call(self.file.close)


def _parameters(value: str, header: str) -> tuple[str, dict[str, str]]:
    """Reject ambiguous and extended parameters instead of silently normalizing them."""
    strict = Message(policy=policy.default)
    try:
        strict[header] = value
        if strict[header].defects:
            raise HTTPException(400, "Malformed multipart header.")
    except ValueError:
        raise HTTPException(400, "Malformed multipart header.") from None
    message = Message()
    message[header] = value
    parts = message.get_params(header=header, unquote=True)
    if not parts:
        raise HTTPException(400, "Invalid multipart header.")
    parameters = {}
    for key, item in parts[1:]:
        key = key.lower()
        if key in parameters or "*" in key or not isinstance(item, str):
            raise HTTPException(400, "Invalid multipart header parameters.")
        parameters[key] = item
    return parts[0][0].strip().lower(), parameters


class _PrivateParser(MultiPartParser):
    def __init__(self, headers, stream, *, storage, stack, max_bytes):
        super().__init__(headers, stream, max_files=1, max_fields=0)
        self.storage = storage
        self.stack = stack
        self.max_bytes = max_bytes
        self.byte_count = 0
        self.header_bytes = 0
        self.part_count = 0
        self.ended = False
        self.metadata = None

    def on_part_begin(self) -> None:
        self.part_count += 1
        if self.part_count != 1:
            raise HTTPException(400, "Provide exactly one file part named file.")
        super().on_part_begin()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_bytes += end - start
        if self.header_bytes > HEADER_BYTES or (
            len(self._current_partial_header_name) + end - start > 128
        ):
            raise HTTPException(400, "Multipart headers exceed the allowed limit.")
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_bytes += end - start
        if self.header_bytes > HEADER_BYTES:
            raise HTTPException(400, "Multipart headers exceed the allowed limit.")
        super().on_header_value(data, start, end)

    def on_header_end(self) -> None:
        name = self._current_partial_header_name.lower()
        if name not in (b"content-disposition", b"content-type") or any(
            existing == name for existing, _ in self._current_part.item_headers
        ):
            raise HTTPException(400, "Unsupported or duplicate multipart header.")
        super().on_header_end()

    def on_headers_finished(self) -> None:
        try:
            raw = self._current_part.content_disposition.decode("utf-8", errors="strict")
        except UnicodeError:
            raise HTTPException(422, "Upload filename must use UTF-8.") from None
        # Never accept escaping that could hide a Windows path before validation.
        if "\\" in raw or any(ord(char) < 32 for char in raw):
            raise HTTPException(422, "Use a safe filename, not a path.")
        disposition, parameters = _parameters(raw, "content-disposition")
        if disposition != "form-data" or set(parameters) != {"name", "filename"}:
            raise HTTPException(400, "Provide exactly one file part named file.")
        if parameters["name"] != "file":
            raise HTTPException(400, "Provide exactly one file part named file.")
        filename = parameters["filename"]
        # Validate before deriving the suffix, to avoid platform-dependent path parsing.
        extension = PurePosixPath(filename).suffix.lower()
        canonical = SUPPORTED_CONTENT_TYPES.get(extension)
        if canonical is None:
            raise HTTPException(415, "Supported formats are PDF, DOCX, TXT and Markdown.")
        try:
            self.metadata = DocumentMetadata(
                filename=filename, content_type=canonical, size_bytes=1
            )
        except ValidationError:
            raise HTTPException(422, "Invalid upload filename.") from None
        headers = Headers(raw=self._current_part.item_headers)
        if content_type := headers.get("content-type"):
            declared, options = _parameters(content_type, "content-type")
            allowed = {canonical, "application/octet-stream"}
            if canonical == "text/markdown":
                allowed.add("text/plain")
            if declared not in allowed:
                raise HTTPException(415, "Content type does not match the filename.")
            if options and (
                set(options) != {"charset"}
                or options["charset"].lower() not in {"utf-8", "utf8"}
                or canonical not in {"text/plain", "text/markdown"}
            ):
                raise HTTPException(415, "Unsupported upload content type parameters.")
        handle = self.stack.enter_context(self.storage.temporary())
        self._current_part.field_name = "file"
        self._current_part.file = PrivateUploadFile(
            handle, size=0, filename=filename, headers=headers
        )

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self.byte_count += end - start
        if self.byte_count > self.max_bytes:
            raise HTTPException(413, "File exceeds the configured byte limit.")
        super().on_part_data(data, start, end)

    def on_end(self) -> None:
        self.ended = True


def _validate_docx(file: BinaryIO) -> None:
    try:
        with zipfile.ZipFile(file) as package:
            entries = package.infolist()
            if not entries or len(entries) > 1024:
                raise HTTPException(422, "DOCX package exceeds allowed entry limits.")
            names = set()
            uncompressed = 0
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name).parts
                mode = entry.external_attr >> 16
                if (
                    name in names
                    or entry.orig_filename != name
                    or name.startswith("/")
                    or "\\" in name
                    or ":" in name
                    or ".." in parts
                    or any(ord(char) < 32 for char in name)
                    or entry.flag_bits & 0x41
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or "vbaproject" in name.lower()
                    or name.lower().endswith(".bin")
                    or name.lower().startswith(("word/embeddings/", "word/activex/"))
                ):
                    raise HTTPException(422, "Unsupported or unsafe DOCX package entry.")
                names.add(name)
                uncompressed += entry.file_size
                if (
                    uncompressed > 40_000_000
                    or entry.file_size > 20_000_000
                    or entry.file_size > max(entry.compress_size, 1) * 100
                ):
                    raise HTTPException(422, "DOCX package exceeds expansion limits.")
            if not {"[Content_Types].xml", "word/document.xml"} <= names:
                raise HTTPException(422, "File lacks basic DOCX package evidence.")
    except (zipfile.BadZipFile, NotImplementedError, ValueError, OverflowError):
        raise HTTPException(422, "Invalid DOCX package.") from None


def validate_basic_format(file: BinaryIO, metadata: DocumentMetadata) -> None:
    """Check limited format evidence only; never mark an attachment validated here."""
    try:
        file.seek(0)
        prefix = file.read(8)
        file.seek(0)
        if metadata.content_type == "application/pdf":
            if not prefix.startswith(b"%PDF-"):
                raise HTTPException(415, "File lacks basic PDF format evidence.")
            file.seek(max(0, metadata.size_bytes - 1024))
            if b"%%EOF" not in file.read():
                raise HTTPException(422, "PDF ending marker is missing.")
        elif metadata.content_type.endswith("wordprocessingml.document"):
            if not prefix.startswith(b"PK\x03\x04"):
                raise HTTPException(415, "File lacks basic DOCX format evidence.")
            _validate_docx(file)
        else:
            if prefix.startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8", b"MZ")):
                raise HTTPException(415, "Binary file does not match the text filename.")
            decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
            while chunk := file.read(64 * 1024):
                text = decoder.decode(chunk)
                if any(
                    (ord(char) < 32 and char not in "\t\n\r") or 127 <= ord(char) <= 159
                    for char in text
                ):
                    raise HTTPException(422, "Text contains unsupported control characters.")
            decoder.decode(b"", final=True)
    except UnicodeError:
        raise HTTPException(422, "Text uploads must contain valid UTF-8.") from None
    finally:
        file.seek(0)


@asynccontextmanager
async def receive_upload(
    request: Request,
    storage: PrivateStorage,
    *,
    max_bytes: int,
    timeout_seconds: int,
) -> AsyncIterator[ReceivedUpload]:
    """Own every temporary handle until publication/finalization finishes or fails."""
    if type(max_bytes) is not int or max_bytes <= 0 or timeout_seconds <= 0:
        raise ValueError("Positive upload and timeout limits are required.")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "Encoded multipart requests are not supported.")
    content_type = request.headers.get("content-type", "")
    if len(content_type) > 512:
        raise HTTPException(400, "Invalid multipart content type.")
    media_type, options = _parameters(content_type, "content-type")
    boundary = options.get("boundary", "")
    if (
        media_type != "multipart/form-data"
        or set(options) - {"boundary", "charset"}
        or not BOUNDARY.fullmatch(boundary)
        or options.get("charset", "utf-8").lower() not in {"utf-8", "utf8"}
    ):
        raise HTTPException(400, "Use multipart/form-data with a valid boundary.")
    total_limit = max_bytes + REQUEST_OVERHEAD_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isascii() or not declared.isdecimal():
            raise HTTPException(400, "Invalid request length.")
        if len(declared) > 20 or int(declared) > total_limit:
            raise HTTPException(413, "Multipart request exceeds the allowed byte limit.")
    final_marker = b"\r\n--" + boundary.encode("ascii") + b"--"
    tail = b""
    marker_tail = b""
    closing_markers = 0

    async def bounded_stream():
        nonlocal tail, marker_tail, closing_markers
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > total_limit:
                raise HTTPException(413, "Multipart request exceeds the allowed byte limit.")
            # Bound parser callback buffers even when the ASGI server sends a huge chunk.
            for index in range(0, len(chunk), 64 * 1024):
                piece = chunk[index : index + 64 * 1024]
                # MultipartParser handles syntax; this framing guard prevents its
                # permissive epilogue handling from hiding bytes after on_end.
                framed = marker_tail + piece
                closing_markers += framed.count(final_marker)
                marker_tail = framed[-(len(final_marker) - 1) :]
                if closing_markers > 1:
                    raise HTTPException(400, "Unexpected data after multipart closing boundary.")
                tail = (tail + piece)[-(len(final_marker) + 2) :]
                yield piece

    with ExitStack() as stack:
        parser = _PrivateParser(
            request.headers, bounded_stream(), storage=storage, stack=stack, max_bytes=max_bytes
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                form = await parser.parse()
                if (
                    not parser.ended
                    or len(form) != 1
                    or parser.metadata is None
                    or not (tail.endswith(final_marker) or tail.endswith(final_marker + b"\r\n"))
                ):
                    raise HTTPException(400, "Incomplete or malformed multipart request.")
                if parser.byte_count == 0:
                    raise HTTPException(422, "Empty files are not supported.")
                uploaded = form["file"]
                metadata = parser.metadata.model_copy(update={"size_bytes": parser.byte_count})
                await finish_thread_call(validate_basic_format, uploaded.file, metadata)
        except TimeoutError:
            raise HTTPException(408, "Upload timed out.") from None
        except ClientDisconnect:
            raise HTTPException(400, "Upload was interrupted.") from None
        except (MultiPartException, ValueError):
            raise HTTPException(400, "Invalid multipart request.") from None
        yield ReceivedUpload(file=uploaded.file, metadata=metadata)

"""Private multipart intake, byte limits, and limited format-evidence checks."""

import asyncio
import io
import threading
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException, Request

from agentsphere.schemas.documents import SUPPORTED_CONTENT_TYPES, DocumentMetadata
from agentsphere.storage import PrivateStorage, StorageError
from agentsphere.uploads import (
    PrivateUploadFile,
    finish_thread_call,
    receive_upload,
    validate_basic_format,
)


@pytest.fixture
def storage(tmp_path):
    store = PrivateStorage(tmp_path / "private")
    store.initialize()
    return store


def multipart(data=b"Hello world\n", *, filename="notes.txt", mime="text/plain", boundary="sphere"):
    header = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"'
    if mime is not None:
        header += f"\r\nContent-Type: {mime}"
    return header.encode("utf-8") + b"\r\n\r\n" + data + f"\r\n--{boundary}--\r\n".encode()


def request_for(body, *, headers=None, fragment=65536, disconnect=False, delay=0):
    chunks = [body[index : index + fragment] for index in range(0, len(body), fragment)]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/upload",
        "headers": headers or [(b"content-type", b"multipart/form-data; boundary=sphere")],
    }

    async def receive():
        if delay:
            await asyncio.sleep(delay)
        if not chunks:
            return {"type": "http.disconnect" if disconnect else "http.request", "body": b""}
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks) or disconnect}

    return Request(scope, receive)


def intake(store, body, *, limit=10000000, **kwargs):
    async def execute():
        request = request_for(body, **kwargs)
        async with receive_upload(request, store, max_bytes=limit, timeout_seconds=30) as upload:
            assert not upload.file.closed
            content = upload.file.read()
            metadata = upload.metadata
        assert upload.file.closed
        return content, metadata

    return asyncio.run(execute())


def assert_rejected(store, body, status, **kwargs):
    with pytest.raises(HTTPException) as caught:
        intake(store, body, **kwargs)
    assert caught.value.status_code == status
    assert list((store.root / "tmp").iterdir()) == []
    assert list((store.root / "objects").iterdir()) == []


@pytest.mark.parametrize("fragment", [1, 3, 17, 65536])
def test_fragmented_upload_measures_actual_bytes_and_cleans_private_temp(storage, fragment):
    content, metadata = intake(storage, multipart(b"A\xe2\x82\xac\n"), fragment=fragment)
    assert content == b"A\xe2\x82\xac\n"
    assert metadata == DocumentMetadata(
        filename="notes.txt", content_type="text/plain", size_bytes=5
    )
    assert list((storage.root / "tmp").iterdir()) == []


@pytest.mark.parametrize("mime", [None, "application/octet-stream", "text/plain; charset=utf-8"])
def test_browser_mime_variants_are_canonicalized(storage, mime):
    _, metadata = intake(storage, multipart(mime=mime))
    assert metadata.content_type == "text/plain"


def test_markdown_browser_plaintext_mime_and_unicode_filename(storage):
    _, metadata = intake(
        storage, multipart(b"# Topic\n", filename="r\u00e9sum\u00e9.md", mime="text/plain")
    )
    assert metadata.filename == "r\u00e9sum\u00e9.md"
    assert metadata.content_type == "text/markdown"


def test_inclusive_ten_million_byte_file_limit(storage):
    data = b"a" * 10_000_000
    content, metadata = intake(storage, multipart(data))
    assert len(content) == metadata.size_bytes == 10_000_000


def test_file_over_limit_rejected_before_buffering(storage):
    assert_rejected(storage, multipart(b"a" * 101), 413, limit=100, fragment=13)


def test_spoofed_small_content_length_does_not_bypass_actual_limit(storage):
    headers = [
        (b"content-type", b"multipart/form-data; boundary=sphere"),
        (b"content-length", b"1"),
    ]
    assert_rejected(storage, multipart(b"a" * 101), 413, limit=100, headers=headers)


def test_actual_complete_multipart_limit_even_after_final_boundary(storage):
    assert_rejected(storage, multipart() + b"x" * 65537, 413, limit=100)


@pytest.mark.parametrize("declared", [b"x", b"-1", b"1.0", b"\xff", b"1" * 50])
def test_invalid_or_excessive_declared_lengths(storage, declared):
    headers = [
        (b"content-type", b"multipart/form-data; boundary=sphere"),
        (b"content-length", declared),
    ]
    expected = 413 if declared == b"1" * 50 else 400
    assert_rejected(storage, multipart(), expected, headers=headers)


@pytest.mark.parametrize(
    "filename",
    [
        "../notes.txt",
        "C:\\fakepath\\notes.txt",
        "/notes.txt",
        "AUX.txt",
        ".notes.txt",
        "a:notes.txt",
    ],
)
def test_unsafe_filename_is_rejected_without_basename_normalization(storage, filename):
    assert_rejected(storage, multipart(filename=filename), 422)


def test_filename_length_limit(storage):
    assert_rejected(storage, multipart(filename="a" * 252 + ".txt"), 422)


def test_unsupported_extension(storage):
    assert_rejected(storage, multipart(filename="code.exe"), 415)


def test_mime_contradiction(storage):
    assert_rejected(storage, multipart(mime="application/pdf"), 415)


@pytest.mark.parametrize("mime", ["text/plain; charset=latin-1", "text/plain; unknown=x"])
def test_unsupported_part_parameters(storage, mime):
    assert_rejected(storage, multipart(mime=mime), 415)


def test_empty_file(storage):
    assert_rejected(storage, multipart(b""), 422)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"garbage",
        multipart()[:-14],
        multipart() + b"trailing bytes",
        multipart() + b"trailing bytes\r\n--sphere--\r\n",
        multipart().replace(b'name="file"', b'name="other"'),
        multipart().replace(b'; filename="notes.txt"', b""),
        multipart().replace(b'name="file"', b'name="file"; name="file"'),
        multipart().replace(b'filename="notes.txt"', b"filename*=UTF-8''notes.txt"),
        multipart().replace(b'filename="notes.txt"', b'filename="notes.txt'),
        multipart().replace(
            b"Content-Type:", b"Content-Transfer-Encoding: binary\r\nContent-Type:"
        ),
        multipart().replace(
            b"Content-Type: text/plain", b"Content-Type: text/plain\r\nContent-Type: text/plain"
        ),
    ],
)
def test_malformed_or_ambiguous_multipart_is_rejected(storage, body):
    assert_rejected(storage, body, 400, fragment=7)


def test_multiple_files_rejected_and_all_temporary_files_cleaned(storage):
    body = multipart().replace(b"--sphere--\r\n", b"") + multipart()
    assert_rejected(storage, body, 400)


def test_header_byte_limit(storage):
    body = multipart().replace(b"Content-Type: text/plain", b"Content-Type: " + b"a" * 8192)
    assert_rejected(storage, body, 400, fragment=79)


def test_header_name_limit(storage):
    body = multipart().replace(b"Content-Type", b"a" * 129)
    assert_rejected(storage, body, 400, fragment=7)


@pytest.mark.parametrize(
    "content_type",
    [
        b"application/json",
        b"multipart/form-data",
        b"multipart/form-data; boundary=",
        b"multipart/form-data; boundary=" + b"a" * 71,
        b"multipart/form-data; boundary=sphere; boundary=other",
        b"multipart/form-data; boundary=sphere; charset=latin1",
    ],
)
def test_invalid_outer_content_type(storage, content_type):
    assert_rejected(storage, multipart(), 400, headers=[(b"content-type", content_type)])


def test_compressed_request_is_rejected(storage):
    headers = [
        (b"content-type", b"multipart/form-data; boundary=sphere"),
        (b"content-encoding", b"gzip"),
    ]
    assert_rejected(storage, multipart(), 415, headers=headers)


def test_disconnect_closes_and_removes_private_temporary(storage):
    assert_rejected(storage, multipart()[:-14], 400, disconnect=True)


def test_timeout_closes_and_removes_private_temporary(storage):
    async def execute():
        request = request_for(multipart(), fragment=64, delay=0.02)
        async with receive_upload(request, storage, max_bytes=100, timeout_seconds=0.05):
            pytest.fail("Timed out upload was accepted")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(execute())
    assert caught.value.status_code == 408
    assert list((storage.root / "tmp").iterdir()) == []


def test_caller_failure_also_closes_and_removes_private_temporary(storage):
    async def execute():
        async with receive_upload(
            request_for(multipart()), storage, max_bytes=100, timeout_seconds=1
        ):
            raise RuntimeError("Finalization failed")

    with pytest.raises(RuntimeError, match="Finalization failed"):
        asyncio.run(execute())
    assert list((storage.root / "tmp").iterdir()) == []


def test_cancellation_closes_and_removes_private_temporary(storage):
    async def execute():
        async with receive_upload(
            request_for(multipart()), storage, max_bytes=100, timeout_seconds=1
        ):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute())
    assert list((storage.root / "tmp").iterdir()) == []


class BlockingHandle:
    """Coordinate a real worker operation without depending on thread timing."""

    def __init__(self, handle, operation):
        self.handle = handle
        self.operation = operation
        self.started = threading.Event()
        self.release = threading.Event()

    def __getattr__(self, name):
        attribute = getattr(self.handle, name)
        if name != self.operation:
            return attribute

        def blocked(*args):
            self.started.set()
            if not self.release.wait(5):
                raise RuntimeError("Test did not release the worker")
            assert not self.handle.closed
            return attribute(*args)

        return blocked


async def cancel_while_worker_is_blocked(task, handle):
    assert await asyncio.to_thread(handle.started.wait, 5)
    # Deliver two independent cancellation requests while the worker owns the
    # handle. Yielding schedules cancellation; Events control the disk operation.
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not handle.closed


@pytest.mark.parametrize(
    "operation,args", [("write", (b"x",)), ("read", (1,)), ("seek", (0,)), ("close", ())]
)
def test_private_upload_file_joins_repeated_cancellation_before_closing(operation, args):
    handle = BlockingHandle(io.BytesIO(b"data"), operation)
    uploaded = PrivateUploadFile(handle, size=0)

    async def execute():
        task = asyncio.create_task(getattr(uploaded, operation)(*args))
        try:
            await cancel_while_worker_is_blocked(task, handle)
        finally:
            handle.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(execute())
        assert handle.closed == (operation == "close")
    finally:
        handle.handle.close()


@pytest.mark.parametrize("operation", ["write", "seek"])
def test_parser_does_not_close_temporary_during_cancelled_disk_operation(
    storage, monkeypatch, operation
):
    original = storage.temporary
    handles = []

    @contextmanager
    def temporary():
        with original() as handle:
            wrapped = BlockingHandle(handle, operation)
            handles.append(wrapped)
            yield wrapped

    monkeypatch.setattr(storage, "temporary", temporary)

    async def intake():
        async with receive_upload(
            request_for(multipart()), storage, max_bytes=100, timeout_seconds=10
        ):
            pytest.fail("Cancelled upload was accepted")

    async def execute():
        task = asyncio.create_task(intake())
        # Wait for the parser to create its handle without assuming scheduling.
        while not handles:
            await asyncio.sleep(0)
        wrapped = handles[0]
        try:
            await cancel_while_worker_is_blocked(task, wrapped)
            assert len(list((storage.root / "tmp").iterdir())) == 1
        finally:
            wrapped.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert wrapped.closed

    asyncio.run(execute())
    assert list((storage.root / "tmp").iterdir()) == []


def test_basic_validation_finishes_before_cancelled_upload_closes_temporary(storage, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    handles = []

    def validate(file, metadata):
        handles.append(file)
        started.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release validation")
        assert not file.closed
        validate_basic_format(file, metadata)

    monkeypatch.setattr("agentsphere.uploads.validate_basic_format", validate)

    async def intake():
        async with receive_upload(
            request_for(multipart()), storage, max_bytes=100, timeout_seconds=10
        ):
            pytest.fail("Cancelled upload was accepted")

    async def execute():
        task = asyncio.create_task(intake())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert not handles[0].closed
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handles[0].closed

    asyncio.run(execute())
    assert list((storage.root / "tmp").iterdir()) == []


def test_joined_thread_returns_result_and_preserves_worker_error():
    async def execute():
        assert await finish_thread_call(lambda: "finished") == "finished"

        def failed():
            raise ValueError("worker failed")

        with pytest.raises(ValueError, match="worker failed"):
            await finish_thread_call(failed)

    asyncio.run(execute())


def test_cancellation_still_wins_if_joined_worker_fails():
    started = threading.Event()
    release = threading.Event()

    def failed():
        started.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release worker")
        raise ValueError("worker failed after cancellation")

    async def execute():
        task = asyncio.create_task(finish_thread_call(failed))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(execute())


def test_storage_failure_does_not_escape_with_a_temporary_file(storage, monkeypatch):
    def unavailable():
        raise StorageError("Storage unavailable")

    monkeypatch.setattr(storage, "temporary", unavailable)
    with pytest.raises(StorageError, match="Storage unavailable"):
        intake(storage, multipart())
    assert list((storage.root / "tmp").iterdir()) == []


def docx_bytes(entries=None, *, compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as package:
        for name, content in entries or [
            ("[Content_Types].xml", b"<Types/>"),
            ("word/document.xml", b"<document/>"),
        ]:
            package.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "filename,data",
    [
        ("notes.pdf", b"%PDF-1.7\nnot yet structurally validated\n%%EOF\n"),
        ("notes.docx", docx_bytes()),
        ("notes.txt", b"UTF-8 \xe2\x82\xac\n"),
        ("notes.markdown", b"# Heading\n\nText\n"),
    ],
)
def test_limited_format_evidence_accepts_supported_inputs_and_rewinds(filename, data):
    metadata = DocumentMetadata(
        filename=filename,
        content_type=SUPPORTED_CONTENT_TYPES[Path(filename).suffix],
        size_bytes=len(data),
    )
    file = io.BytesIO(data)
    validate_basic_format(file, metadata)
    assert file.tell() == 0
    assert not file.closed


@pytest.mark.parametrize(
    "filename,data,status",
    [
        ("notes.pdf", b"not a pdf", 415),
        ("notes.pdf", b"%PDF-1.7 no ending", 422),
        ("notes.docx", b"not a docx", 415),
        ("notes.docx", b"PK\x03\x04broken", 422),
        ("notes.docx", docx_bytes([("word/document.xml", b"x")]), 422),
        ("notes.txt", b"\x80", 422),
        ("notes.txt", b"incomplete \xe2\x82", 422),
        ("notes.md", b"a\x00b", 422),
        ("notes.txt", b"a\x1bb", 422),
        ("notes.txt", b"%PDF-1.7", 415),
        ("notes.txt", b"PK\x03\x04content", 415),
    ],
)
def test_wrong_format_or_binary_text_rejected_and_rewound(filename, data, status):
    metadata = DocumentMetadata(
        filename=filename,
        content_type=SUPPORTED_CONTENT_TYPES[Path(filename).suffix],
        size_bytes=len(data),
    )
    file = io.BytesIO(data)
    with pytest.raises(HTTPException) as caught:
        validate_basic_format(file, metadata)
    assert caught.value.status_code == status
    assert file.tell() == 0


@pytest.mark.parametrize(
    "entry",
    [
        "../outside.txt",
        "/outside.txt",
        "C:/outside.txt",
        "word\\document2.xml",
        "word/vbaProject.bin",
        "word/embeddings/object.dat",
        "word/activeX/object.xml",
    ],
)
def test_unsafe_docx_entries_rejected(storage, entry):
    data = docx_bytes(
        [("[Content_Types].xml", b"<Types/>"), ("word/document.xml", b"<document/>"), (entry, b"x")]
    )
    if "\\" in entry:
        # Windows ZipInfo normalizes separators while writing. Put the hostile
        # bytes into both directory records to test a package received over HTTP.
        data = data.replace(entry.replace("\\", "/").encode(), entry.encode())
    assert_rejected(storage, multipart(data, filename="notes.docx", mime=None), 422)


def test_docx_expansion_ratio_rejected_without_decompression(storage):
    data = docx_bytes(
        [("[Content_Types].xml", b"x"), ("word/document.xml", b"a" * 100000)],
        compression=zipfile.ZIP_DEFLATED,
    )
    assert_rejected(storage, multipart(data, filename="notes.docx", mime=None), 422)


def test_docx_entry_count_limit(storage):
    entries = [("[Content_Types].xml", b"x"), ("word/document.xml", b"x")]
    entries += [(f"word/part{i}.xml", b"x") for i in range(1023)]
    assert_rejected(storage, multipart(docx_bytes(entries), filename="notes.docx", mime=None), 422)

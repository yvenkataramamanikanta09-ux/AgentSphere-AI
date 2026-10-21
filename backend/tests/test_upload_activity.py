"""Upload acquisition waits asynchronously; protected request bodies never retry."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agentsphere.api import document_files
from agentsphere.storage import StorageBusy, StorageError


def test_upload_wait_yields_and_only_retries_acquisition(monkeypatch):
    attempts, waits, releases, bodies = [], [], [], []

    @contextmanager
    def activity():
        attempts.append(True)
        if len(attempts) < 3:
            raise StorageBusy("Concurrent initialization")
        try:
            yield
        finally:
            releases.append(True)

    async def wait(delay):
        waits.append(delay)

    monkeypatch.setattr(document_files.asyncio, "sleep", wait)

    async def run():
        async with document_files.upload_activity(SimpleNamespace(activity=activity)):
            bodies.append(True)

    asyncio.run(run())
    assert len(attempts) == 3
    assert waits == [0.01, 0.01]
    assert releases == bodies == [True]


def test_upload_activity_does_not_retry_body_busy_errors():
    attempts, releases = [], []

    @contextmanager
    def activity():
        attempts.append(True)
        try:
            yield
        finally:
            releases.append(True)

    async def run():
        async with document_files.upload_activity(SimpleNamespace(activity=activity)):
            raise StorageBusy("Protected-body failure")

    with pytest.raises(StorageBusy, match="Protected-body failure"):
        asyncio.run(run())
    assert attempts == releases == [True]


@pytest.mark.parametrize("error", [StorageBusy("Busy"), StorageError("Unsafe object")])
def test_upload_activity_timeout_and_unsafe_objects_do_not_enter_body(monkeypatch, error):
    attempts = []
    clock = iter((0, 3))
    monkeypatch.setattr(document_files, "time", SimpleNamespace(monotonic=lambda: next(clock)))

    @contextmanager
    def activity():
        attempts.append(True)
        raise error
        yield  # pragma: no cover - preserves the context-manager contract.

    async def run():
        async with document_files.upload_activity(SimpleNamespace(activity=activity)):
            pytest.fail("A rejected activity fence must not enter the request body.")

    with pytest.raises(type(error), match=str(error)):
        asyncio.run(run())
    assert attempts == [True]


def test_upload_cancel_while_waiting_does_not_enter_or_retry_body(monkeypatch):
    attempts, entered = [], []

    @contextmanager
    def activity():
        attempts.append(True)
        raise StorageBusy("Busy")
        yield  # pragma: no cover

    async def run():
        waiting = asyncio.Event()

        async def wait(delay):
            waiting.set()
            await asyncio.Future()

        monkeypatch.setattr(document_files.asyncio, "sleep", wait)

        async def request():
            async with document_files.upload_activity(SimpleNamespace(activity=activity)):
                entered.append(True)

        task = asyncio.create_task(request())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert attempts == [True]
    assert entered == []

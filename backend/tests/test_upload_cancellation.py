"""Native asyncio cancellation must not abandon a side-effect worker thread."""

import asyncio
from threading import Event

import pytest

from agentsphere.api import document_files


def test_finish_thread_call_returns_result_and_forwards_arguments():
    def add(first, *, second):
        return first + second

    assert asyncio.run(document_files.finish_thread_call(add, 2, second=3)) == 5


def test_finish_thread_call_preserves_worker_exception():
    error = ValueError("test-only worker failure")

    def fail():
        raise error

    with pytest.raises(ValueError) as raised:
        asyncio.run(document_files.finish_thread_call(fail))
    assert raised.value is error


@pytest.mark.parametrize("cancellations", [1, 3])
@pytest.mark.parametrize("worker_fails", [False, True])
def test_finish_thread_call_joins_before_propagating_native_cancellation(
    cancellations, worker_fails
):
    started, release, completed = Event(), Event(), Event()

    def blocked():
        started.set()
        assert release.wait(5), "Test did not release its worker thread."
        completed.set()
        if worker_fails:
            raise ValueError("Failure after native cancellation must still be joined.")
        return "committed result"

    async def scenario():
        task = asyncio.create_task(document_files.finish_thread_call(blocked))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            for _ in range(cancellations):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "Cancellation abandoned a running side-effect thread."
                assert not completed.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert completed.is_set()
        finally:
            release.set()
            await asyncio.to_thread(completed.wait, 5)
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

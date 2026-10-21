"""Mailbox publication regressions; no application database/storage is accessed."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

spec = importlib.util.spec_from_file_location("e2e_runner", Path(__file__).with_name("run_e2e.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_atomic_json_replaces_a_complete_value(tmp_path):
    target = tmp_path / "mailbox.json"
    runner.atomic_json(target, {"action": "pause"})
    runner.atomic_json(target, {"action": "resume"})
    assert target.read_text() == '{"action": "resume"}'
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_json_does_not_retry_unrelated_filesystem_failure(tmp_path, monkeypatch):
    target = tmp_path / "mailbox.json"
    target.write_text("original")
    calls = []

    def fail(*args):
        calls.append(args)
        raise OSError("Synthetic filesystem failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="Synthetic"):
        runner.atomic_json(target, {"action": "pause"})
    assert len(calls) == 1
    assert target.read_text() == "original"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name != "nt", reason="Windows handle sharing is platform-specific")
def test_atomic_json_retries_an_actual_windows_reader_handle(tmp_path, monkeypatch):
    target = tmp_path / "mailbox.json"
    target.write_text("original")
    replace = Path.replace
    attempted = Event()
    blocked = Event()

    def publish(temporary, destination):
        attempted.set()
        try:
            return replace(temporary, destination)
        except PermissionError:
            blocked.set()
            raise

    monkeypatch.setattr(Path, "replace", publish)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with target.open():
            future = executor.submit(runner.atomic_json, target, {"action": "resume"})
            assert attempted.wait(timeout=1)
            assert blocked.wait(timeout=1)
        future.result(timeout=3)
    assert target.read_text() == '{"action": "resume"}'
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name != "nt", reason="Windows access errors are platform-specific")
def test_atomic_json_stops_when_the_sharing_retry_budget_expires(tmp_path, monkeypatch):
    target = tmp_path / "mailbox.json"
    target.write_text("original")
    ticks = iter([0, 3])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    with target.open(), pytest.raises(PermissionError):
        runner.atomic_json(target, {"action": "pause"})
    assert target.read_text() == "original"
    assert list(tmp_path.iterdir()) == [target]

"""Killable parser subprocess with bounded pipes and a minimal environment."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from time import monotonic

import pydantic
from pydantic import ValidationError

from agentsphere.extraction import ExtractionError, ExtractionResult
from agentsphere.process_limits import ResourceLimitsUnavailable, WindowsJob


def parser_environment():
    # Exclude DATABASE_URL, keys, tokens, proxy credentials and user PYTHONPATH.
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    environment.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return environment


def parser_command():
    if os.name == "nt":
        # Windows venv python.exe is a redirector that spawns base Python. With
        # an active-process limit of one that either fails or races containment.
        # Start the real interpreter directly, using only our trusted venv paths.
        bootstrap = (
            "import runpy,site,sys;"
            f"site.addsitedir({str(Path(pydantic.__file__).resolve().parent.parent)!r});"
            f"sys.argv=['agentsphere.parser_process',{str(os.getpid())!r}];"
            "runpy.run_module('agentsphere.parser_process',run_name='__main__')"
        )
        return [sys._base_executable, "-I", "-B", "-c", bootstrap]
    return [sys.executable, "-I", "-B", "-m", "agentsphere.parser_process", str(os.getpid())]


def terminate(process):
    if process.poll() is None:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def _run_parser(payload, *, timeout, output_limit, memory_mb, cancelled, command=None):
    """All pipe readers/writers are bounded; stderr never enters parent memory."""
    try:
        process = subprocess.Popen(
            command or parser_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=parser_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
            bufsize=0,
        )
    except OSError:
        raise ExtractionError("parser_unavailable", retryable=True) from None
    containment = None
    oversized, pipe_error = Event(), Event()
    output = bytearray()

    def read_output():
        try:
            while piece := process.stdout.read(min(65_536, output_limit + 1 - len(output))):
                output.extend(piece)
                if len(output) > output_limit:
                    oversized.set()
                    terminate(process)
                    return
        except (OSError, ValueError):
            pipe_error.set()

    def write_input():
        try:
            with process.stdin as stream:
                # Raw FileIO.write may write only part of a large buffer.
                view = memoryview(payload)
                while view:
                    written = stream.write(view[:65_536])
                    if not written:
                        raise OSError
                    view = view[written:]
        except (OSError, ValueError):
            pipe_error.set()

    reader = writer = None
    try:
        # Child waits for stdin until the job is attached. Fail closed on Windows
        # if nested jobs/permissions prevent installing the promised hard limit.
        containment = WindowsJob(process, memory_mb)
        reader = Thread(target=read_output, name="parser-output", daemon=True)
        writer = Thread(target=write_input, name="parser-input", daemon=True)
        reader.start()
        writer.start()
        deadline = monotonic() + timeout
        while process.poll() is None:
            if cancelled():
                raise ExtractionError("worker_interrupted", retryable=True)
            if oversized.is_set():
                raise ExtractionError("parser_output_limit")
            if monotonic() >= deadline:
                raise ExtractionError("extraction_timeout")
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=5)
        writer.join(timeout=5)
        if reader.is_alive() or writer.is_alive() or oversized.is_set():
            raise ExtractionError("parser_output_limit")
        if process.returncode != 0 or pipe_error.is_set():
            raise ExtractionError("parser_failed")
        return bytes(output)
    except ResourceLimitsUnavailable:
        raise ExtractionError("resource_limits_unavailable", retryable=True) from None
    except OSError:
        raise ExtractionError("parser_unavailable", retryable=True) from None
    finally:
        if containment is not None:
            containment.close()
        terminate(process)
        if reader is not None:
            reader.join(timeout=5)
        if writer is not None:
            writer.join(timeout=5)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def extract_in_subprocess(data, content_type, settings, *, cancelled=lambda: False):
    if not isinstance(data, bytes) or not 1 <= len(data) <= min(
        settings.max_upload_bytes, 10_000_000
    ):
        raise ExtractionError("input_size_limit")
    request = {
        "size": len(data),
        "content_type": content_type,
        "limits": settings.extraction_limits.model_dump(),
        "memory_mb": settings.extraction_memory_mb,
        "output_limit": settings.extraction_output_bytes,
    }
    header = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    raw = _run_parser(
        header + data,
        timeout=settings.extraction_timeout_seconds,
        output_limit=settings.extraction_output_bytes,
        memory_mb=settings.extraction_memory_mb,
        cancelled=cancelled,
    )
    try:
        response = json.loads(raw)
        if not isinstance(response, dict):
            raise ValueError
        if "error" in response:
            # Never trust arbitrary child output as a log/API error string.
            import re

            code = response["error"]
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                raise ValueError
            raise ExtractionError(code, retryable=response.get("retryable") is True)
        result = ExtractionResult.model_validate(response["result"])
        if len(result.sections) > settings.extraction_max_sections:
            raise ValueError
        if sum(len(section.text.encode("utf-8")) for section in result.sections) > (
            settings.extraction_max_text_bytes
        ):
            raise ValueError
        return result
    except (KeyError, ValueError, TypeError, ValidationError):
        raise ExtractionError("invalid_extraction_result") from None

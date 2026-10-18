"""Real child-process checks for parser isolation, bounded I/O, and termination."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest
from test_extraction import DOCX, PDF, docx_fixture, pdf_fixture

from agentsphere import sandbox
from agentsphere.config import Settings
from agentsphere.extraction import ExtractionError
from agentsphere.process_limits import ResourceLimitsUnavailable, WindowsJob


@pytest.fixture
def parser_settings():
    return Settings(_env_file=None, database_url=None, test_database_url=None)


@pytest.fixture
def parser_children(monkeypatch):
    """Keep test failures from leaving any synthetic parser process running."""
    original = subprocess.Popen
    children = []

    def record(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(sandbox.subprocess, "Popen", record)
    yield children
    for process in children:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def python_command(code):
    # A Windows venv python.exe is a redirector that would spawn another
    # interpreter, violating the production job's one-process restriction.
    executable = sys._base_executable if os.name == "nt" else sys.executable
    return [executable, "-I", "-c", code]


def run_command(code, *, payload=b"input", timeout=5, output_limit=65_536, cancelled=lambda: False):
    return sandbox._run_parser(
        payload,
        timeout=timeout,
        output_limit=output_limit,
        memory_mb=128,
        cancelled=cancelled,
        command=python_command(code),
    )


def protocol_request(settings, data=b"abc", content_type="text/plain", **overrides):
    request = {
        "size": len(data),
        "content_type": content_type,
        "limits": settings.extraction_limits.model_dump(),
        "memory_mb": settings.extraction_memory_mb,
        "output_limit": settings.extraction_output_bytes,
    }
    request.update(overrides)
    return json.dumps(request).encode() + b"\n" + data


@pytest.mark.parametrize(
    ("data", "content_type", "expected"),
    [
        (b"A plain document", "text/plain", "A plain document"),
        (b"# Heading\n\nMarkdown body", "text/markdown", "Heading"),
        (pdf_fixture("PDF body"), PDF, "PDF body"),
        (docx_fixture(), DOCX, "Plan"),
    ],
    ids=["txt", "markdown", "pdf", "docx"],
)
def test_real_parser_extracts_each_admitted_format(data, content_type, expected, parser_settings):
    result = sandbox.extract_in_subprocess(data, content_type, parser_settings)
    assert result.sections[0].text == expected
    assert result.parser_version == "m5-stage3-v1"


def test_default_output_budget_accommodates_repeated_unicode_headings(parser_settings):
    heading = "\U0001f600" * 500
    data = ("# " + heading + "\n\n" + "\n\n".join("a" for _ in range(4_999))).encode()
    result = sandbox.extract_in_subprocess(data, "text/markdown", parser_settings)
    assert len(result.sections) == 5_000
    assert result.sections[0].text == heading
    assert all(section.heading == heading for section in result.sections)
    assert result.sections[-1].text == "a"


def test_real_parser_retains_empty_pdf_warning(parser_settings):
    result = sandbox.extract_in_subprocess(pdf_fixture(None), PDF, parser_settings)
    assert result.sections == []
    assert result.warning_code == "no_extractable_text"


@pytest.mark.parametrize(
    ("data", "content_type", "code"),
    [
        (b"not a PDF", PDF, "invalid_pdf"),
        (pdf_fixture(None, password="secret"), PDF, "encrypted_pdf"),
        (b"not a ZIP", DOCX, "invalid_docx"),
        (b"bad\xff", "text/plain", "invalid_utf8"),
        (b"bad\x00", "text/markdown", "unsafe_text"),
    ],
    ids=["invalid_pdf", "encrypted_pdf", "invalid_docx", "invalid_utf8", "unsafe_text"],
)
def test_real_parser_returns_safe_format_errors(data, content_type, code, parser_settings):
    with pytest.raises(ExtractionError) as caught:
        sandbox.extract_in_subprocess(data, content_type, parser_settings)
    assert caught.value.code == code
    assert str(caught.value) == code


def test_real_timeout_kills_child_and_closes_pipes(parser_children):
    started = monotonic()
    with pytest.raises(ExtractionError) as caught:
        run_command("import sys,time;sys.stdin.buffer.read();time.sleep(60)", timeout=0.2)
    assert caught.value.code == "extraction_timeout"
    assert monotonic() - started < 5
    assert parser_children[0].poll() is not None
    assert parser_children[0].stdin.closed and parser_children[0].stdout.closed


def test_cancellation_kills_child_without_waiting_for_parser(parser_children):
    started = monotonic()
    with pytest.raises(ExtractionError) as caught:
        run_command(
            "import sys,time;sys.stdin.buffer.read();time.sleep(60)", cancelled=lambda: True
        )
    assert caught.value.code == "worker_interrupted"
    assert caught.value.retryable
    assert monotonic() - started < 5
    assert parser_children[0].poll() is not None


def test_flooded_stdout_is_bounded_and_child_is_killed(parser_children):
    with pytest.raises(ExtractionError) as caught:
        run_command(
            "import os,sys,time;sys.stdin.buffer.read();os.write(1,b'x'*1_000_000);time.sleep(60)",
            output_limit=65_536,
        )
    assert caught.value.code == "parser_output_limit"
    assert parser_children[0].poll() is not None


def test_stderr_is_discarded_without_blocking_or_entering_stdout():
    output = run_command(
        "import os,sys;sys.stdin.buffer.read();os.write(2,b'x'*2_000_000);os.write(1,b'ok')"
    )
    assert output == b"ok"


def test_large_pipe_input_is_written_completely():
    payload = bytes(range(256)) * 4_096
    output = run_command(
        "import sys,hashlib;data=sys.stdin.buffer.read();"
        "print(str(len(data))+':'+hashlib.sha256(data).hexdigest(),end='')",
        payload=payload,
    )
    assert output.decode() == f"{len(payload)}:{hashlib.sha256(payload).hexdigest()}"


def test_containment_failure_is_closed_and_child_is_reaped(monkeypatch, parser_children):
    def unavailable(*args):
        raise ResourceLimitsUnavailable("synthetic containment failure")

    monkeypatch.setattr(sandbox, "WindowsJob", unavailable)
    with pytest.raises(ExtractionError) as caught:
        run_command("import sys,time;sys.stdin.buffer.read();time.sleep(60)")
    assert caught.value.code == "resource_limits_unavailable"
    assert caught.value.retryable
    assert parser_children[0].poll() is not None


def test_launch_environment_excludes_credentials_and_user_pythonpath(monkeypatch):
    sensitive = {
        "DATABASE_URL": "synthetic-database-credential",
        "TEST_DATABASE_URL": "synthetic-test-database-credential",
        "AUTH_RATE_LIMIT_KEY": "synthetic-authentication-secret",
        "AWS_SECRET_ACCESS_KEY": "synthetic-cloud-secret",
        "HTTP_PROXY": "http://synthetic-proxy-credential.invalid",
        "PYTHONPATH": "synthetic-untrusted-pythonpath",
    }
    for name, value in sensitive.items():
        monkeypatch.setenv(name, value)
    environment = sandbox.parser_environment()
    assert set(environment) <= {
        "SystemRoot",
        "WINDIR",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
    }
    output = run_command(
        "import json,os,sys;sys.stdin.buffer.read();print(json.dumps(dict(os.environ)))"
    )
    observed = json.loads(output)
    assert all(name not in observed for name in sensitive)
    assert all(value not in output.decode() for value in sensitive.values())


def test_parser_child_clears_environment_and_never_reads_dotenv(monkeypatch, parser_settings):
    # Audit the real parser module's imports and file access. If it starts using
    # Settings or opens any .env, the child fails instead of returning valid text.
    original = sandbox.parser_command()
    guard = """
import os, sys
def audit(event, args):
    if event == 'open' and isinstance(args[0], str) and args[0].endswith('.env'):
        raise RuntimeError('dotenv access prohibited in parser child')
    if event == 'import' and args[0] in ('agentsphere.config', 'agentsphere.database'):
        raise RuntimeError('configuration import prohibited in parser child')
    if event == 'import' and args[0] == 'agentsphere.extraction' and os.environ:
        raise RuntimeError('environment must be cleared before parser import')
sys.addaudithook(audit)
"""
    if "-c" in original:
        command = original[:-1] + [guard + original[-1]]
    else:
        # Preserve isolated module loading and the real parent PID on POSIX.
        command = python_command(
            guard
            + "\nimport runpy;sys.argv="
            + repr(["agentsphere.parser_process", str(os.getpid())])
            + ";runpy.run_module('agentsphere.parser_process',run_name='__main__')"
        )
    monkeypatch.setattr(sandbox, "parser_command", lambda: command)
    result = sandbox.extract_in_subprocess(b"No configuration", "text/plain", parser_settings)
    assert result.sections[0].text == "No configuration"


@pytest.mark.parametrize(
    "response",
    [
        b"",
        b"not JSON",
        b"[]",
        b'{"result":{}}',
        b'{"error":"/private/synthetic/path"}',
        b'{"error":"bad\\n"}',
        b'{"result":{"sections":[{"text":"a","line_start":2}]}}',
        b'{"result":{"sections":[],"parser_version":" "}}',
    ],
)
def test_real_child_malformed_json_or_models_are_rejected(response, monkeypatch, parser_settings):
    command = python_command(
        f"import sys;sys.stdin.buffer.read();sys.stdout.buffer.write({response!r})"
    )
    monkeypatch.setattr(sandbox, "parser_command", lambda: command)
    with pytest.raises(ExtractionError) as caught:
        sandbox.extract_in_subprocess(b"abc", "text/plain", parser_settings)
    assert caught.value.code == "invalid_extraction_result"


def test_parent_rechecks_configured_section_limit(monkeypatch):
    response = json.dumps({"result": {"sections": [{"text": "a"}, {"text": "b"}]}}).encode()
    command = python_command(
        f"import sys;sys.stdin.buffer.read();sys.stdout.buffer.write({response!r})"
    )
    monkeypatch.setattr(sandbox, "parser_command", lambda: command)
    settings = Settings(
        _env_file=None, database_url=None, test_database_url=None, extraction_max_sections=1
    )
    with pytest.raises(ExtractionError) as caught:
        sandbox.extract_in_subprocess(b"abc", "text/plain", settings)
    assert caught.value.code == "invalid_extraction_result"


@pytest.mark.parametrize("protocol_data", [b"not JSON\nabc", b"{}\nabc", b"x" * 16_385])
def test_child_rejects_malformed_protocol_with_safe_error(protocol_data, parser_settings):
    response = sandbox._run_parser(
        protocol_data,
        timeout=5,
        output_limit=65_536,
        memory_mb=parser_settings.extraction_memory_mb,
        cancelled=lambda: False,
    )
    assert json.loads(response) == {"error": "parser_failed", "retryable": False}


def test_child_rejects_mismatched_declared_input_size(parser_settings):
    response = sandbox._run_parser(
        protocol_request(parser_settings, size=2),
        timeout=5,
        output_limit=65_536,
        memory_mb=parser_settings.extraction_memory_mb,
        cancelled=lambda: False,
    )
    assert json.loads(response) == {"error": "parser_failed", "retryable": False}


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_production_windows_command_uses_real_interpreter():
    assert sandbox.parser_command()[0] == sys._base_executable
    assert "-I" in sandbox.parser_command()
    assert "-B" in sandbox.parser_command()


@pytest.mark.skipif(os.name != "nt", reason="Windows base-interpreter worker launch")
def test_parser_works_when_worker_uses_base_interpreter_with_venv_packages():
    packages = str(Path(sandbox.pydantic.__file__).resolve().parent.parent)
    code = (
        "import site;"
        f"site.addsitedir({packages!r});"
        "from agentsphere.config import Settings;"
        "from agentsphere.sandbox import extract_in_subprocess;"
        "s=Settings(_env_file=None,database_url=None,test_database_url=None);"
        "r=extract_in_subprocess(b'Base worker evidence','text/plain',s);"
        "print(r.sections[0].text)"
    )
    result = subprocess.run(
        [sys._base_executable, "-I", "-B", "-c", code],
        env=sandbox.parser_environment() | {"USERPROFILE": str(Path.home())},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Base worker evidence"


def test_unavailable_interpreter_is_a_sanitized_retryable_error(monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("Injected private interpreter path")

    monkeypatch.setattr(sandbox.subprocess, "Popen", unavailable)
    with pytest.raises(ExtractionError) as caught:
        run_command("pass")
    assert caught.value.code == "parser_unavailable" and caught.value.retryable
    assert "private" not in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_windows_job_enforces_memory_limit():
    code = """
import sys
sys.stdin.buffer.read(1)
try:
    allocation = bytearray(256 * 1024 * 1024)
except MemoryError:
    print('limited', flush=True)
else:
    print('unlimited', flush=True)
"""
    with subprocess.Popen(
        python_command(code),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        job = WindowsJob(process, 128)
        try:
            output, _ = process.communicate(b"x", timeout=5)
            assert process.returncode == 0
            assert output.strip() == b"limited"
        finally:
            job.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_windows_job_kill_on_close_terminates_child():
    with subprocess.Popen(
        python_command(
            "import sys,time;sys.stdin.buffer.read(1);time.sleep(60);print('completed')"
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        job = WindowsJob(process, 128)
        process.stdin.write(b"x")
        process.stdin.flush()
        started = monotonic()
        job.close()
        process.wait(timeout=5)
        assert monotonic() - started < 5
        assert process.stdout.read() == b""
        job.close()  # Closing an already closed containment handle is harmless.


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_windows_job_prevents_spawning_an_uncontained_grandchild():
    code = """
import subprocess,sys
sys.stdin.buffer.read(1)
try:
    child = subprocess.Popen([sys.executable, '-I', '-c', 'pass'])
except OSError:
    print('blocked', flush=True)
else:
    child.wait(timeout=3)
    print('escaped', flush=True)
"""
    with subprocess.Popen(
        python_command(code),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        job = WindowsJob(process, 128)
        try:
            output, _ = process.communicate(b"x", timeout=5)
            assert process.returncode == 0
            assert output.strip() == b"blocked"
        finally:
            job.close()

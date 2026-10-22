"""Real offline smoke/benchmark using public synthetic text only, no database.

Explicitly run after public provisioning. Missing artifacts/dependencies FAIL;
there is no skip or fake substitute. Network connections are blocked for the run.
"""

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import time
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from prepare_local_embeddings import REVIEWED_REVISION, reviewed_profile

from agentsphere.chunking import ExtractedSection, chunk_document, source_fingerprint
from agentsphere.embeddings import EmbeddingLimits, LocalEmbedder, validate_vectors
from agentsphere.search_config import DEFAULT_MODEL_CACHE


def memory_mib() -> float:
    if sys.platform == "win32":
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                *[
                    (name, ctypes.c_size_t)
                    for name in (
                        "PeakWorkingSetSize",
                        "WorkingSetSize",
                        "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage",
                        "PagefileUsage",
                        "PeakPagefileUsage",
                        "PrivateUsage",
                    )
                ],
            ]

        counters = Memory()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Memory),
            wintypes.DWORD,
        ]
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise RuntimeError("Process memory measurement failed.")
        return counters.WorkingSetSize / 1024**2
    import resource

    scale = 1024**2 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale


def verify(snapshot: Path) -> dict:
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    checks = 0
    with patch("socket.socket.connect", side_effect=AssertionError("Network is disabled.")):
        before_memory = memory_mib()
        started = time.perf_counter()
        profile = reviewed_profile()
        embedder = LocalEmbedder(profile, snapshot, limits=EmbeddingLimits(cpu_threads=2))
        load_seconds = time.perf_counter() - started
        loaded_memory = memory_mib()
        document_id = UUID("18e0aef5-a202-442a-889c-79cafc00f909")
        samples = (
            "English source.\n\nSecond paragraph with a clear boundary. " * 180,
            "తెలుగు పత్రంలో సమాచారాన్ని యథాతథంగా ఉంచాలి. " * 180,
            "हिंदी दस्तावेज़ में स्रोत संदर्भ सुरक्षित रखें। " * 180,
            "Mixed English తెలుగు हिंदी e\u0301 😀\r\n\t\u00a0line. " * 180,
            "\tA\r\n\r\nB\u00a0e\u0301\u200b😀 " * 180,
        )
        for ordinal, text in enumerate(samples):
            section = ExtractedSection(
                document_id=document_id,
                section_id=uuid4(),
                ordinal=0,
                text=text,
                page_number=3 if ordinal == 0 else None,
                heading="Synthetic heading" if ordinal == 1 else None,
                line_start=10 if ordinal >= 2 else None,
                line_end=500 if ordinal >= 2 else None,
            )
            fingerprint = source_fingerprint(
                [section], file_sha256="a" * 64, parser_version="synthetic-v1"
            )
            chunks = chunk_document(
                document_id=document_id,
                extraction_epoch=uuid4(),
                file_sha256="a" * 64,
                parser_version="synthetic-v1",
                expected_source_fingerprint=fingerprint,
                sections=[section],
                tokenizer=embedder.tokenizer,
            ).chunks
            covered = bytearray(len(text))
            for chunk in chunks:
                start, end = chunk.source.char_start, chunk.source.char_end
                assert chunk.text == text[start:end]
                assert chunk.token_count == embedder.tokenizer.count(
                    chunk.text, add_special_tokens=False
                )
                assert chunk.input_token_count <= 512
                assert chunk.content_sha256 == hashlib.sha256(chunk.text.encode()).hexdigest()
                covered[start:end] = b"\x01" * (end - start)
            assert covered and all(covered), "Source content was lost."
            changed_id = section.model_copy(update={"section_id": uuid4()})
            again = chunk_document(
                document_id=document_id,
                extraction_epoch=uuid4(),
                file_sha256="a" * 64,
                parser_version="synthetic-v1",
                expected_source_fingerprint=fingerprint,
                sections=[changed_id],
                tokenizer=embedder.tokenizer,
            ).chunks
            assert [chunk.logical_id for chunk in chunks] == [chunk.logical_id for chunk in again]
            vectors = embedder.embed_passages([chunks[0].text])
            validate_vectors(vectors, expected_count=1)
            checks += 4
        # Short synthetic multilingual queries, deterministic model.eval(), correct prefixes.
        query = "How is document access protected?"
        first = embedder.embed_queries([query])
        repeated = embedder.embed_queries([query])
        assert first == repeated
        passage = embedder.embed_passages([query])
        assert passage != first
        assert len(first[0]) == 384 and math.isclose(math.hypot(*first[0]), 1, abs_tol=1e-4)
        checks += 3
        warm = []
        for _ in range(5):
            started = time.perf_counter()
            embedder.embed_queries([query])
            warm.append(time.perf_counter() - started)
        batch = ["Synthetic local passage about secure document processing. " * 12] * 8
        started = time.perf_counter()
        output = embedder.embed_passages(batch)
        batch_seconds = time.perf_counter() - started
        validate_vectors(output, expected_count=8)
        checks += 1
        result = {
            "checks_passed": checks,
            "failed": 0,
            "skipped": 0,
            "model": profile.model_name,
            "revision": REVIEWED_REVISION,
            "profile_fingerprint": profile.fingerprint,
            "dimensions": len(output[0]),
            "offline_network_blocked": True,
            "load_seconds_including_imports_and_integrity": load_seconds,
            "warm_query_seconds": warm,
            "warm_query_median_seconds": statistics.median(warm),
            "batch_size": 8,
            "batch_seconds": batch_seconds,
            "batch_items_per_second": 8 / batch_seconds,
            "batch_body_tokens_each": embedder.tokenizer.count(batch[0], add_special_tokens=False),
            "memory_before_mib": before_memory,
            "memory_loaded_mib": loaded_memory,
            "memory_after_inference_mib": memory_mib(),
            "memory_kind": "working_set" if sys.platform == "win32" else "peak_rss",
            "cpu_threads": 2,
            "logical_cpus": os.cpu_count(),
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "packages": {
                name: version(name)
                for name in (
                    "torch",
                    "transformers",
                    "tokenizers",
                    "safetensors",
                    "huggingface-hub",
                )
            },
        }
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_MODEL_CACHE / REVIEWED_REVISION)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.snapshot)
    except Exception:
        print("Real offline embedding verification FAILED; no fake substitution was used.")
        raise
    if args.output:
        # Reports contain public synthetic metrics; use a new ignored output path.
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Bounded, versioned CPU embeddings from a verified, explicitly provisioned snapshot.

Importing this module never imports PyTorch, opens a model, or contacts a service.
There is deliberately no hosted provider, implicit download, or fake fallback.
"""

import hashlib
import json
import math
import re
import stat
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from agentsphere.search_contracts import LOCAL_MODEL, EmbeddingProfileSpec

EMBEDDING_INTERFACE_VERSION = 1
SNAPSHOT_MANIFEST = "agentsphere_snapshot.json"
_ARTIFACT_NAMES = frozenset(
    {
        "config.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "special_tokens_map.json",
        "sentencepiece.bpe.model",
        "model.safetensors",
    }
)
_REQUIRED_ARTIFACTS = frozenset(
    {"config.json", "tokenizer_config.json", "tokenizer.json", "model.safetensors"}
)


class EmbeddingError(ValueError):
    """A safe diagnostic: never include document content or private paths."""


@dataclass(frozen=True)
class EmbeddingLimits:
    batch_size: int = 32
    max_passage_characters: int = 16_000
    max_query_characters: int = 2_000
    max_total_characters: int = 128_000
    max_query_tokens: int = 256
    cpu_threads: int = 2

    def __post_init__(self):
        bounds = {
            "batch_size": (1, 128),
            "max_passage_characters": (1, 16_000),
            "max_query_characters": (1, 2_000),
            "max_total_characters": (1, 2_048_000),
            "max_query_tokens": (1, 256),
            "cpu_threads": (1, 8),
        }
        if any(
            type(value := getattr(self, name)) is not int or not low <= value <= high
            for name, (low, high) in bounds.items()
        ):
            raise EmbeddingError("Embedding limits are invalid.")


class Embedder(Protocol):
    """Version 1 embeds raw bodies; implementations add the profile's prefix once."""

    profile: EmbeddingProfileSpec
    interface_version: int

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


def _regular_path(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.lstat()
        is_link = stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if is_link or not expected_kind(info.st_mode):
            raise EmbeddingError("Local model artifacts must be regular nonlinked objects.")
    except OSError:
        raise EmbeddingError("Required local model artifacts are unavailable.") from None


def _read_json(path: Path, *, maximum: int = 65_536):
    _regular_path(path)
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise EmbeddingError("Local model metadata exceeds its allowed size.")
        return json.loads(data)
    except (OSError, UnicodeError, ValueError):
        raise EmbeddingError("Local model metadata is invalid.") from None


def verify_snapshot(snapshot: Path, profile: EmbeddingProfileSpec) -> Path:
    """Check pinned provenance, bounded metadata, file hashes and a safe model format.

    The manifest is created only by the explicit public provisioning command. Hashes
    detect missing/changed artifacts; this is not protection from a local account
    which can replace both files and manifest. Restrict cache write permissions.
    """
    snapshot = Path(snapshot)
    if (
        not snapshot.is_absolute()
        or snapshot == Path(snapshot.anchor)
        or ".." in snapshot.parts
        or "\x00" in str(snapshot)
        or str(snapshot).startswith(("\\\\", "//"))
    ):
        raise EmbeddingError("A local absolute model snapshot directory is required.")
    for parent in reversed((snapshot, *snapshot.parents)):
        _regular_path(parent, directory=True)
    manifest = _read_json(snapshot / SNAPSHOT_MANIFEST)
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "model_name",
        "model_revision",
        "tokenizer_name",
        "tokenizer_revision",
        "files",
    }:
        raise EmbeddingError("The local model manifest is invalid.")
    expected = {
        "schema_version": 1,
        "model_name": profile.model_name,
        "model_revision": profile.model_revision,
        "tokenizer_name": profile.tokenizer_name,
        "tokenizer_revision": profile.tokenizer_revision,
    }
    if (
        type(manifest["schema_version"]) is not int
        or any(manifest.get(key) != value for key, value in expected.items())
        or profile.model_revision != profile.tokenizer_revision
    ):
        raise EmbeddingError("The local snapshot does not match the pinned embedding profile.")
    files = manifest["files"]
    if (
        not isinstance(files, dict)
        or not _REQUIRED_ARTIFACTS <= files.keys() <= _ARTIFACT_NAMES
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in files.values()
        )
    ):
        raise EmbeddingError("The local model artifact manifest is invalid.")
    try:
        # Reject extra .bin/.py/config files which could influence library loading.
        if {path.name for path in snapshot.iterdir()} != set(files) | {SNAPSHOT_MANIFEST}:
            raise EmbeddingError("The local snapshot contains unexpected artifacts.")
        total_size = 0
        for name, expected_hash in files.items():
            path = snapshot / name
            _regular_path(path)
            size = path.stat().st_size
            total_size += size
            if size <= 0 or size > 1_000_000_000 or total_size > 1_500_000_000:
                raise EmbeddingError("Local model artifacts exceed supported resource limits.")
            digest = hashlib.sha256()
            measured = 0
            with path.open("rb") as handle:
                while block := handle.read(1_048_576):
                    measured += len(block)
                    if measured > size:
                        raise EmbeddingError("Local model artifacts changed during verification.")
                    digest.update(block)
            if measured != size or digest.hexdigest() != expected_hash:
                raise EmbeddingError("Local model artifact integrity verification failed.")
    except OSError:
        raise EmbeddingError("Required local model artifacts are unavailable.") from None
    config = _read_json(snapshot / "config.json")
    tokenizer_config = _read_json(snapshot / "tokenizer_config.json")
    if (
        not isinstance(config, dict)
        or config.get("model_type") != "bert"
        or config.get("hidden_size") != profile.dimensions
        or type(config.get("max_position_embeddings")) is not int
        or config["max_position_embeddings"] < profile.model_max_tokens
        or config.get("auto_map")
        or not isinstance(tokenizer_config, dict)
        or tokenizer_config.get("auto_map")
    ):
        raise EmbeddingError("The local model architecture is incompatible with the profile.")
    return snapshot


class LocalTokenizer:
    """The exact fast tokenizer, returning offsets into the original Python string."""

    def __init__(
        self,
        profile: EmbeddingProfileSpec,
        snapshot: Path,
        *,
        max_characters: int = 2_000_000,
    ):
        if type(max_characters) is not int or not 1 <= max_characters <= 2_000_000:
            raise EmbeddingError("Tokenizer character limit is invalid.")
        snapshot = verify_snapshot(snapshot, profile)
        try:
            from transformers import AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(
                str(snapshot),
                local_files_only=True,
                trust_remote_code=False,
                use_fast=True,
            )
            if not self._tokenizer.is_fast:
                raise EmbeddingError("A compatible fast tokenizer is required.")
        except Exception:
            raise EmbeddingError("The pinned local tokenizer could not be loaded.") from None
        self.profile = profile
        self.max_characters = max_characters
        self._snapshot = snapshot
        self._lock = threading.RLock()

    def _check(self, text: str) -> None:
        if not isinstance(text, str) or len(text) > self.max_characters:
            raise EmbeddingError("Text exceeds the tokenizer character limit.")
        if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
            raise EmbeddingError("Text contains invalid Unicode code points.")

    def count(self, text: str, *, add_special_tokens: bool = True) -> int:
        self._check(text)
        if type(add_special_tokens) is not bool:
            raise EmbeddingError("Tokenizer options are invalid.")
        try:
            with self._lock:
                return len(
                    self._tokenizer(
                        text,
                        add_special_tokens=add_special_tokens,
                        truncation=False,
                        padding=False,
                        return_attention_mask=False,
                    )["input_ids"]
                )
        except Exception:
            raise EmbeddingError("Local tokenization failed.") from None

    def offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        self._check(text)
        try:
            with self._lock:
                offsets = self._tokenizer(
                    text,
                    add_special_tokens=False,
                    return_offsets_mapping=True,
                    truncation=False,
                    padding=False,
                    return_attention_mask=False,
                )["offset_mapping"]
            previous_start = -1
            for start, end in offsets:
                # Repeated/overlapping spans are legitimate for normalized Unicode.
                if not 0 <= start < end <= len(text) or start < previous_start:
                    raise EmbeddingError("Tokenizer returned invalid source offsets.")
                previous_start = start
            return tuple((int(start), int(end)) for start, end in offsets)
        except Exception:
            raise EmbeddingError("Local tokenization returned invalid source offsets.") from None


def validate_vectors(
    vectors: Sequence[Sequence[float]], *, expected_count: int, dimensions: int = 384
) -> tuple[tuple[float, ...], ...]:
    """Validate already normalized vectors; never silently repair invalid outputs."""
    if (
        type(expected_count) is not int
        or not 1 <= expected_count <= 128
        or type(dimensions) is not int
        or dimensions != 384
        or not isinstance(vectors, (tuple, list))
        or len(vectors) != expected_count
    ):
        raise EmbeddingError("Embedding output shape is invalid.")
    result = []
    for vector in vectors:
        if not isinstance(vector, (tuple, list)) or len(vector) != dimensions:
            raise EmbeddingError("Embedding output dimensions are invalid.")
        try:
            if any(type(value) not in {int, float} or not math.isfinite(value) for value in vector):
                raise EmbeddingError("Embedding output must contain finite numbers.")
            converted = tuple(float(value) for value in vector)
        except (OverflowError, ValueError):
            raise EmbeddingError("Embedding output must contain finite numbers.") from None
        norm = math.hypot(*converted)
        if norm == 0 or not math.isclose(norm, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise EmbeddingError("Embedding output must have nonzero unit norm.")
        result.append(converted)
    return tuple(result)


def _prepare_inputs(
    texts: Sequence[str],
    kind: Literal["query", "passage"],
    profile: EmbeddingProfileSpec,
    limits: EmbeddingLimits,
) -> tuple[str, ...]:
    if not isinstance(texts, (list, tuple)) or not 1 <= len(texts) <= limits.batch_size:
        raise EmbeddingError("Embedding batch must be a bounded nonempty list or tuple.")
    maximum = limits.max_query_characters if kind == "query" else limits.max_passage_characters
    total = 0
    for text in texts:
        if not isinstance(text, str) or not text.strip() or len(text) > maximum:
            raise EmbeddingError("Embedding text is empty or exceeds its character limit.")
        total += len(text)
        if any(0xD800 <= ord(char) <= 0xDFFF or char == "\x00" for char in text):
            raise EmbeddingError("Embedding text contains invalid Unicode or null characters.")
    if total > limits.max_total_characters:
        raise EmbeddingError("Embedding batch exceeds its total character limit.")
    prefix = profile.query_prefix if kind == "query" else profile.passage_prefix
    return tuple(prefix + text for text in texts)


class LocalEmbedder:
    """CPU mean pooling and L2 normalization as specified by the E5 model card.

    One instance serializes inference. Input count, token/character sizes and CPU
    threads are bounded. This in-process primitive does not promise a killable
    wall-clock timeout; a later indexing worker must supervise its own process.
    """

    interface_version = EMBEDDING_INTERFACE_VERSION
    backend_name = LOCAL_MODEL
    is_test_double = False

    def __init__(
        self,
        profile: EmbeddingProfileSpec,
        snapshot: Path,
        *,
        limits: EmbeddingLimits | None = None,
    ):
        self.profile = profile
        self.limits = limits or EmbeddingLimits()
        self.tokenizer = LocalTokenizer(profile, snapshot)
        self._lock = threading.Lock()
        try:
            import torch
            from transformers import AutoModel

            torch.set_num_threads(self.limits.cpu_threads)
            self._torch = torch
            self._model = AutoModel.from_pretrained(
                str(self.tokenizer._snapshot),
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
            ).to(device="cpu", dtype=torch.float32)
            self._model.eval()
            if self._model.config.hidden_size != profile.dimensions:
                raise EmbeddingError("Local model output dimensions are incompatible.")
        except Exception:
            raise EmbeddingError("The pinned local embedding model could not be loaded.") from None

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return self._embed(texts, "passage")

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return self._embed(texts, "query")

    def _embed(
        self, texts: Sequence[str], kind: Literal["query", "passage"]
    ) -> tuple[tuple[float, ...], ...]:
        with self._lock:
            return self._embed_locked(texts, kind)

    def _embed_locked(
        self, texts: Sequence[str], kind: Literal["query", "passage"]
    ) -> tuple[tuple[float, ...], ...]:
        prefixed = _prepare_inputs(texts, kind, self.profile, self.limits)
        for body, full in zip(texts, prefixed, strict=True):
            if self.tokenizer.count(full) > self.profile.model_max_tokens:
                raise EmbeddingError("Embedding input exceeds the full model token limit.")
            maximum = self.limits.max_query_tokens if kind == "query" else self.profile.chunk_tokens
            body_tokens = self.tokenizer.count(body, add_special_tokens=False)
            if not 1 <= body_tokens <= maximum:
                raise EmbeddingError("Embedding body exceeds the configured token limit.")
        try:
            with self.tokenizer._lock, self._torch.inference_mode():
                batch = self.tokenizer._tokenizer(
                    list(prefixed), return_tensors="pt", padding=True, truncation=False
                )
                if batch["input_ids"].shape[1] > self.profile.model_max_tokens:
                    raise EmbeddingError("Embedding input exceeds the full model token limit.")
                hidden = self._model(**batch).last_hidden_state
                if (
                    tuple(hidden.shape[:2]) != tuple(batch["input_ids"].shape)
                    or hidden.shape[-1] != self.profile.dimensions
                ):
                    raise EmbeddingError("Local model output shape is invalid.")
                mask = batch["attention_mask"].unsqueeze(-1).bool()
                summed = hidden.masked_fill(~mask, 0.0).sum(dim=1)
                counts = mask.sum(dim=1)
                if (counts == 0).any():
                    raise EmbeddingError("Local model returned an empty attention mask.")
                pooled = summed / counts
                normalized = self._torch.nn.functional.normalize(pooled, p=2, dim=1)
                vectors = normalized.cpu().tolist()
            return validate_vectors(vectors, expected_count=len(texts))
        except EmbeddingError:
            raise
        except Exception:
            raise EmbeddingError("Local embedding inference failed.") from None


class DeterministicFakeEmbedder:
    """Synthetic hash vectors for tests only; these have no semantic quality.

    This class is never selected by LocalEmbedder or runtime configuration. Explicit
    opt-in prevents a missing model from silently appearing to work in production.
    It does not emulate real tokenizer counts or prove retrieval performance.
    """

    interface_version = EMBEDDING_INTERFACE_VERSION
    backend_name = "deterministic_fake_for_tests_v1"
    is_test_double = True

    def __init__(
        self,
        profile: EmbeddingProfileSpec,
        *,
        for_tests: bool,
        limits: EmbeddingLimits | None = None,
    ):
        if for_tests is not True:
            raise EmbeddingError("Fake embeddings require explicit test-only opt-in.")
        self.profile = profile
        self.limits = limits or EmbeddingLimits()

    def embed_passages(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return self._embed(texts, "passage")

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return self._embed(texts, "query")

    def _embed(
        self, texts: Sequence[str], kind: Literal["query", "passage"]
    ) -> tuple[tuple[float, ...], ...]:
        prefixed = _prepare_inputs(texts, kind, self.profile, self.limits)
        vectors = []
        for text in prefixed:
            seed = (self.profile.fingerprint + "\x00" + text).encode("utf-8")
            values = [
                int.from_bytes(hashlib.sha256(seed + i.to_bytes(2, "big")).digest()[:4], "big")
                / 2**31
                - 1
                for i in range(self.profile.dimensions)
            ]
            norm = math.sqrt(math.fsum(value * value for value in values))
            vectors.append(tuple(value / norm for value in values))
        return validate_vectors(vectors, expected_count=len(texts))

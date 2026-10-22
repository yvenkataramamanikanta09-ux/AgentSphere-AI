"""Pure offline contracts and local-loader failure tests: no model downloads."""

import hashlib
import json
import math
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentsphere.embeddings import (
    DeterministicFakeEmbedder,
    EmbeddingError,
    EmbeddingLimits,
    LocalEmbedder,
    LocalTokenizer,
    validate_vectors,
    verify_snapshot,
)
from agentsphere.search_contracts import EmbeddingProfileSpec


@pytest.fixture
def profile():
    return EmbeddingProfileSpec(model_revision="a" * 40, tokenizer_revision="a" * 40)


def write_manifest(snapshot, profile):
    manifest = {
        "schema_version": 1,
        "model_name": profile.model_name,
        "model_revision": profile.model_revision,
        "tokenizer_name": profile.tokenizer_name,
        "tokenizer_revision": profile.tokenizer_revision,
        "files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in snapshot.iterdir()
            if path.name != "agentsphere_snapshot.json"
        },
    }
    (snapshot / "agentsphere_snapshot.json").write_text(json.dumps(manifest))


@pytest.fixture
def snapshot(tmp_path, profile):
    directory = tmp_path / "snapshot"
    directory.mkdir()
    (directory / "config.json").write_text(
        json.dumps({"model_type": "bert", "hidden_size": 384, "max_position_embeddings": 512})
    )
    (directory / "tokenizer_config.json").write_text("{}")
    (directory / "tokenizer.json").write_text("{}")
    (directory / "model.safetensors").write_bytes(b"synthetic fixture, not model weights")
    write_manifest(directory, profile)
    return directory


class StubTokenizer:
    is_fast = True

    def __init__(self):
        self.calls = []
        self.returned_offsets = None

    def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if isinstance(text, list):
            raise RuntimeError("synthetic inference failure with PRIVATE_PATH and PRIVATE_TEXT")
        result = {
            "input_ids": list(
                range(len(text) + (2 if kwargs.get("add_special_tokens", True) else 0))
            )
        }
        if kwargs.get("return_offsets_mapping"):
            result["offset_mapping"] = (
                self.returned_offsets
                if self.returned_offsets is not None
                else [(i, i + 1) for i in range(len(text))]
            )
        return result


@pytest.fixture
def loader(monkeypatch):
    tokenizer = StubTokenizer()
    calls = {}

    def load_tokenizer(path, **kwargs):
        calls["tokenizer"] = (path, kwargs)
        return tokenizer

    class StubModel:
        config = SimpleNamespace(hidden_size=384)

        def to(self, **kwargs):
            calls["device"] = kwargs
            return self

        def eval(self):
            calls["eval"] = True

    def load_model(path, **kwargs):
        calls["model"] = (path, kwargs)
        return StubModel()

    def threads(value):
        calls["threads"] = value

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=load_tokenizer),
            AutoModel=SimpleNamespace(from_pretrained=load_model),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(set_num_threads=threads, float32="float32", inference_mode=Context),
    )
    return tokenizer, calls


def test_valid_snapshot_is_verified_without_model_loading(snapshot, profile):
    assert verify_snapshot(snapshot, profile) == snapshot


@pytest.mark.parametrize("changed", ["model_revision", "tokenizer_revision", "model_name"])
def test_manifest_identity_must_match(snapshot, profile, changed):
    path = snapshot / "agentsphere_snapshot.json"
    manifest = json.loads(path.read_text())
    manifest[changed] = "b" * 40
    path.write_text(json.dumps(manifest))
    with pytest.raises(EmbeddingError, match="pinned"):
        verify_snapshot(snapshot, profile)


def test_model_and_tokenizer_revision_must_be_same_verified_bundle(snapshot, profile):
    changed = profile.model_copy(update={"tokenizer_revision": "b" * 40})
    write_manifest(snapshot, changed)
    with pytest.raises(EmbeddingError, match="pinned"):
        verify_snapshot(snapshot, changed)


@pytest.mark.parametrize("field,value", [("schema_version", True), ("files", []), ("files", {})])
def test_invalid_manifest_shape(snapshot, profile, field, value):
    path = snapshot / "agentsphere_snapshot.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(EmbeddingError):
        verify_snapshot(snapshot, profile)


@pytest.mark.parametrize("name", ["config.json", "tokenizer.json", "model.safetensors"])
def test_missing_or_changed_artifact_fails(snapshot, profile, name):
    path = snapshot / name
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(EmbeddingError, match="integrity"):
        verify_snapshot(snapshot, profile)
    path.unlink()
    with pytest.raises(EmbeddingError):
        verify_snapshot(snapshot, profile)


@pytest.mark.parametrize("name", ["model.bin", "model.py", ".env", "extra.json"])
def test_unexpected_artifacts_fail_closed(snapshot, profile, name):
    (snapshot / name).write_text("synthetic")
    with pytest.raises(EmbeddingError, match="unexpected"):
        verify_snapshot(snapshot, profile)


@pytest.mark.parametrize(
    "changes", [{"model_type": "custom"}, {"hidden_size": 768}, {"max_position_embeddings": 128}]
)
def test_incompatible_architecture(snapshot, profile, changes):
    path = snapshot / "config.json"
    config = json.loads(path.read_text())
    config.update(changes)
    path.write_text(json.dumps(config))
    write_manifest(snapshot, profile)
    with pytest.raises(EmbeddingError, match="architecture"):
        verify_snapshot(snapshot, profile)


@pytest.mark.parametrize("name", ["config.json", "tokenizer_config.json"])
def test_remote_custom_code_metadata_is_rejected(snapshot, profile, name):
    path = snapshot / name
    config = json.loads(path.read_text())
    config["auto_map"] = {"AutoModel": "custom.Model"}
    path.write_text(json.dumps(config))
    write_manifest(snapshot, profile)
    with pytest.raises(EmbeddingError, match="architecture"):
        verify_snapshot(snapshot, profile)


def test_missing_snapshot_and_relative_paths_fail_safely(tmp_path, profile):
    for path in [Path("relative"), tmp_path / "missing", Path(tmp_path.anchor)]:
        with pytest.raises(EmbeddingError) as error:
            verify_snapshot(path, profile)
        assert str(tmp_path) not in str(error.value)


def test_directory_in_place_of_artifact_is_rejected(snapshot, profile):
    path = snapshot / "model.safetensors"
    path.unlink()
    path.mkdir()
    with pytest.raises(EmbeddingError, match="nonlinked"):
        verify_snapshot(snapshot, profile)


def test_reparse_point_cannot_be_loaded(snapshot, profile, monkeypatch):
    original = Path.lstat

    def lstat(path):
        if path == snapshot / "tokenizer.json":
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(EmbeddingError, match="nonlinked"):
        verify_snapshot(snapshot, profile)


def test_artifact_os_failure_is_safe(snapshot, profile, monkeypatch):
    original = Path.open

    def open_path(path, *args, **kwargs):
        if path.name == "model.safetensors":
            raise OSError("PRIVATE_PATH simulated sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(EmbeddingError) as error:
        verify_snapshot(snapshot, profile)
    assert "PRIVATE" not in str(error.value)


def test_oversized_or_invalid_metadata(snapshot, profile):
    path = snapshot / "agentsphere_snapshot.json"
    for content in [b"not-json", b" " * 65_537]:
        path.write_bytes(content)
        with pytest.raises(EmbeddingError, match="metadata"):
            verify_snapshot(snapshot, profile)


def test_tokenizer_load_is_offline_fast_and_without_remote_code(snapshot, profile, loader):
    tokenizer, calls = loader
    adapter = LocalTokenizer(profile, snapshot)
    assert calls["tokenizer"][1] == {
        "local_files_only": True,
        "trust_remote_code": False,
        "use_fast": True,
    }
    assert adapter.count("తెలుగు हिन्दी e\u0301😀", add_special_tokens=False) == len(
        "తెలుగు हिन्दी e\u0301😀"
    )
    assert adapter.count("hello") == 7
    assert adapter.offsets("e\u0301😀") == ((0, 1), (1, 2), (2, 3))
    assert all(call[1]["truncation"] is False for call in tokenizer.calls)
    assert all(call[1]["padding"] is False for call in tokenizer.calls)


def test_tokenizer_preserves_repeated_unicode_spans(snapshot, profile, loader):
    tokenizer, _ = loader
    tokenizer.returned_offsets = [(0, 1), (0, 1), (1, 3), (2, 3)]
    adapter = LocalTokenizer(profile, snapshot)
    assert adapter.offsets("a\u0301b") == ((0, 1), (0, 1), (1, 3), (2, 3))


@pytest.mark.parametrize("offsets", [[(-1, 1)], [(0, 4)], [(0, 0)], [(1, 2), (0, 1)]])
def test_invalid_tokenizer_offsets_are_not_used(snapshot, profile, loader, offsets):
    tokenizer, _ = loader
    tokenizer.returned_offsets = offsets
    with pytest.raises(EmbeddingError, match="offsets"):
        LocalTokenizer(profile, snapshot).offsets("abc")


@pytest.mark.parametrize("maximum", [True, 0, -1, 2_000_001, 1.5])
def test_invalid_tokenizer_limits(snapshot, profile, maximum):
    with pytest.raises(EmbeddingError, match="limit"):
        LocalTokenizer(profile, snapshot, max_characters=maximum)


def test_tokenizer_rejects_oversized_and_invalid_unicode(snapshot, profile, loader):
    adapter = LocalTokenizer(profile, snapshot, max_characters=4)
    for text in ["12345", "\ud800", None]:
        with pytest.raises(EmbeddingError):
            adapter.count(text)
        with pytest.raises(EmbeddingError):
            adapter.offsets(text)


def test_slow_or_failed_loader_cannot_fallback(snapshot, profile, loader, monkeypatch):
    tokenizer, _ = loader
    tokenizer.is_fast = False
    with pytest.raises(EmbeddingError, match="could not be loaded"):
        LocalTokenizer(profile, snapshot)
    tokenizer.is_fast = True

    def fail(*args, **kwargs):
        raise OSError("PRIVATE_PATH private exception details")

    monkeypatch.setattr(sys.modules["transformers"].AutoTokenizer, "from_pretrained", fail)
    with pytest.raises(EmbeddingError) as error:
        LocalTokenizer(profile, snapshot)
    assert "PRIVATE" not in str(error.value)


def test_model_loader_uses_cpu_eval_safetensors_and_offline(snapshot, profile, loader):
    _, calls = loader
    adapter = LocalEmbedder(profile, snapshot)
    assert adapter.interface_version == 1 and not adapter.is_test_double
    assert calls["model"][1] == {
        "local_files_only": True,
        "trust_remote_code": False,
        "use_safetensors": True,
    }
    assert calls["device"] == {"device": "cpu", "dtype": "float32"}
    assert calls["eval"] is True
    assert calls["threads"] == 2


def test_model_loader_failure_is_safe(snapshot, profile, loader, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_PATH PRIVATE_TEXT")

    monkeypatch.setattr(sys.modules["transformers"].AutoModel, "from_pretrained", fail)
    with pytest.raises(EmbeddingError) as error:
        LocalEmbedder(profile, snapshot)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_size", 0),
        ("batch_size", 129),
        ("batch_size", True),
        ("max_passage_characters", 16001),
        ("max_query_characters", 2001),
        ("max_total_characters", 0),
        ("max_query_tokens", 257),
        ("cpu_threads", 9),
    ],
)
def test_invalid_embedding_limits(field, value):
    with pytest.raises(EmbeddingError, match="limits"):
        EmbeddingLimits(**{field: value})


@pytest.mark.parametrize(
    "value", [[], (), "query", None, [""], [" \n"], [None], ["\x00"], ["\ud800"]]
)
def test_invalid_batches(profile, value):
    adapter = DeterministicFakeEmbedder(profile, for_tests=True)
    for method in [adapter.embed_passages, adapter.embed_queries]:
        with pytest.raises(EmbeddingError):
            method(value)


def test_batch_character_limits(profile):
    adapter = DeterministicFakeEmbedder(
        profile, for_tests=True, limits=EmbeddingLimits(batch_size=2, max_total_characters=5)
    )
    for batch in [["a", "b", "c"], ["abc", "def"], ["x" * 16001]]:
        with pytest.raises(EmbeddingError):
            adapter.embed_passages(batch)
    with pytest.raises(EmbeddingError):
        adapter.embed_queries(["x" * 2001])


def test_real_embedder_checks_total_and_body_tokens_before_inference(snapshot, profile, loader):
    adapter = LocalEmbedder(profile, snapshot)
    with pytest.raises(EmbeddingError, match="full model token"):
        adapter.embed_passages(["x" * 510])
    with pytest.raises(EmbeddingError, match="body"):
        adapter.embed_passages(["x" * 351])
    with pytest.raises(EmbeddingError, match="body"):
        adapter.embed_queries(["x" * 257])


def test_inference_failure_is_safe_and_prefix_is_exact(snapshot, profile, loader):
    tokenizer, _ = loader
    adapter = LocalEmbedder(profile, snapshot)
    for method, expected in [
        (adapter.embed_queries, "query: PRIVATE_TEXT"),
        (adapter.embed_passages, "passage: PRIVATE_TEXT"),
    ]:
        with pytest.raises(EmbeddingError, match="inference failed") as error:
            method(["PRIVATE_TEXT"])
        assert "PRIVATE" not in str(error.value)
        assert tokenizer.calls[-1][0] == [expected]
        assert tokenizer.calls[-1][1]["truncation"] is False


@pytest.mark.parametrize("value", [False, 1, "yes", None])
def test_fake_requires_explicit_test_opt_in(profile, value):
    with pytest.raises(EmbeddingError, match="test-only"):
        DeterministicFakeEmbedder(profile, for_tests=value)


def test_fake_vectors_are_stable_prefix_sensitive_and_profile_versioned(profile):
    adapter = DeterministicFakeEmbedder(profile, for_tests=True)
    passages = adapter.embed_passages(["Hello", "తెలుగు हिन्दी"])
    assert passages == DeterministicFakeEmbedder(profile, for_tests=True).embed_passages(
        ["Hello", "తెలుగు हिन्दी"]
    )
    assert passages[0] != adapter.embed_queries(["Hello"])[0]
    changed = profile.model_copy(update={"chunker_version": "section_tokens_v2"})
    assert passages != DeterministicFakeEmbedder(changed, for_tests=True).embed_passages(
        ["Hello", "తెలుగు हिन्दी"]
    )
    assert adapter.is_test_double and "fake" in adapter.backend_name
    assert all(len(vector) == 384 for vector in passages)
    assert all(math.isclose(math.hypot(*vector), 1) for vector in passages)


def test_fake_is_stable_across_python_processes(profile):
    code = (
        "import hashlib,json; "
        "from agentsphere.embeddings import DeterministicFakeEmbedder; "
        "from agentsphere.search_contracts import EmbeddingProfileSpec; "
        "p=EmbeddingProfileSpec(model_revision='a'*40,tokenizer_revision='a'*40); "
        "v=DeterministicFakeEmbedder(p,for_tests=True).embed_passages(['synthetic']); "
        "print(hashlib.sha256(json.dumps(v).encode()).hexdigest())"
    )
    first = subprocess.check_output([sys.executable, "-c", code], text=True)
    second = subprocess.check_output([sys.executable, "-c", code], text=True)
    assert first == second


@pytest.mark.parametrize(
    "vectors,count",
    [([], 1), ([[0] * 383], 1), ([[0] * 385], 1), ([[0] * 384], 1), ([[1] * 384], 1)],
)
def test_invalid_vector_shape_and_norm(vectors, count):
    with pytest.raises(EmbeddingError):
        validate_vectors(vectors, expected_count=count)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "1", 10**1000])
def test_invalid_vector_values_fail_safely(value):
    vector = [0.0] * 384
    vector[0] = value
    with pytest.raises(EmbeddingError):
        validate_vectors([vector], expected_count=1)


def test_extreme_finite_vector_is_rejected_without_overflow():
    with pytest.raises(EmbeddingError, match="unit norm"):
        validate_vectors([[1e308] * 384], expected_count=1)


def test_valid_vectors_are_returned_without_implicit_normalization():
    vector = [0.0] * 384
    vector[5] = 1.0
    assert validate_vectors([vector], expected_count=1) == (tuple(vector),)
    with pytest.raises(EmbeddingError):
        validate_vectors([vector], expected_count=2)


def test_import_does_not_load_transformers_or_torch():
    code = (
        "import sys; import agentsphere.embeddings; "
        "assert 'torch' not in sys.modules; assert 'transformers' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_masked_mean_pooling_excludes_padding_and_normalizes(profile):
    # Real tensor arithmetic, synthetic model output: not a real-model quality test.
    import torch

    vectors = torch.zeros((2, 3, 384), dtype=torch.float32)
    vectors[:, 0, 0] = 1
    vectors[:, 1, 1] = 1
    vectors[0, 2, :] = 1000  # Ignored padding must not influence the first result.
    vectors[1, 2, 2] = 1
    calls = []

    def tokenize(texts, **kwargs):
        calls.append((texts, kwargs))
        return {
            "input_ids": torch.ones((2, 3), dtype=torch.int64),
            "attention_mask": torch.tensor([[1, 1, 0], [1, 1, 1]]),
        }

    adapter = object.__new__(LocalEmbedder)
    adapter.profile = profile
    adapter.limits = EmbeddingLimits()
    adapter._lock = threading.Lock()
    adapter._torch = torch
    adapter.tokenizer = SimpleNamespace(
        count=lambda text, **kwargs: 10,
        _tokenizer=tokenize,
        _lock=threading.RLock(),
    )
    adapter._model = lambda **kwargs: SimpleNamespace(last_hidden_state=vectors)
    result = adapter.embed_passages(["first", "second"])
    assert result[0][:3] == pytest.approx([1 / math.sqrt(2), 1 / math.sqrt(2), 0])
    assert result[1][:3] == pytest.approx([1 / math.sqrt(3)] * 3)
    assert all(value == 0 for value in result[0][3:])
    assert calls == [
        (
            ["passage: first", "passage: second"],
            {
                "return_tensors": "pt",
                "padding": True,
                "truncation": False,
            },
        )
    ]


@pytest.mark.parametrize("invalid", ["zero", "nan", "wrong_shape", "empty_mask"])
def test_invalid_model_tensor_outputs_are_rejected(profile, invalid):
    import torch

    vectors = torch.ones((1, 3, 384), dtype=torch.float32)
    mask = torch.ones((1, 3), dtype=torch.int64)
    if invalid == "zero":
        vectors.zero_()
    elif invalid == "nan":
        vectors[0, 0, 0] = float("nan")
    elif invalid == "wrong_shape":
        vectors = torch.ones((1, 3, 383))
    else:
        mask.zero_()
    adapter = object.__new__(LocalEmbedder)
    adapter.profile = profile
    adapter.limits = EmbeddingLimits()
    adapter._lock = threading.Lock()
    adapter._torch = torch
    adapter.tokenizer = SimpleNamespace(
        count=lambda text, **kwargs: 10,
        _tokenizer=lambda *args, **kwargs: {
            "input_ids": torch.ones((1, 3), dtype=torch.int64),
            "attention_mask": mask,
        },
        _lock=threading.RLock(),
    )
    adapter._model = lambda **kwargs: SimpleNamespace(last_hidden_state=vectors)
    with pytest.raises(EmbeddingError):
        adapter.embed_passages(["synthetic"])

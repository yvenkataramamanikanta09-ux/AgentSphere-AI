"""Synthetic public artifact acquisition tests: never contact a model provider."""

import hashlib
import importlib.util
import io
import json
import stat
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentsphere.embeddings import SNAPSHOT_MANIFEST, EmbeddingError

SPEC = importlib.util.spec_from_file_location(
    "embedding_provisioning",
    Path(__file__).resolve().parents[1] / "scripts/prepare_local_embeddings.py",
)
assert SPEC is not None and SPEC.loader is not None
provisioning = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provisioning)


class PublicResponses:
    def __init__(self, metadata, artifacts):
        self.metadata = metadata
        self.artifacts = artifacts
        self.requests = []
        self.interrupt = None

    def open(self, url, *, timeout):
        self.requests.append((url, timeout))
        assert url.startswith("https://huggingface.co/")
        if "/api/models/" in url:
            value = self.metadata
            return io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
        name = url.rsplit("/", 1)[1]
        if self.interrupt == name:
            raise OSError("synthetic provider diagnostic with PRIVATE_PATH")
        return io.BytesIO(self.artifacts[name])


@pytest.fixture
def public(monkeypatch, tmp_path):
    artifacts = {
        "config.json": json.dumps(
            {"model_type": "bert", "hidden_size": 384, "max_position_embeddings": 512}
        ).encode(),
        "tokenizer_config.json": b"{}",
        "tokenizer.json": b"{}",
        "special_tokens_map.json": b"{}",
        "sentencepiece.bpe.model": b"synthetic tokenizer artifact",
        "model.safetensors": b"synthetic public weights fixture, not actual model weights",
    }
    metadata = {
        "sha": provisioning.REVIEWED_REVISION,
        "id": "intfloat/multilingual-e5-small",
        "cardData": {"license": "mit"},
        "siblings": [
            {
                "rfilename": name,
                "size": len(data),
                "blobId": hashlib.sha1(
                    f"blob {len(data)}\0".encode() + data, usedforsecurity=False
                ).hexdigest(),
                **(
                    {"lfs": {"sha256": hashlib.sha256(data).hexdigest()}}
                    if name == "model.safetensors"
                    else {}
                ),
            }
            for name, data in artifacts.items()
        ],
    }
    opener = PublicResponses(metadata, artifacts)
    cache = tmp_path / "ignored" / "models"
    monkeypatch.setattr(provisioning, "DEFAULT_MODEL_CACHE", cache)
    monkeypatch.setattr(provisioning, "_opener", lambda: opener)
    return SimpleNamespace(cache=cache, metadata=metadata, artifacts=artifacts, opener=opener)


def temporary_objects(public):
    return list(public.cache.glob(".public-download-*")) if public.cache.exists() else []


def test_public_provisioning_verifies_pinned_files_and_publishes_manifest(public):
    snapshot = provisioning.provision(public.cache, download_public_model=True)
    assert snapshot == public.cache / provisioning.REVIEWED_REVISION
    assert {p.name for p in snapshot.iterdir()} == set(provisioning.ARTIFACTS) | {SNAPSHOT_MANIFEST}
    manifest = json.loads((snapshot / SNAPSHOT_MANIFEST).read_text())
    assert manifest["model_revision"] == provisioning.REVIEWED_REVISION
    assert manifest["tokenizer_revision"] == provisioning.REVIEWED_REVISION
    for name, data in public.artifacts.items():
        assert (snapshot / name).read_bytes() == data
        assert manifest["files"][name] == hashlib.sha256(data).hexdigest()
    assert len(public.opener.requests) == 1 + len(provisioning.ARTIFACTS)
    assert all(provisioning.REVIEWED_REVISION in url for url, _ in public.opener.requests)
    assert not temporary_objects(public)


@pytest.mark.parametrize("opt_in", [False, None, 1, "true"])
def test_without_explicit_opt_in_there_is_no_network_or_filesystem_io(public, opt_in):
    with pytest.raises(EmbeddingError, match="authorization"):
        provisioning.provision(public.cache, download_public_model=opt_in)
    assert not public.cache.exists()
    assert not public.opener.requests


def test_cache_is_restricted_to_exact_ignored_location(public):
    for path in (Path("relative"), public.cache.parent / "other", public.cache / ".." / "other"):
        with pytest.raises(EmbeddingError):
            provisioning.provision(path, download_public_model=True)
    assert not public.cache.exists()
    assert not public.opener.requests


def test_cache_refuses_regular_file_ancestor(public):
    public.cache.parent.write_bytes(b"not a directory")
    with pytest.raises(EmbeddingError, match="unsafe"):
        provisioning.provision(public.cache, download_public_model=True)
    assert not public.opener.requests


def test_cache_refuses_reparse_point_without_following_it(public, monkeypatch):
    public.cache.mkdir(parents=True)
    original = Path.lstat

    def lstat(path):
        if path == public.cache:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(EmbeddingError, match="unsafe"):
        provisioning.provision(public.cache, download_public_model=True)
    assert not public.opener.requests


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/model",
        "https://example.org/model",
        "https://huggingface.co.evil.test/model",
        "https://hf.co.evil.test/model",
        "file:///model",
        "https://user:password@huggingface.co/model",
        "https://huggingface.co:8443/model",
        "https://huggingface.co/model#fragment",
    ],
)
def test_public_redirect_rejects_nonpublic_or_credentialed_targets(url):
    request = urllib.request.Request("https://huggingface.co/original")
    with pytest.raises(EmbeddingError, match="redirect"):
        provisioning._PublicRedirects().redirect_request(request, None, 302, "Found", {}, url)


@pytest.mark.parametrize(
    "url",
    [
        "https://huggingface.co/file",
        "https://cdn-lfs.huggingface.co/file",
        "https://cdn-lfs.hf.co/file",
        "https://cas-bridge.xethub.hf.co/file?signature=synthetic",
    ],
)
def test_public_redirect_allows_known_https_artifact_hosts(url):
    request = urllib.request.Request("https://huggingface.co/original")
    redirected = provisioning._PublicRedirects().redirect_request(
        request, None, 302, "Found", {}, url
    )
    assert redirected.full_url == url


def test_existing_verified_snapshot_is_reused_without_network_or_overwrite(public, monkeypatch):
    snapshot = provisioning.provision(public.cache, download_public_model=True)
    before = {p.name: p.read_bytes() for p in snapshot.iterdir()}
    monkeypatch.setattr(provisioning, "_opener", lambda: pytest.fail("Unexpected network opener"))
    assert provisioning.provision(public.cache, download_public_model=True) == snapshot
    assert {p.name: p.read_bytes() for p in snapshot.iterdir()} == before


@pytest.mark.parametrize("mutation", ["content", "manifest_revision", "extra_file"])
def test_existing_invalid_snapshot_is_not_repaired_overwritten_or_redownloaded(
    public, monkeypatch, mutation
):
    snapshot = provisioning.provision(public.cache, download_public_model=True)
    if mutation == "content":
        (snapshot / "tokenizer.json").write_bytes(b"corrupt")
    elif mutation == "manifest_revision":
        path = snapshot / SNAPSHOT_MANIFEST
        manifest = json.loads(path.read_text())
        manifest["tokenizer_revision"] = "a" * 40
        path.write_text(json.dumps(manifest))
    else:
        (snapshot / "unreviewed.py").write_text("do not load")
    before = {p.name: p.read_bytes() for p in snapshot.iterdir()}
    monkeypatch.setattr(provisioning, "_opener", lambda: pytest.fail("Unexpected network opener"))
    with pytest.raises(EmbeddingError):
        provisioning.provision(public.cache, download_public_model=True)
    assert {p.name: p.read_bytes() for p in snapshot.iterdir()} == before


@pytest.mark.parametrize("field,value", [("sha", "b" * 40), ("id", "other/model")])
def test_metadata_identity_must_match_reviewed_snapshot(public, field, value):
    public.metadata[field] = value
    with pytest.raises(EmbeddingError, match="reviewed"):
        provisioning.provision(public.cache, download_public_model=True)
    assert len(public.opener.requests) == 1
    assert not temporary_objects(public)


@pytest.mark.parametrize("license_name", [None, "apache-2.0", "unknown"])
def test_metadata_license_must_match_reviewed_mit_license(public, license_name):
    public.metadata["cardData"]["license"] = license_name
    with pytest.raises(EmbeddingError, match="reviewed"):
        provisioning.provision(public.cache, download_public_model=True)
    assert len(public.opener.requests) == 1


def test_missing_allowlisted_artifact_is_rejected_before_download(public):
    public.metadata["siblings"].pop()
    with pytest.raises(EmbeddingError, match="incomplete"):
        provisioning.provision(public.cache, download_public_model=True)
    assert len(public.opener.requests) == 1


def test_duplicate_metadata_filenames_are_rejected_before_download(public):
    public.metadata["siblings"].append(dict(public.metadata["siblings"][0]))
    with pytest.raises(EmbeddingError, match="duplicate"):
        provisioning.provision(public.cache, download_public_model=True)
    assert len(public.opener.requests) == 1


@pytest.mark.parametrize("lfs", [None, [], "invalid", {}, {"sha256": "invalid"}])
def test_malformed_lfs_integrity_metadata_is_rejected_before_download(public, lfs):
    public.metadata["siblings"][-1]["lfs"] = lfs
    with pytest.raises(EmbeddingError):
        provisioning.provision(public.cache, download_public_model=True)
    assert len(public.opener.requests) == 1
    assert not temporary_objects(public)


@pytest.mark.parametrize("size", [None, "20", True, 0, -1, 600_000_001])
def test_bad_artifact_sizes_are_rejected_before_artifact_network_io(public, size):
    public.metadata["siblings"][0]["size"] = size
    with pytest.raises(EmbeddingError):
        provisioning.provision(public.cache, download_public_model=True)
    assert len(public.opener.requests) == 1
    assert not temporary_objects(public)


@pytest.mark.parametrize(
    "mutation",
    ["json", "oversized_metadata", "top_level", "card_data", "siblings", "entry"],
)
def test_malformed_provider_metadata_fails_safely_without_artifact_requests(public, mutation):
    values = {
        "json": b"not-json",
        "oversized_metadata": b" " * 2_000_001,
        "top_level": [],
        "card_data": {**public.metadata, "cardData": None},
        "siblings": {**public.metadata, "siblings": None},
        "entry": {**public.metadata, "siblings": [None]},
    }
    public.opener.metadata = values[mutation]
    with pytest.raises((EmbeddingError, ValueError, TypeError)):
        provisioning.provision(public.cache, download_public_model=True)
    assert len(public.opener.requests) == 1
    assert not temporary_objects(public)


@pytest.mark.parametrize("integrity_failure", ["lfs", "git_blob", "short", "oversized"])
def test_corrupt_or_incorrect_downloads_are_not_published_and_owned_temporary_is_removed(
    public, integrity_failure
):
    if integrity_failure == "lfs":
        public.metadata["siblings"][-1]["lfs"]["sha256"] = "0" * 64
    elif integrity_failure == "git_blob":
        public.metadata["siblings"][0]["blobId"] = "0" * 40
    elif integrity_failure == "short":
        public.artifacts["config.json"] = public.artifacts["config.json"][:-1]
    else:
        public.artifacts["config.json"] += b"extra"
    with pytest.raises(EmbeddingError):
        provisioning.provision(public.cache, download_public_model=True)
    assert not (public.cache / provisioning.REVIEWED_REVISION).exists()
    assert not temporary_objects(public)


def test_interrupted_transfer_preserves_unrelated_cache_objects_and_removes_only_owned_temp(public):
    public.cache.mkdir(parents=True)
    unrelated = public.cache / "unrelated-snapshot"
    unrelated.mkdir()
    sentinel = unrelated / "preserve.txt"
    sentinel.write_bytes(b"preserve this object")
    public.opener.interrupt = "model.safetensors"
    with pytest.raises(OSError):
        provisioning.provision(public.cache, download_public_model=True)
    assert sentinel.read_bytes() == b"preserve this object"
    assert not temporary_objects(public)
    assert not (public.cache / provisioning.REVIEWED_REVISION).exists()


def test_download_time_limit_is_enforced_before_publication(public, monkeypatch):
    calls = iter((0, provisioning.MAX_DOWNLOAD_SECONDS + 1))
    monkeypatch.setattr(provisioning.time, "monotonic", lambda: next(calls))
    with pytest.raises(EmbeddingError, match="limits"):
        provisioning.provision(public.cache, download_public_model=True)
    assert not temporary_objects(public)


def test_concurrent_publication_never_overwrites_other_snapshot(public, monkeypatch):
    verify = provisioning.verify_snapshot
    concurrent = public.cache / provisioning.REVIEWED_REVISION

    def publish_other_after_verification(path, profile):
        result = verify(path, profile)
        concurrent.mkdir()
        (concurrent / "preserve.txt").write_bytes(b"other publisher")
        return result

    monkeypatch.setattr(provisioning, "verify_snapshot", publish_other_after_verification)
    with pytest.raises(EmbeddingError, match="concurrent"):
        provisioning.provision(public.cache, download_public_model=True)
    assert (concurrent / "preserve.txt").read_bytes() == b"other publisher"
    assert not temporary_objects(public)


def test_metadata_unreviewed_extra_files_are_never_requested_or_written(public):
    public.metadata["siblings"].append(
        {"rfilename": "../../unreviewed.py", "size": 1, "blobId": "a" * 40}
    )
    snapshot = provisioning.provision(public.cache, download_public_model=True)
    assert set(path.name for path in snapshot.iterdir()) == set(provisioning.ARTIFACTS) | {
        SNAPSHOT_MANIFEST
    }
    assert not any("unreviewed" in url for url, _ in public.opener.requests)

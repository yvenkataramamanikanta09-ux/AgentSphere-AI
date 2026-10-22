"""Explicit public-artifact provisioning, separate from offline runtime loading.

Only the reviewed public repository/revision and safe artifact allowlist can be
downloaded. No settings, document data, credentials or queries are read or sent.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from agentsphere.embeddings import SNAPSHOT_MANIFEST, EmbeddingError, verify_snapshot
from agentsphere.search_config import DEFAULT_MODEL_CACHE
from agentsphere.search_contracts import LOCAL_MODEL, EmbeddingProfileSpec

REVIEWED_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
ARTIFACTS = (
    "config.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "model.safetensors",
)
MAX_SNAPSHOT_BYTES = 600_000_000
MAX_DOWNLOAD_SECONDS = 1800


def reviewed_profile() -> EmbeddingProfileSpec:
    return EmbeddingProfileSpec(
        model_revision=REVIEWED_REVISION, tokenizer_revision=REVIEWED_REVISION
    )


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        try:
            origin = urlsplit(newurl)
            host = origin.hostname or ""
            safe_origin = (
                origin.scheme == "https"
                and origin.username is None
                and origin.password is None
                and origin.port in (None, 443)
                and not origin.fragment
            )
        except ValueError:
            raise EmbeddingError("Public artifact redirect was refused.") from None
        if not safe_origin or not (
            host == "huggingface.co"
            or host.endswith(".huggingface.co")
            or host.endswith(".hf.co")
            or host.endswith(".xethub.hf.co")
        ):
            raise EmbeddingError("Public artifact redirect was refused.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _opener():
    # No cookie jar, hub client, implicit HF token, auth handler or dotenv import.
    return urllib.request.build_opener(_PublicRedirects())


def _safe_cache(cache: Path) -> Path:
    if not cache.is_absolute() or ".." in cache.parts or str(cache).startswith(("//", "\\\\")):
        raise EmbeddingError("An absolute nonlinked local cache path is required.")
    # This command intentionally provisions only under the repository's ignored
    # model cache. Deployment-specific artifact distribution is a separate task.
    if cache != DEFAULT_MODEL_CACHE:
        raise EmbeddingError("Public provisioning is restricted to the ignored model cache.")
    for path in reversed((cache, *cache.parents)):
        try:
            info = path.lstat()
        except FileNotFoundError:
            path.mkdir()
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise EmbeddingError("The model cache contains an unsafe filesystem object.")
    return cache


def provision(cache: Path, *, download_public_model: bool = False) -> Path:
    if download_public_model is not True:
        raise EmbeddingError("Explicit public-model download authorization is required.")
    cache = _safe_cache(cache)
    destination = cache / REVIEWED_REVISION
    if destination.exists() or destination.is_symlink():
        return verify_snapshot(destination, reviewed_profile())
    opener = _opener()
    metadata_url = (
        f"https://huggingface.co/api/models/{LOCAL_MODEL}/revision/{REVIEWED_REVISION}?blobs=true"
    )
    with opener.open(metadata_url, timeout=60) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise EmbeddingError("Public model metadata exceeds supported limits.")
    metadata = json.loads(raw)
    if (
        not isinstance(metadata, dict)
        or metadata.get("sha") != REVIEWED_REVISION
        or metadata.get("id") != LOCAL_MODEL
        or not isinstance(metadata.get("cardData"), dict)
        or metadata.get("cardData", {}).get("license") != "mit"
    ):
        raise EmbeddingError("Public model metadata does not match the reviewed snapshot.")
    siblings = metadata.get("siblings")
    if not isinstance(siblings, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("rfilename"), str)
        for entry in siblings
    ):
        raise EmbeddingError("Public artifact metadata is invalid.")
    entries = {entry["rfilename"]: entry for entry in siblings}
    if len(entries) != len(siblings):
        raise EmbeddingError("Public artifact metadata contains duplicate filenames.")
    if not set(ARTIFACTS) <= entries.keys():
        raise EmbeddingError("The reviewed public snapshot is incomplete.")
    if any(
        type(entries[name].get("size")) is not int or entries[name]["size"] <= 0
        for name in ARTIFACTS
    ):
        raise EmbeddingError("Public artifact sizes are unavailable.")
    if not 0 < sum(entries[name]["size"] for name in ARTIFACTS) <= MAX_SNAPSHOT_BYTES:
        raise EmbeddingError("The public snapshot exceeds supported size limits.")
    for name in ARTIFACTS:
        entry = entries[name]
        if "lfs" in entry and (
            not isinstance(entry["lfs"], dict)
            or not isinstance(entry["lfs"].get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["lfs"]["sha256"]) is None
        ):
            raise EmbeddingError("Public artifact integrity metadata is invalid.")
        if "lfs" not in entry and (
            not isinstance(entry.get("blobId"), str)
            or re.fullmatch(r"[0-9a-f]{40}", entry["blobId"]) is None
        ):
            raise EmbeddingError("Public artifact integrity metadata is invalid.")
    started = time.monotonic()
    temporary = Path(tempfile.mkdtemp(prefix=".public-download-", dir=cache))
    try:
        hashes = {}
        for name in ARTIFACTS:
            entry = entries[name]
            expected_size = entry["size"]
            sha256 = hashlib.sha256()
            git_blob = hashlib.sha1(f"blob {expected_size}\0".encode(), usedforsecurity=False)
            received = 0
            url = f"https://huggingface.co/{LOCAL_MODEL}/resolve/{REVIEWED_REVISION}/{name}"
            with opener.open(url, timeout=180) as response, (temporary / name).open("xb") as handle:
                while block := response.read(1_048_576):
                    received += len(block)
                    if (
                        received > expected_size
                        or time.monotonic() - started > MAX_DOWNLOAD_SECONDS
                    ):
                        raise EmbeddingError("The public artifact download exceeded its limits.")
                    sha256.update(block)
                    git_blob.update(block)
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            expected_lfs = entry.get("lfs", {}).get("sha256")
            if received != expected_size or (
                sha256.hexdigest() != expected_lfs
                if expected_lfs
                else git_blob.hexdigest() != entry.get("blobId")
            ):
                raise EmbeddingError("Public artifact size or integrity verification failed.")
            hashes[name] = sha256.hexdigest()
            print(f"Verified public artifact: {name} ({received} bytes)", flush=True)
        manifest = {
            "schema_version": 1,
            "model_name": LOCAL_MODEL,
            "model_revision": REVIEWED_REVISION,
            "tokenizer_name": LOCAL_MODEL,
            "tokenizer_revision": REVIEWED_REVISION,
            "files": hashes,
        }
        (temporary / SNAPSHOT_MANIFEST).write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        verify_snapshot(temporary, reviewed_profile())
        # Never overwrite an existing snapshot, including a concurrent provision.
        if destination.exists():
            raise EmbeddingError(
                "A snapshot was published concurrently; verify it before retrying."
            )
        temporary.rename(destination)
        return destination
    finally:
        # Only this invocation's randomly created directory is eligible here.
        if temporary.exists():
            if (
                temporary.parent != cache
                or not temporary.name.startswith(".public-download-")
                or not temporary.resolve().is_relative_to(cache.resolve())
                or temporary.is_symlink()
                or getattr(temporary.lstat(), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise EmbeddingError("Temporary snapshot cleanup refused an unsafe target.")
            shutil.rmtree(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-public-model", action="store_true")
    args = parser.parse_args()
    try:
        snapshot = provision(DEFAULT_MODEL_CACHE, download_public_model=args.download_public_model)
    except (EmbeddingError, OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        print("Public snapshot provisioning failed; no private data or diagnostics were sent.")
        return 1
    print(f"Verified offline snapshot ready: {snapshot.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Module 6 Stage 2 — source-preserving chunks and local embeddings

Stage 2 provides standalone chunking and local embedding building blocks over
the exact text already stored in Module 5 `document_sections`. It does not start
a search service, populate the Module 6 tables, create vector jobs, connect to
Qdrant, or change existing document APIs or extraction behavior. Search and
indexing feature flags remain disabled.

Implementation starts from `8cb43813f0f85acfe27ccf4d0285d414d859cf59` on
`develop`. The normal development database remains at **0002**. Revision **0004**
is exercised only in new disposable verification environments.

## Concepts and boundaries

A token is a model-specific unit of text, not a character, word, or byte. The
same number of Unicode characters can require very different token counts in
English, Telugu, Hindi, and mixed-language text. Chunking therefore uses the
exact pinned tokenizer, while source offsets continue to count Python Unicode
code points into the unchanged stored section text.

An embedding is a fixed-length numeric representation of text. E5 uses a
`passage: ` prefix for document text and a `query: ` prefix for search text. The
prefix is part of the embedding input and its hash; it is not inserted into the
original chunk text. L2 normalization makes each nonzero output vector have
length approximately one. Embeddings alone do not establish retrieval quality,
authorization, factual correctness, or a calibrated relevance probability.

PostgreSQL remains the future authority for document visibility and permissions.
These pure local building blocks do not authorize callers. A later indexing or
search implementation must load authorized, current snapshots, recheck them at
publication, and use the Stage 1 generation/lease guards. No background indexing
or persistence integration is added here.

## Chunking and provenance

The initial profile targets **350 body tokens** with **50 tokens of overlap**.
Every actual prefixed model input, including special tokens, must remain within
the model's **512-token** limit. Exact retokenization determines the final size;
character counts and estimated prefix reserves are not treated as proof.

Chunks remain inside one extracted section. Paragraph, sentence, and line
boundaries are preferred where compatible with token and resource limits. Each
chunk carries a half-open `[char_start, char_end)` span into the original text,
and its text must equal that exact Unicode substring. The algorithm does not
decode token IDs to reconstruct or normalize source text. Empty sections produce
no invented content. Oversized or otherwise unrepresentable input fails explicitly
instead of being silently truncated.

Source references are copied from the section: actual PDF page numbers, available
DOCX headings, and TXT/Markdown section-level line ranges. Unknown references
remain absent. No DOCX page number, narrower raw-file line range, or additional
heading is inferred. A chunk's narrower character span does not change the
precision of the stored source line reference.

The existing `stable_chunk_id` contract names chunks using document identity,
source/profile fingerprints, section ordinal, character span, and content hash.
Regenerated section UUIDs are excluded from the logical identity but remain in
the current source reference. Exact UTF-8 chunk text and exact prefixed embedding
input receive separate SHA-256 hashes. Source fingerprint construction is
versioned and deterministic; changed content or source locations invalidate the
old snapshot. An extraction epoch remains a separate freshness/fencing identity.

`source_fingerprint` serializes a canonical JSON manifest with sorted keys,
compact separators, and unchanged Unicode. Version 1 includes the original file
SHA-256, parser version, and each ordered section's ordinal, exact-text SHA-256,
page, heading, line bounds, and `section` precision. It excludes section UUIDs,
document UUIDs, and extraction epochs. Document identity is separately included
in the logical chunk ID. The caller supplies the expected stored fingerprint;
any mismatch fails before chunks are returned. The caller must provide a
consistent snapshot, with unique section UUIDs and contiguous ordinals beginning
at zero. The Stage 1 SQL schema accepts this fingerprint but does not compute it.

The splitter uses the pinned fast tokenizer's offset mapping to choose candidate
ends. A real paragraph, line, or sentence boundary in the final quarter of a
candidate span is preferred. It then retokenizes the exact substring and its
prefixed input, backing off when necessary. The next chunk targets 50 tokens of
overlap according to whole-section offsets; real boundary adjustments, repeated
Unicode spans, and substring retokenization can change the exact overlap. Forward
progress is required. Tokenizer-normalized gaps remain in the unchanged source
substring rather than being reconstructed from decoded IDs. Empty or whitespace-only
sections produce no chunks; nonempty input with no usable encoding fails safely.

Default hard input/work counters are 10,000 sections, 2,000,000 total Unicode
characters, 2,000,000 token offsets per section, 10,000 output chunks, 16,000
characters per chunk, and 100,000 tokenizer calls. Chunking has a cooperative
60-second deadline checked between bounded calls, configurable up to 120 seconds.
Exceeding a bound returns an error and no partial `ChunkSet`. This is not an OS
memory quota or interruption of a single native tokenizer operation.

## Model, dependencies, and artifact acquisition

The primary local model is `intfloat/multilingual-e5-small`, with **384-dimensional**
outputs and a 512-token maximum input. Model and tokenizer identities must use
explicit reviewed **40-character commit revisions**. Remote custom model code is
not permitted. Runtime uses an existing local snapshot with offline-only loading;
missing or incompatible artifacts cause a safe failure rather than downloading,
selecting another revision, or falling back to fake embeddings.

Both model and tokenizer use reviewed commit
`614241f622f53c4eeff9890bdc4f31cfecc418b3`. The repository model card declares
the MIT license. Its E5 pooling/prefix instructions and architecture are the
implementation reference; language support is not a claim that retrieval quality
has been measured for any language in this application. See the pinned
[model card](https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md),
[model configuration](https://huggingface.co/intfloat/multilingual-e5-small/blob/614241f622f53c4eeff9890bdc4f31cfecc418b3/config.json),
and [public snapshot tree](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3).

The optional Poetry `embeddings` group pins `transformers==5.20.2` and
`torch==2.14.1+cpu`; the CPU wheel source is explicit and limited to PyTorch.
Poetry locks the transitive tokenizer, safetensors, model-hub, numerical, and
supporting packages. The existing API dependencies are not upgraded for this
feature. A default backend installation does not need this optional group;
real-model verification does. Windows/Python and exact installed versions are
recorded with the final verification and benchmark results below.

The acquisition allowlist contains only `config.json`, `tokenizer_config.json`,
`tokenizer.json`, `special_tokens_map.json`, `sentencepiece.bpe.model`, and
`model.safetensors`. The public metadata must match the repository identity,
reviewed revision, and MIT license. Every artifact has a positive declared size,
the combined size is capped at 600,000,000 bytes, and downloaded bytes must match
the declared size plus the public Git-blob or LFS SHA-256 digest. A per-command
1,800-second download budget and socket timeouts apply. HTTPS redirects are
restricted to approved public artifact hosts; credentials, unexpected ports,
and fragments are rejected. Signed artifact query strings are permitted.

Only a new owned temporary directory is written. After integrity checks, a
manifest binds exact model/tokenizer identities, revisions, and artifact hashes.
A verified existing snapshot is reused; an invalid snapshot is neither repaired
nor overwritten. Failed downloads remove only their owned temporary directory,
subject to ancestry/link/reparse checks. Runtime checks exact manifest keys,
regular nonlinked artifacts, hashes, expected architecture and dimensions, and
absence of unreviewed additional files. These checks do not protect against a
local administrator replacing both trusted artifacts and their manifest.

The separately invoked acquisition procedure may download only the reviewed public
model/tokenizer artifacts into an ignored local cache. It must never receive
document text, search queries, application credentials, or database settings.
The normal application and embedding runtime do not perform model-provider
requests. The cache is not source code and must not be added to Git or exposed
through an HTTP static-file route.

From `backend`, the commands are:

```powershell
poetry install --with embeddings,lint
# Explicit one-time network action: public artifacts only, reviewed revision only.
.\.venv\Scripts\python.exe scripts/prepare_local_embeddings.py --download-public-model
# Subsequent verification is offline and fails if artifacts are missing.
.\.venv\Scripts\python.exe scripts/verify_local_embeddings.py
```

Provisioning is deliberately restricted to the repository's ignored
`backend/.local/models/<revision>` directory. The runtime constructor can receive
an explicitly prepared absolute snapshot path, but cannot acquire it implicitly.
No command enables `SEARCH_ENABLED` or `SEARCH_INDEXING_ENABLED`, and neither
flag is needed for this standalone verification.

The first public artifact acquisition attempt timed out. Its incomplete temporary
snapshot was not published; a subsequent explicit attempt completed and verified
the pinned public artifact sizes and hashes. No range-download infrastructure,
hosted embedding service, application credentials, or private input was introduced
to work around that transient failure. Successful acquisition alone is not a
claim that real inference passed; the separate offline result is recorded below.

## Embedding interface and validation

The `Embedder` version 1 interface provides `embed_passages` and `embed_queries`,
distinguishes passage and query inputs, applies the corresponding prefix once at
the boundary, and validates batch and text limits before inference. Callers pass
raw text: an actual `passage:` sequence occurring in source content is not
silently removed. It rejects empty or oversized inputs, model/tokenizer
revision mismatches, invalid shapes, non-finite values, zero-length vectors, and
incorrect normalization. Default limits are 32 inputs per batch, 16,000 Unicode
characters per passage, 2,000 per query, 128,000 across a batch, 350 passage body
tokens, and 256 query body tokens. Full prefixed inputs independently remain at
most 512 tokens including special tokens. Tokenization never requests truncation.
CPU inference uses float32 evaluation/inference mode, attention-mask mean pooling,
L2 normalization, and two CPU threads by default. One embedder instance serializes
inference and uses a tokenizer lock. It does not train the model.

`SearchSettings.search_chunking_limits` and `.search_embedding_limits` construct
validated pure limit values without creating paths or loading a model. The new
`SEARCH_CHUNK_MAX_SECTIONS`, `SEARCH_CHUNK_MAX_CHARACTERS`,
`SEARCH_CHUNK_MAX_TOKENIZER_CALLS`, `SEARCH_CHUNK_TIMEOUT_SECONDS`,
`SEARCH_EMBEDDING_CPU_THREADS`, and `SEARCH_EMBEDDING_MAX_BATCH_CHARACTERS` fields
are documented in `.env.example`. Existing batch/chunk/query settings feed the
same local limits. These remain independent from the disabled search services.

`torch.set_num_threads` affects the hosting process. The standalone inference
primitive has no killable wall-clock or hard RSS limit; the existing future-worker
embedding timeout setting is not implemented as a process supervisor here. A
later indexing worker must own that containment boundary. Resource bounds prevent
unbounded accepted input, but they are not a promise of a particular memory or
latency ceiling on every CPU.

A deterministic fake embedder exists solely for automated contract tests. Its
stable synthetic vectors test shape, hashing, normalization, and repeatability;
they are never a runtime fallback or evidence of real semantic retrieval quality.
Real-model smoke results and fake-backed unit results are reported separately.

## Verification commands and safety

From the backend, use the existing Poetry environment. The disposable driver
creates its own random database, upgrades it to the code head, checks Alembic
model/schema consistency, runs tests against owned schemas, and removes only the
database it created. It supplies a separate temporary private storage root.

```powershell
Set-Location C:\Users\yvenk\Desktop\agentsphere-ai\AgentSphere-AI\backend
.\.venv\Scripts\python.exe scripts/verify_stage2.py -q
.\.venv\Scripts\python.exe scripts/verify_processing.py
.\.venv\Scripts\python.exe -m ruff check src tests scripts migrations
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts migrations
poetry check --lock
.\.venv\Scripts\python.exe -m alembic heads

Set-Location ..\frontend
npm test
npm run typecheck
npm run lint
npm run build
```

`alembic heads` reads migration code only. Do not run bare `alembic upgrade`,
`check`, or `current` against the default development connection as a verification
shortcut. Do not initialize, reconcile, or clean development private storage.
The existing read-only seven-table count/digest baseline and filesystem `lstat`
comparison are used to detect unintended development changes without recording
raw user rows or credentials.

Tests must cover chunk bounds/overlap, exact substring reconstruction, stable IDs,
hashes and provenance, changed extraction snapshots, invalid configuration,
revision mismatches, multilingual/adversarial text, bounded resource failures,
fake determinism, and offline real inference where artifacts are available.
Stage 1 schema/migration/worker compatibility regressions remain in the full
backend suite. The existing extraction-worker driver exercises real HTTP,
PostgreSQL, extraction, download gating, cleanup, and reconciliation.

## Final verification record

Verification completed on 22 October 2026 against the unchanged source snapshot.
All final executed tests had zero failures, errors or skips. Backend JUnit totals
were parsed and checked independently. Ignored SHA-256 source evidence binds the
verification to the implementation; documentation-only result updates followed.

| Check | Observed result |
| --- | --- |
| Complete backend pytest | **1,364 passed**, 0 failed/errors/skipped; 746.38 seconds; owned database removed |
| New Stage 2 unit tests, included above | **273 passed**: chunking 102, embeddings 85, provisioning 56, settings 30 |
| Stage 1 regressions, included above | **90** schema/migration, **207** profile/contracts/settings, **17** worker compatibility tests passed |
| Complete frontend Vitest | **163 passed**, 11 files; 0 failed/skipped |
| Existing real extraction-worker/API/PostgreSQL checks | **79 passed**, 0 failed; owned database removed |
| Backend/scripts/migrations Ruff and formatting | Passed; 82 Python files formatted |
| TypeScript, frontend lint and production build | Passed |
| Poetry lock and disposable Alembic consistency | Lock passed; code head 0004; disposable upgrade/schema check passed |
| Real offline model smoke test | **24 checks passed**, 0 failed/skipped; socket connections blocked; no fake substitution |
| Python browser-harness unit tests | **4 passed**, 0 failed/skipped |
| Installed dependency consistency (`pip check`) | Passed; no broken requirements |
| Post-verification development baseline/storage comparison | Seven original count/SHA-256 snapshots unchanged; revision 0002; all eight newer tables absent; private root/activity lock absent |
| Secrets and generated-artifact review | No detected real secrets/artifacts in the 14 source changes; models/reports/environments/builds ignored; index empty |

Playwright browser tests were **not rerun** for Stage 2; no fresh browser result
is claimed. Its four Python harness unit tests were run separately. The final full backend log emitted no pytest warning summary; earlier checks
have reported the existing Starlette/httpx TestClient deprecation. The broader
frontend Python Ruff
check still returns I001 at `frontend/scripts/verify_workspace_api.py:8`; that
unrelated file is unchanged. Backend/migrations Ruff and frontend npm lint pass.
Git also reports normal LF/CRLF normalization warnings.

The real tokenizer emitted its over-model-length warning while mapping a whole
2,160-token section for chunking. This is not an inference failure: all actual
passage inputs were rechecked and bounded to 512 tokens before the model ran.
The warning was retained, not suppressed.

Initial package/model transfers timed out, and a smoke attempt during the
incomplete optional installation failed safely at tokenizer loading. The retry
completed and verified all six public artifacts; the final complete optional
installation and real offline smoke succeeded. An independently checksum-verified
public wheel was also fetched as a contingency but was not needed for installation.
A harness command initially used a nonexistent filename; the corrected
`frontend/scripts/test_e2e_runner.py` invocation passed all four tests. Those
unsuccessful attempts are not counted as passing verification.

## Review findings and final status

Implementation tests found and fixed a trailing-whitespace coverage defect before
release: the final chunk must extend through the complete source section rather
than choosing an earlier natural boundary. A regression now verifies this.
Acquisition review additionally hardened credential/port/fragment redirects,
malformed public metadata and LFS hash objects, duplicate entries and per-file
size checks. Invalid inputs are rejected before artifact requests where possible;
these are new-code hardening fixes, not claims of an exploited vulnerability.
Tokenizer and chunker character ceilings were aligned at 2,000,000 code points.

No private documents, credentials, database contents or query text were sent to
an external model provider. Acquisition requested only reviewed public model and
package artifacts. Runtime inference was demonstrated with socket connections
blocked. The source audit found no real credentials, private keys, databases,
model weights, upload files, environments or generated output in the source
change set. Existing placeholder database URLs in `.env.example` and synthetic
credentialed-URL rejection tests are not working credentials.

Final branch is `develop`; HEAD remains
`8cb43813f0f85acfe27ccf4d0285d414d859cf59`. The 14 source changes are unstaged
(5 modified and 9 new), and the staging index is empty. `git diff --check` passes.
`main` is unchanged. No commit, push, development migration, private-storage
initialization/cleanup, Qdrant installation or Stage 3 work was performed.
Public model acquisition adds approximately 493 MB of ignored artifact data;
local CPU use, disk and electricity are the development costs. No paid service
or hosted embedding API was activated.

## Local inference measurements

Only synthetic public text may be used for these measurements. Report OS, Python,
CPU, available RAM, dependency versions, thread count, batch size, token lengths,
model revision, and offline execution conditions. Separate model initialization
from warmed inference. Record observed vector dimension, normalization, batch
throughput, first-load time, warm latency, and approximate process memory using
the measurement method actually available. CPU results are local measurements,
not production capacity or retrieval-quality guarantees.

Observed real-model measurements on Windows 11 build 26300, Python 3.12.2,
AMD Ryzen 5 5500U (6 cores/12 logical processors), approximately 16 GB RAM
(OS-reported 15.33 GiB), CPU-only float32, two Torch threads:

| Measurement | Observed value |
| --- | --- |
| First load, including cold package imports and artifact hashing | 121.03 seconds |
| Warm single query, median of five | 49.67 ms |
| Warm query observed range | 46.90–238.97 ms |
| Batch of 8 passages, each 132 body tokens | 1.069 seconds |
| Batch throughput | 7.48 passages/second |
| Process working set before load | 36.6 MiB |
| Process working set after load / after inference | 1103.3 / 1135.7 MiB |
| Vector dimensions / normalization | 384 / finite nonzero unit norm |

Installed runtime versions: Torch 2.14.1+cpu, Transformers 5.20.2,
Tokenizers 0.23.3, Safetensors 0.8.0, Hugging Face Hub 2.2.0. No pre-existing
locked package version changed. The optional group adds 24 lockfile packages;
23 needed installation because one was already present.

Measurements used only synthetic English, Telugu, Hindi and mixed Unicode text
with network connections blocked. Other disposable regression work was running
on the machine, so timings are approximate observations under shared load, not
isolated capacity guarantees. Windows `GetProcessMemoryInfo` reports the process
working set, not a hard quota or exact model-only allocation. No retrieval
quality, Recall@K or real-user relevance is measured in this stage.

## Security, limitations, and next gate

- No external embedding API is enabled, and no private text or queries may leave
  the machine through the model acquisition or inference path.
- Public model artifacts and dependency packages still require supply-chain
  trust, pinned revisions, hash verification, and maintained security review.
  A revision pin does not replace a malware scan or operating-system isolation.
- Local caches and embeddings remain sensitive operational data. Filesystem
  permissions, encrypted disks, backups, log handling, and host administration
  are deployment responsibilities; no public model-cache route is added.
- Token, character, section, chunk, batch, and thread bounds limit work. Any
  platform limitation on hard time/memory containment must be recorded honestly.
- Unicode offsets refer to stored extracted text, not bytes, JavaScript UTF-16
  indices, PDF glyph coordinates, or original DOCX XML offsets.
- No vector index, persisted chunk generation, indexing worker, search endpoint,
  search UI, retrieval-quality score, RAG answer, or AI agent is implemented here.
- Windows/local CPU verification does not establish Linux, CUDA, other browsers,
  production HTTPS, or manual screen-reader coverage.
- Development database/storage remain protected. A future migration from 0002
  through 0003/0004 still requires the separately approved backup, restore-drill,
  writer-stop, migration, and record-preservation checklist in Stage 1.

## Changed-file inventory

The source change set contains **14 files: 5 modified and 9 new**.

| File | Purpose |
| --- | --- |
| `backend/src/agentsphere/chunking.py` (new) | Exact section fingerprints, bounded token chunks, Unicode spans, provenance, stable logical IDs and hashes |
| `backend/src/agentsphere/embeddings.py` (new) | Versioned local tokenizer/embedder, verified offline safetensors, pooling, validation and explicit test-only fake |
| `backend/src/agentsphere/search_config.py` | Local work/thread/character limits and pure settings-to-limit adapters |
| `backend/pyproject.toml` | Optional exact-pinned CPU embedding dependencies and explicit CPU wheel source |
| `backend/poetry.lock` | Matching dependency resolution; existing package versions preserved |
| `backend/.env.example` | Nonsecret reviewed public pins and Stage 2 limit templates; service flags remain false |
| `backend/scripts/prepare_local_embeddings.py` (new) | Explicit bounded public artifact acquisition, revision/license/size/hash checks and safe publication |
| `backend/scripts/verify_local_embeddings.py` (new) | Real synthetic offline smoke, Unicode/chunk invariants and CPU/memory measurements |
| `backend/tests/test_chunking.py` (new) | 102 exact-span, overlap, identity, provenance, Unicode and resource/error regressions |
| `backend/tests/test_embeddings.py` (new) | 85 loader, revision, batch, prefix, finite-vector, deterministic-fake and tensor pooling regressions |
| `backend/tests/test_embedding_provisioning.py` (new) | 56 synthetic acquisition, path, metadata, redirect, interruption, integrity and cleanup regressions |
| `backend/tests/test_chunk_embedding_config.py` (new) | 30 pure configuration default, mapping, limit and no-I/O regressions |
| `backend/README.md` | Optional installation, public provisioning and offline verification commands |
| `docs/MODULE6_STAGE2.md` (new) | This architecture, privacy, runbook, results, benchmark and limitation record |

No migration, API, extraction-worker, original private-storage, frontend source,
or Stage 1 schema/contract files changed. Ignored artifacts and logs under
`backend/.local`, local environments and frontend builds are excluded. No source
changes have been staged, committed or pushed.

Stop after implementation and verification. Staging, committing, pushing, and
Module 6 Stage 3 require the next explicit review and approval.

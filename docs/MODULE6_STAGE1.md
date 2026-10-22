# Module 6 Stage 1 — PostgreSQL Schema and Search Contracts

This stage establishes the database and validation foundation for later semantic
search. PostgreSQL remains the authority for document ownership and visibility.
Qdrant will be a rebuildable derived index in a separately approved stage.

Work starts on `develop` at
`4ad56f17c7ad710156e47f20089bb73bf4f23562`. The starting working tree was clean.
Implementation and verification began on 21 October 2026 and continued on
22 October 2026. Staging was separately authorized for commit review; no commit
or push is part of this stage.

## Scope and learning concepts

A **profile** identifies the exact embedding/chunking configuration. A **source
epoch** identifies one publication of extracted sections. A **generation** is one
attempt to build an index for that source/profile. A stable **logical chunk ID**
can survive a retry even though its generation-local database/vector row changes.
An **outbox job** preserves work and its target identifiers in PostgreSQL.

Implemented here: five new ORM models, additive revision 0004, database integrity
guards, pure Pydantic contracts, inactive settings, migration/constraint tests,
and narrow compatibility changes for existing verification tools.

Not implemented: text splitting, tokenization, embedding inference, model or
tokenizer downloads, Qdrant installation/connections/collections, vector indexing,
search routes, React search UI, ranking/evaluation, or automatic backfill. There
is no new network client or hosted embedding dependency. No document or query
content is sent to an external service.

## Relationships and source of truth

```text
documents (existing, owns workspace and lifecycle)
  └─ document_files (existing original and processing state)
       └─ document_search_sources (one current extraction epoch per document)
            └─ document_index_versions (document/profile/claim generations)
                 └─ document_chunks (generation-local rows, current section links)

embedding_profiles ── document_index_versions
vector_jobs ── optional live document/profile/generation links
            └─ immutable retained target identifiers for later cleanup
```

Existing `documents`, users, sessions, workspaces, memberships, extracted
sections, and processing jobs are not rewritten or given new mapped columns.
Document lifecycle (`registered`/`archived`) remains independent of file processing
and search-generation state. Metadata-only documents remain compatible and have
no extracted content to index automatically.

| New table | Purpose and important invariants |
| --- | --- |
| `embedding_profiles` | Required local model/tokenizer identities and 40-character pinned revisions; 384 dimensions; cosine normalization; prefixes; chunking parameters; unique immutable fingerprint and collection name |
| `document_search_sources` | Document/file FKs, unique extraction epoch, source/file hashes and parser version; must reference the same successfully validated extraction |
| `document_index_versions` | Snapshot of source epoch/fingerprint, profile FK, claim token and monotonic fence; manifest/count/verification fields; at most one active generation per document/profile |
| `document_chunks` | Stable logical identity, generation-local ordinal, exact text and hashes, declared token count, section identity/ordinal, Unicode code-point span and preserved source locations |
| `vector_jobs` | Index or generation-deletion outbox, lease/retry/fence fields, safe error codes, claim indexes, active-index uniqueness and permanent cleanup-target uniqueness |

Collections are named `as_<profile fingerprint>` by contract. Client text, display
filenames, credentials, and private filesystem paths cannot become collection names.
Workspace ownership is derived through the existing document relationships;
retained job workspace/document IDs are verified against live scope at creation.

## Immutable profiles, identities, and source references

`EmbeddingProfileSpec` is frozen and rejects extra fields. Its schema-version-1
fingerprint is SHA-256 over canonical sorted compact JSON, including explicit
defaults. PostgreSQL independently recomputes that fingerprint and rejects profile
updates. A different model revision or chunking setting requires a new profile.

The approved initial contract is `intfloat/multilingual-e5-small`, 384 dimensions,
512 model input tokens, normalized cosine vectors, `query: ` and `passage: ` input
prefixes, and a 350-token target with 50-token overlap. The schema conservatively
caps chunk body tokens at 480, reserving input space; later code must still verify
the exact prefixed tokenizer output and must not silently truncate it.

Revision syntax validation does not prove an artifact exists or that its weights
have been reviewed. Tests use synthetic revision strings; no model revision was
downloaded or activated here. Token counts and source fingerprints are stored
contracts, not results of an implemented tokenizer/source-snapshot algorithm.

`stable_chunk_id()` uses UUID5 over document, source/profile fingerprints, section
ordinal, content hash, and half-open span. It does not split text and deliberately
does not depend on a regenerated section UUID. Positional information keeps
identical text at different source locations distinct.

Offsets count Unicode code points in persisted section text, not UTF-8 bytes or
JavaScript UTF-16 units. Source page/heading/line fields must equal the current
parent section. Line locations initially have **section precision**. No narrower
raw-file line range or DOCX page number is invented from rendered text.

## State and publication guards

Generations begin `building` and inactive. Allowed transitions are:

```text
building → ready | empty | failed | obsolete
ready | empty | failed → obsolete
obsolete → no new state
```

`ready` requires a nonempty complete chunk set; `empty` requires zero chunks and
cannot be active. Completion/verification timestamps and a manifest hash are
required for both. A partial unique index allows only one active ready generation
per document/profile. Terminal generations are not rebuilt in place.

Database guards verify the current extraction epoch/fingerprint, successful file
processing, contiguous chunk ordinals and source references before publication.
They recheck the live indexing job's lease token, monotonic claim fence and expiry.
Publication requires **READ COMMITTED** isolation. Chunk deletion locks its
generation even while its committed state is still `building`, so a concurrent
publication cannot validate a chunk that is being removed. An old repeatable-read
snapshot is rejected rather than trusted for completeness.
Source/job locks are acquired with `NOWAIT` at that publication boundary so lock
contention fails safely rather than waiting in an inverted source/generation order.
Later workers must use short transactions, a consistent lock order and bounded
retry of applicable PostgreSQL concurrency failures; no worker implementation is
provided here.

Changing an extraction epoch makes previous generations obsolete and cancels
queued/running jobs for old epochs. Deleting/replacing sections invalidates their
previous generation instead of leaving ready rows with invented references. Chunk
content hashes are checked in PostgreSQL; prefixed-input hashes and section-text
substrings must match their profile/source.

The database cannot verify a remote vector write. A later trusted worker must
verify Qdrant acknowledgements/manifests before recording publication metadata.
These guards do not constitute distributed exactly-once processing.

## Cleanup retention and job contracts

Vector jobs begin `queued`; only a claim can advance their attempt/fence. Running
jobs require lease ownership/expiry, and terminal jobs require completion times.
Retry transitions preserve their immutable request/cleanup scope. Jobs do not
execute or enqueue themselves in this stage.

Nullable live FKs use `SET NULL`. Separate retained workspace, document, profile,
collection, source, and generation identifiers survive source deletion. The
chunk-to-generation and optional job-to-generation FKs are deferrable to allow
multiple cascade paths to settle; tests force constraints immediate afterward
to verify final consistency. Cleanup
tombstones remain unique even after a cleanup job reaches a terminal state.

Initial cleanup jobs require verified live provenance. A future reconciliation
worker must use retained verified tombstones when the original records are gone;
it must not synthesize arbitrary collection-deletion addresses. This stage does
not perform physical vector/file deletion, orphan reconciliation, or automatic
vector-job creation during document deletion.

## Configuration and API contracts

`SearchSettings` contributes flat `SEARCH_*` fields to existing `Settings` without
a nested settings factory rereading dotenv files. The existing `_env_file=None`
test behavior is preserved. Configuration validation performs no service/file I/O.

Keep both `SEARCH_ENABLED=false` and `SEARCH_INDEXING_ENABLED=false`. Hosted
embeddings are disabled and rejected if enabled; model loading must remain offline
with remote model code disabled. No fallback to a paid service exists.

The template includes explicit pinned revisions, absolute local model-cache
configuration, bounded batch/chunk/concurrency/timeout/lease/retry/query settings,
and private Qdrant origin/host restrictions. No real pins, endpoint credentials or
working service keys are supplied. Keys are `SecretStr`, require suitable length,
and read/write keys must differ. Credentials in URLs, URL paths/query fragments,
TLS verification bypass and public insecure HTTP origins are rejected. Loopback
HTTP requires explicit development permission; production requires HTTPS. An
allowed hostname is configuration intent, not proof of its eventual network route.

`SearchRequest`, `SourceReference`, `SearchResult`, and `SearchResponse` are future
data contracts only. They validate bounded queries/results/UUID filters, honest
locations, finite cosine scores, and duplicate/limit consistency. No endpoint is
registered, no request is authorized by these classes, and no exact token count,
search result, confidence probability, or retrieval quality is claimed.

## Migration and compatibility

Revision `0004` follows `0003`; all five tables are new and initially empty. Its
SQL guard functions are frozen in the migration and resolve names against their
own schema and PostgreSQL system schema. Upgrade does not import evolving model
definitions, backfill document text, change legacy rows, or initialize storage.

Downgrade takes exclusive locks and refuses if **any** Module 6 table is populated.
No automatic destructive fallback exists. An empty downgrade removes only the new
tables/functions and returns to 0003. Do not delete search rows merely to bypass
this guard on a real environment.

The necessary worker compatibility changes are its pre-storage startup check and
the original-file reconciliation health check accepting known revisions `0003`
and `0004`. Both still refuse 0002/unknown revisions, perform no migration, and use
unchanged extraction/file-cleanup algorithms. No search hook was
added to extraction publication. Disposable verification drivers now upgrade to
`head`; legacy migration tests retain explicit 0002/0003 coverage.

## Safe verification commands

Use the existing Poetry environment and existing PostgreSQL cluster. `start` is
permitted for the existing cluster; do not run `init` or copy credentials.

```powershell
# From backend; scripts create new run-owned databases and temporary storage:
.\.venv\Scripts\python.exe scripts/local_postgres.py start
.\.venv\Scripts\python.exe scripts/verify_stage2.py -q
# Focused new real PostgreSQL schema tests:
.\.venv\Scripts\python.exe scripts/verify_stage2.py -q tests/integration/test_search_schema.py
# Existing real HTTP API and separate extraction worker, against disposable head:
.\.venv\Scripts\python.exe scripts/verify_processing.py
.\.venv\Scripts\python.exe -m ruff check src tests scripts migrations
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts migrations
poetry check --lock
.\.venv\Scripts\python.exe -m alembic heads
# From frontend:
npm test
npm run typecheck
npm run lint
npm run build
```

`alembic heads` reads code only. Do not run bare `alembic upgrade`, `check` or
`current` against the default development connection as a verification shortcut.
The drivers run full schema consistency checks on their disposable databases.
Random test schemas/databases and test-only storage are removed by their owning
invocation; interrupted runs require exact ownership investigation, not wildcard
cleanup.

Tests cover clean upgrades, populated 0002/0003 upgrades, all legacy row/column
preservation and unchanged PostgreSQL PK/FK/unique/check/index definitions,
profile immutability, source mismatches,
incomplete/stale publication, token/fence reuse, lock contention, section replacement,
cleanup retention, guarded downgrade, and invalid configuration. Synthetic rows
prove schema behavior, not actual inference or Qdrant behavior.

## Final verification record — 22 October 2026

| Check | Final result |
| --- | --- |
| New pure contracts/configuration tests | 207 passed; 0 failed/skipped |
| Worker/reconciliation schema compatibility tests | 17 passed; 0 failed/skipped |
| Existing storage migration + startup subset | 53 passed; 0 failed/skipped |
| Full backend regression suite | **1,091 passed; 0 failed/errors/skipped**, 520.11 seconds; refreshed review JUnit totals verified |
| New schema suite, included in backend total | **90 passed; 0 failed/skipped**, including publication/deletion and isolation regressions |
| Frontend Vitest | 163 passed across 11 files; 0 failed/skipped |
| TypeScript, frontend lint and production build | Passed |
| Poetry lock validation | Passed; no dependency or lockfile change |
| Alembic code head | 0004 |
| Backend/migrations Ruff and formatting | Passed; 74 Python files formatted |
| Disposable Alembic consistency | Passed; no new upgrade operations detected |
| Offline 0003→0004 upgrade and guarded downgrade SQL rendering | Passed with a synthetic URL; no connection or migration executed |
| Existing real worker/API/PostgreSQL driver | 79 passed; 0 failed; all resources removed by their owner |
| Python browser-harness unit tests | 4 passed; 0 failed/skipped |

The initial schema failures exposed multi-path cascading-delete behavior for live
cleanup-job links. They were investigated instead of skipped. The implementation
now defers both affected generation FKs and tests immediate final consistency.
The initial schema suite had 86 passing tests. Review additionally fixed SQL CHECK
NULL semantics so a live generation cannot lose its retained target identifier.
All final executed tests/checks pass except the separately scoped, pre-existing
frontend Python I001 described below. No final pytest/Vitest test is skipped.

The first real worker regression run stopped at the reconciliation revision
guard: extraction, downloads and cleanup had passed, but the unchanged guard
still rejected 0004. That guard now accepts the additive revision and has eight
focused regressions. The first full backend attempt was deliberately stopped
before completion, its disposable database was removed by its owning parent,
and a fresh complete run passed after the fix. These incomplete/failed attempts
are not counted as final passing runs; ignored local logs retain them. The real
HTTP/worker driver then passed all 79 checks, including reconciliation at 0004.

### Authorized commit-review verification

The original implementation reported 1,087 backend and 86 schema tests. Final
review reproduced a chunk-deletion/publication race in both transaction orders:
the DELETE guard could see a committed `building` state while another transaction
was publishing it and skip the generation lock. A retained repeatable-read
snapshot could also validate a chunk already removed by another transaction.
All four new regressions failed against the previous guard. The frozen migration
now locks the generation before chunk deletion and rejects publication outside
READ COMMITTED. All four pass after the fix; totals are now **1,091 backend** and
**90 schema** tests. Existing preservation tests additionally compare PostgreSQL
PK, FK, unique/check constraints and index definitions before/after migration
and empty downgrade. These checks use only new disposable databases.

The complete backend, real HTTP/extraction-worker, frontend Vitest, Python
browser-harness, TypeScript, frontend lint/build, scoped Ruff/formatting, Poetry
lock and disposable Alembic checks were rerun for review. All final tests pass
without skips. The Playwright suite was **not rerun**, and no new browser result
is claimed. Review source hashes are recorded locally and checked before staging;
local test reports and source manifests are ignored rather than committed.

Clean and populated migration fixtures verified 0004, matching ORM metadata,
unchanged legacy table definitions and every pre-existing data value. The
expected Alembic-version change in disposable fixtures is intentional; normal
development remains 0002. Both final drivers removed only their own databases
and temporary storage. No Qdrant/model/inference test was run or claimed: none
of those components is implemented or installed in this stage. Playwright was
not rerun; frontend application code is unchanged, with the complete Vitest,
TypeScript/lint/build and real API/extraction-worker regressions executed.

Known warnings: the existing Starlette/httpx TestClient deprecation persists. A
broader check of frontend Python verification scripts has an existing unrelated
Ruff I001 import-order issue in `frontend/scripts/verify_workspace_api.py`; that
file is unchanged. Git can warn about LF/CRLF normalization. None of these is a
claim that an otherwise failing check passed.

## Development-data safety and separately approved upgrade checklist

A guarded repeatable-read **read-only** baseline confirmed development at 0002.
Counts and SHA-256 row snapshots cover all seven original tables including
`alembic_version`. No raw rows/credentials are stored in the evidence. Baseline
counts are: version 1, documents 0, users 1, sessions 29, workspaces 2,
memberships 0, and rate-limit buckets 57. All Module 5/6 tables are absent.
Read-only filesystem `lstat` checks found both the private storage root and sibling
activity-lock path absent; neither was initialized or cleaned. Evidence is ignored
under `backend/.local/module6-stage1/`. The final read-only comparison after both
drivers completed matched all seven counts/digests, revision 0002, all eight
newer tables absent, and both filesystem paths absent. It performs no DDL,
application writes, storage activity-context entry, or initialization.

Future development migration requires separate backup/migration approval:

1. Confirm exact intended database, loopback target, current revision, commit,
   storage location, grants and available space without printing secrets.
2. Stop writers and workers. Record read-only counts/hashes; back up PostgreSQL
   with a new protected exclusive dump and matching private objects if present.
3. Restore the dump into a new disposable environment; verify records and perform
   a restore drill. Never restore over development as a test.
4. Review the additive 0002 → 0003 → 0004 migration sequence and capability checks.
   Do not skip backup approval or run these migrations from application startup.
5. After explicit approval, apply the reviewed sequence with writers stopped,
   compare original-record snapshots and schema, then verify existing workflows.
6. On failure stop for investigation. Do not automatically downgrade populated
   tables, reset data, bypass guards, or clean private objects.

## Changed-file inventory

| File | Purpose |
| --- | --- |
| `backend/src/agentsphere/search_models.py` | Five additive ORM models and constraints/indexes |
| `backend/migrations/versions/0004_search_foundation.py` | Frozen additive schema, cross-row guards, schema-pinned functions and protected downgrade |
| `backend/migrations/env.py` | Registers the new metadata for Alembic consistency |
| `backend/src/agentsphere/search_contracts.py` | Pure profile, identity, state and future search data contracts |
| `backend/src/agentsphere/search_config.py` | Local-only inactive configuration and security/limit validation |
| `backend/src/agentsphere/config.py` | Incorporates flat search settings while preserving existing settings behavior |
| `backend/.env.example` | Nonsecret disabled configuration template |
| `backend/src/agentsphere/worker.py` | Necessary startup compatibility for known 0003/0004 schemas only |
| `backend/src/agentsphere/reconciliation.py` | Matching original-file reconciliation revision compatibility; algorithm unchanged |
| `backend/scripts/verify_stage2.py` | Disposable backend verifier upgrades to code head |
| `backend/scripts/verify_processing.py` | Disposable real-worker verifier upgrades to code head |
| `backend/tests/integration/test_document_storage_schema.py` | Updated head consistency assertions with explicit legacy migration coverage |
| `backend/tests/integration/test_search_schema.py` | Real PostgreSQL preservation, constraints, lifecycle, fencing and cleanup tests |
| `backend/tests/test_search_contracts.py` | Pure profile/identity/state/future-request validation regressions |
| `backend/tests/test_search_config.py` | Configuration safety, bounds, disabled defaults and no-I/O tests |
| `backend/tests/test_worker_schema.py` | Worker startup and reconciliation acceptance/refusal for known revisions |
| `backend/README.md` | Current schema-head/disposable verification and worker compatibility guidance |
| `docs/MODULE6_STAGE1.md` | This architecture, safety, verification and limitation record |

No dependencies, frontend application source, existing API routes, migration
0003, original storage implementation, or extraction algorithm changed. No
environments, uploads, private objects, database files, screenshots, credentials,
raw reports or generated build artifacts belong in the source inventory.

Review inventory: **18 files, 9 modified and 9 new**, with no unrelated files added. Pattern scanning
found only the existing commented placeholder database URLs in `.env.example`;
manual review confirmed they are templates, not credentials. No private keys,
real credentials, database files, user uploads or generated artifacts are part
of the changes. `.local`, `.venv`, frontend dependencies/builds and verification
reports remain ignored. `git diff --check` passes. Only this reviewed file set
is authorized for staging; committing and pushing require separate approval.
Branch remains `develop`, HEAD `4ad56f17c7ad710156e47f20089bb73bf4f23562`.
`main` remains `803ec0e20deca93174579f8413a0be49ffb4fcb8`.

## Limitations and next review gate

This is an inactive schema foundation. There is no working semantic search or
vector cleanup service to demonstrate. Profile-pin existence, real token counts,
multilingual retrieval quality, model memory/latency, Qdrant persistence/filtering,
and end-to-end distributed recovery remain unverified until later stages.

Existing Module 5 security/platform limits still apply: parsing is not malware
scanning, no OCR or production OS sandbox is added, and Linux/non-Chromium/manual
screen-reader/production HTTPS coverage has not been newly verified here.
Database-owner privileges can alter database guards; these controls do not defend
against a compromised PostgreSQL administrator. Operational private networking,
TLS, credentials and filesystem permissions need later service verification.

Stop after Stage 1 implementation and final regression verification. Do not stage,
commit, push, migrate development, initialize development storage, or begin Stage 2
without the requested review and approval.

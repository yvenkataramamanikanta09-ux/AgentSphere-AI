"""Run separately: python -m agentsphere.worker [--once | --reconcile]."""

import argparse
import hashlib
import json
import logging
import signal
from dataclasses import asdict
from threading import Event, Thread

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from agentsphere.config import Settings
from agentsphere.database import build_engine
from agentsphere.extraction import ExtractionError
from agentsphere.models import JobKind
from agentsphere.reconciliation import reconcile
from agentsphere.sandbox import extract_in_subprocess
from agentsphere.storage import PrivateStorage, StorageError
from agentsphere.worker_jobs import DurableJobQueue

logger = logging.getLogger(__name__)


class LeaseHeartbeat:
    def __init__(self, queue, claim, stopping):
        self.queue, self.claim, self.stopping = queue, claim, stopping
        self.finished, self.lost = Event(), Event()
        self.thread = Thread(target=self._run, name="job-heartbeat", daemon=True)

    def _run(self):
        while not self.finished.wait(min(5, self.queue.lease_seconds / 3)):
            try:
                if not self.queue.renew(self.claim):
                    self.lost.set()
                    return
            except SQLAlchemyError:
                self.lost.set()
                logger.warning("Job heartbeat unavailable; publication will be fenced.")
                return

    def cancelled(self):
        return self.stopping.is_set() or self.lost.is_set()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.finished.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            self.lost.set()


def read_original(storage, record, maximum):
    if not 1 <= record.size_bytes <= min(maximum, 10_000_000):
        raise ExtractionError("input_size_limit")
    digest, pieces, size = hashlib.sha256(), [], 0
    with storage.open(record.key) as handle:
        while piece := handle.read(65_536):
            size += len(piece)
            if size > record.size_bytes:
                raise ExtractionError("original_integrity_failed")
            digest.update(piece)
            pieces.append(piece)
    if size != record.size_bytes or digest.hexdigest() != record.sha256:
        raise ExtractionError("original_integrity_failed")
    return b"".join(pieces)


class ProcessingWorker:
    def __init__(self, queue, storage, settings, *, stopping=None, extractor=None):
        self.queue, self.storage, self.settings = queue, storage, settings
        self.stopping = stopping if stopping is not None else Event()
        # An injected extractor supports deterministic race/failure tests. The CLI
        # always uses the resource-contained, killable parser subprocess.
        self.extractor = extractor

    def _fail(self, claim, code, retryable):
        try:
            self.queue.fail(claim, code, retryable=retryable)
        except SQLAlchemyError:
            # Lease expiry makes this durable work recoverable after DB recovery.
            logger.warning("Job outcome unavailable; lease recovery is required.")

    def run_once(self):
        claim = self.queue.claim()
        if claim is None:
            return False
        try:
            if claim.exhausted:
                self._fail(claim, "attempts_exhausted", False)
                return True
            if claim.kind == JobKind.DELETE_FILES:
                with self.storage.activity():
                    if not self.queue.cleanup(claim, self.storage):
                        self._fail(claim, "cleanup_deferred", True)
                return True
            record = self.queue.begin(claim)
            if record is None:
                self._fail(claim, "document_unavailable", False)
                return True
            with LeaseHeartbeat(self.queue, claim, self.stopping) as heartbeat:
                data = read_original(self.storage, record, self.settings.max_upload_bytes)
                if heartbeat.cancelled():
                    raise ExtractionError("worker_interrupted", retryable=True)
                result = (
                    self.extractor(data, record.content_type)
                    if self.extractor is not None
                    else extract_in_subprocess(
                        data, record.content_type, self.settings, cancelled=heartbeat.cancelled
                    )
                )
                if heartbeat.cancelled():
                    raise ExtractionError("worker_interrupted", retryable=True)
                self.queue.succeed(claim, result)
        except ExtractionError as error:
            self._fail(claim, error.code, error.retryable)
        except StorageError:
            self._fail(claim, "storage_unavailable", True)
        except SQLAlchemyError:
            logger.warning("Job database operation unavailable; no unfenced result published.")
            self._fail(claim, "database_unavailable", True)
        except Exception as error:
            logger.warning("Job execution unavailable (%s).", type(error).__name__)
            self._fail(claim, "processing_unavailable", True)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Attempt one available job, then exit.")
    mode.add_argument(
        "--reconcile", action="store_true", help="Conservative maintenance pass, then exit."
    )
    args = parser.parse_args(argv)
    settings = Settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is required for the separate worker.")
    engine = build_engine(settings.database_url.get_secret_value())
    factory = sessionmaker(engine, expire_on_commit=False)
    stopping = Event()

    def stop(*_):
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        # 0004 is additive; extraction still uses only the 0003 tables and jobs.
        # Fail before storage initialization on older/unknown schemas; never migrate.
        with engine.connect() as db:
            if db.scalar(text("SELECT version_num FROM alembic_version")) not in {"0003", "0004"}:
                raise SystemExit("Worker requires the separately approved 0003 or 0004 migration.")
        storage = PrivateStorage(settings.private_storage_root)
        storage.initialize(max_entries=settings.reconciliation_max_entries)
        if args.reconcile:
            result = reconcile(
                factory,
                storage,
                min_age_seconds=settings.reconciliation_min_age_seconds,
                max_entries=settings.reconciliation_max_entries,
            )
            print(json.dumps(asdict(result)))
            return 0 if result.complete else 1
        queue = DurableJobQueue(
            factory, settings.worker_lease_seconds, settings.worker_retry_base_seconds
        )
        worker = ProcessingWorker(queue, storage, settings, stopping=stopping)
        if args.once:
            worker.run_once()
            return 0
        logger.info("Document worker started; parsing is performed in child processes.")
        while not stopping.is_set():
            try:
                if not worker.run_once():
                    stopping.wait(settings.worker_poll_seconds)
            except SQLAlchemyError:
                logger.warning("Job claim unavailable; retrying after polling interval.")
                stopping.wait(settings.worker_poll_seconds)
        return 0
    except (SQLAlchemyError, StorageError):
        raise SystemExit(
            "Worker storage/database unavailable; no migration was attempted."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())

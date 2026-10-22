"""Worker CLI schema compatibility fails closed before any storage initialization."""

from unittest.mock import Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from agentsphere import reconciliation, worker
from agentsphere.config import Settings


@pytest.fixture
def startup(monkeypatch):
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test@127.0.0.1/worker_startup_test",
        test_database_url=None,
    )
    engine, connection, storage = Mock(), Mock(), Mock()
    engine.connect.return_value.__enter__ = Mock(return_value=connection)
    engine.connect.return_value.__exit__ = Mock(return_value=False)
    storage_factory, processing_factory = Mock(return_value=storage), Mock()
    monkeypatch.setattr(worker, "Settings", lambda: settings)
    monkeypatch.setattr(worker, "build_engine", Mock(return_value=engine))
    monkeypatch.setattr(worker, "sessionmaker", Mock())
    monkeypatch.setattr(worker.signal, "signal", Mock())
    monkeypatch.setattr(worker, "PrivateStorage", storage_factory)
    monkeypatch.setattr(worker, "DurableJobQueue", Mock())
    monkeypatch.setattr(worker, "ProcessingWorker", processing_factory)
    return engine, connection, storage_factory, storage, processing_factory


@pytest.mark.parametrize("revision", ["0003", "0004"])
def test_supported_schema_runs_existing_processing_only_after_revision_check(startup, revision):
    engine, connection, storage_factory, storage, processing_factory = startup
    events = []

    def revision_checked(_statement):
        events.append("revision_checked")
        return revision

    connection.scalar.side_effect = revision_checked
    storage.initialize.side_effect = lambda **_kwargs: events.append("storage_initialized")
    assert worker.main(["--once"]) == 0
    assert events == ["revision_checked", "storage_initialized"]
    storage_factory.assert_called_once()
    processing_factory.return_value.run_once.assert_called_once_with()
    engine.dispose.assert_called_once_with()


@pytest.mark.parametrize("revision", [None, "0000", "0001", "0002", "0005", "0003-untrusted"])
def test_unknown_or_older_schema_refuses_before_storage_writes(startup, revision):
    engine, connection, storage_factory, storage, processing_factory = startup
    connection.scalar.return_value = revision
    with pytest.raises(SystemExit, match="0003 or 0004 migration"):
        worker.main(["--once"])
    storage_factory.assert_not_called()
    storage.initialize.assert_not_called()
    processing_factory.assert_not_called()
    engine.dispose.assert_called_once_with()


def test_failed_revision_lookup_refuses_without_storage_writes_or_private_diagnostics(startup):
    engine, connection, storage_factory, storage, processing_factory = startup
    connection.scalar.side_effect = SQLAlchemyError("private database detail")
    with pytest.raises(SystemExit, match="no migration was attempted") as caught:
        worker.main(["--once"])
    assert "private database detail" not in str(caught.value)
    storage_factory.assert_not_called()
    storage.initialize.assert_not_called()
    processing_factory.assert_not_called()
    engine.dispose.assert_called_once_with()


@pytest.mark.parametrize("revision", ["0003", "0004"])
def test_original_file_reconciliation_accepts_additive_schema(monkeypatch, revision):
    database = Mock()
    database.scalar.side_effect = ["read committed", revision]
    storage_schema_check = Mock()
    monkeypatch.setattr(reconciliation, "require_storage_schema", storage_schema_check)
    reconciliation._healthy(database)
    storage_schema_check.assert_called_once_with(database)


@pytest.mark.parametrize("revision", [None, "0000", "0001", "0002", "0005", "0004-untrusted"])
def test_original_file_reconciliation_rejects_older_or_unknown_schema(monkeypatch, revision):
    database = Mock()
    database.scalar.side_effect = ["read committed", revision]
    monkeypatch.setattr(reconciliation, "require_storage_schema", Mock())
    with pytest.raises(ValueError, match="0003 or 0004"):
        reconciliation._healthy(database)

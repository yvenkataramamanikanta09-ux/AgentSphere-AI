"""Connection guard tests are pure and never require a PostgreSQL server."""

import pytest
from conftest import validate_test_database_url


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_accepts_dedicated_loopback_database(host):
    validate_test_database_url(
        f"postgresql+psycopg://user:private-password@{host}:55432/agentsphere_test",
        "postgresql+psycopg://user:private-password@localhost:55432/agentsphere",
    )


@pytest.mark.parametrize(
    "query",
    [
        "dbname=agentsphere",
        "host=database.example.com",
        "hostaddr=203.0.113.1",
        "port=5432",
        "options=-c%20search_path=public",
        "service=production",
        "sslmode=require",
    ],
)
def test_rejects_all_test_connection_query_parameters(query):
    with pytest.raises(ValueError, match="query parameters"):
        validate_test_database_url(
            f"postgresql+psycopg://user:private-password@localhost/agentsphere_test?{query}"
        )


@pytest.mark.parametrize("host", ["database.example.com", "192.168.1.2", "127.0.0.2", ""])
def test_rejects_non_loopback_or_implicit_hosts(host):
    with pytest.raises(ValueError, match="loopback"):
        validate_test_database_url(f"postgresql+psycopg://user@{host}/agentsphere_test")


@pytest.mark.parametrize(
    "database",
    ["agentsphere", "agentsphere_test_backup", "", "dbname=agentsphere_test", "../local_test"],
)
def test_rejects_non_dedicated_database_names(database):
    with pytest.raises(ValueError, match="dedicated database"):
        validate_test_database_url(f"postgresql+psycopg://user@localhost/{database}")


@pytest.mark.parametrize("driver", ["postgresql", "postgresql+psycopg2", "mysql", "sqlite"])
def test_rejects_wrong_driver(driver):
    with pytest.raises(ValueError, match="postgresql"):
        validate_test_database_url(f"{driver}://user@localhost/agentsphere_test")


@pytest.mark.parametrize("development_host", ["localhost", "127.0.0.1", "[::1]", "db-alias"])
def test_rejects_same_database_even_when_hosts_may_be_aliases(development_host):
    with pytest.raises(ValueError, match="must be different"):
        validate_test_database_url(
            "postgresql+psycopg://user@127.0.0.1:5432/agentsphere_test",
            f"postgresql+psycopg://user@{development_host}/agentsphere_test",
        )


def test_allows_same_database_name_on_different_ports():
    validate_test_database_url(
        "postgresql+psycopg://user@localhost:55432/agentsphere_test",
        "postgresql+psycopg://user@localhost:5432/agentsphere_test",
    )


def test_rejects_development_query_overrides():
    with pytest.raises(ValueError, match="Development database URL.*query parameters"):
        validate_test_database_url(
            "postgresql+psycopg://user@localhost/agentsphere_test",
            "postgresql+psycopg://user@localhost/agentsphere?dbname=agentsphere_test",
        )


@pytest.mark.parametrize("url", ["not-a-url", "postgresql+psycopg://localhost:noport/db_test"])
def test_invalid_url_diagnostics_do_not_expose_input(url):
    with pytest.raises(ValueError) as caught:
        validate_test_database_url(url)
    assert url not in str(caught.value)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_rejects_invalid_port(port):
    with pytest.raises(ValueError, match="port"):
        validate_test_database_url(f"postgresql+psycopg://localhost:{port}/agentsphere_test")


def test_diagnostics_do_not_include_credentials():
    with pytest.raises(ValueError) as caught:
        validate_test_database_url(
            "postgresql+psycopg://private-user:private-password@remote/db_test"
        )
    assert "private-user" not in str(caught.value)
    assert "private-password" not in str(caught.value)

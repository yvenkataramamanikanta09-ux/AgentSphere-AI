import pytest
from pydantic import ValidationError

from agentsphere.config import Settings


@pytest.mark.parametrize(
    "values",
    [
        {"app_origin": "http://localhost:8000"},
        {"app_origin": "https://localhost/docs"},
        {"app_origin": "https://user:password@localhost"},
        {"environment": "production", "allow_insecure_local_http": True},
        {"app_origin": "http://example.com", "allow_insecure_local_http": True},
        {"auth_rate_limit_key": "short"},
        {"environment": "production"},
    ],
)
def test_unsafe_security_settings_rejected(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_explicit_loopback_development_cookie_name():
    settings = Settings(
        _env_file=None, app_origin="http://127.0.0.1:8000", allow_insecure_local_http=True
    )
    assert settings.cookie_name == "agentsphere_dev_session"
    assert Settings(_env_file=None).cookie_name.startswith("__Host-")


def test_password_concurrency_is_bounded(monkeypatch):
    from fastapi import HTTPException

    from agentsphere import security

    class BusySlots:
        def acquire(self, timeout):
            return False

    monkeypatch.setattr(security, "PASSWORD_SLOTS", BusySlots())
    with pytest.raises(HTTPException) as error:
        security.hash_password("not-allocated-while-busy")
    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "1"}

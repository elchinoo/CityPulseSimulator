import os
from urllib.parse import urlsplit

import pytest

for name in ("DB_EXTERNAL_DSN", "PSQL_CONNECTION_STRING", "CPSIM_API_KEY", "CPSIM_ALLOW_ANONYMOUS", "CPSIM_OIDC_ISSUER",
             "CPSIM_SERVICE_CLIENT_SECRET", "CITYPULSE_URL", "PAYBROKER_URL", "TRUSTDESK_URL", "PAGERLENS_URL", "LLMGW_URL", "IDENTITY_URL", "PAGERLENS_TOKEN", "CPSIM_METRICS_TOKEN", "CPSIM_MAX_RATE",
             "CPSIM_ALLOW_HTTP_TARGETS", "PAYBROKER_SECRET_KEY", "LLMGW_CLIENT_KEY",
             "LLM_API_KEY", "LLM_CHAT_MODEL", "EMBEDDING_MODEL", "SHOPFRONT_URL", "SHOPFRONT_TOKEN"):
    os.environ.pop(name, None)
os.environ.pop("CPSIM_TARGET_ALLOWLIST", None)
os.environ.pop("CPSIM_TOKEN_ENV_ALLOWLIST", None)
os.environ.pop("CPSIM_ALLOW_PRIVATE_URLS", None)
os.environ["CPSIM_API_KEY_FILE"] = "/nonexistent/key"
os.environ["CPSIM_METRICS_TOKEN_FILE"] = "/nonexistent/key"
os.environ["CPSIM_SERVICE_CLIENT_SECRET_FILE"] = "/nonexistent/key"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
if TEST_DATABASE_URL:
    assert urlsplit(TEST_DATABASE_URL).path.lstrip("/").endswith("test"), "TEST_DATABASE_URL must name a throwaway database (the tests drop its schema)"
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
else:
    os.environ.pop("DATABASE_URL", None)


@pytest.fixture
def db():
    """An empty, migrated database (TEST_DATABASE_URL); the test is skipped without one."""
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run DB tests")
    from cpsim import api, core, migrate

    with core.db.connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.commit()
    migrate.run()
    core.auth.limiter.clear()
    core.local._setup_code = None
    api._announced = False
    api.runtime.hr_runs.clear()
    api.runtime.wl_runners.clear()
    api.runtime.company_schedules.clear()
    yield


class _OfflineGuard:
    """Stands in for the NetGuard in the tests that use made-up hosts (no DNS): same normalisation, no resolution."""

    def check(self, url):
        return url.rstrip("/")


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    """The target allow-list stays real (the tests' hosts are listed here); only the DNS part of the NetGuard is replaced."""
    from cpsim import outbound
    monkeypatch.setattr(outbound, "guard", _OfflineGuard())
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", ".example .example.org .invalid x paybroker pagerlens localhost 127.0.0.1")
    monkeypatch.setenv("CPSIM_TOKEN_ENV_ALLOWLIST", "MY_TOKEN MY_OWN_KEY MY_SHOP_SECRET_KEY MY_GATEWAY_KEY MY_CLIENT_TOKEN X_TOKEN X_KEY GATEWAY_KEY PAYBROKER_KEY")

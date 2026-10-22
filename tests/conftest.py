import os
from urllib.parse import urlsplit

import pytest

for name in ("DB_EXTERNAL_DSN", "PSQL_CONNECTION_STRING", "CPSIM_API_KEY", "CPSIM_ALLOW_ANONYMOUS", "CPSIM_OIDC_ISSUER",
             "CPSIM_SERVICE_CLIENT_SECRET", "PAGERLENS_URL", "PAGERLENS_TOKEN", "CPSIM_METRICS_TOKEN"):
    os.environ.pop(name, None)
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
    yield

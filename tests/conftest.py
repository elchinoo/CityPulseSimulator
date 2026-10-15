import os
import tempfile

os.environ["CPSIM_DATA_DIR"] = tempfile.mkdtemp(prefix="cpsim-test-")
for name in ("DATABASE_URL", "DB_EXTERNAL_DSN", "PSQL_CONNECTION_STRING", "CPSIM_API_KEY", "CPSIM_ALLOW_ANONYMOUS",
             "PAGERLENS_URL", "PAGERLENS_TOKEN", "CPSIM_METRICS_TOKEN"):
    os.environ.pop(name, None)
os.environ["CPSIM_API_KEY_FILE"] = "/nonexistent/key"
os.environ["CPSIM_METRICS_TOKEN_FILE"] = "/nonexistent/key"

import os
import sys
from dataclasses import dataclass


def read_secret(env: str, default_file: str = "") -> str:
    """Secret from the environment variable, else the first line of the file named by <ENV>_FILE."""
    value = os.environ.get(env, "")
    if value:
        return value
    path = os.environ.get(f"{env}_FILE", default_file)
    if not path:
        return ""
    try:
        with open(path) as fh:
            return fh.readline().strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        print(f"WARNING: cannot read {env} file {path}: {exc.strerror}", file=sys.stderr)
        return ""


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: str
    api_key: str
    metrics_token: str
    allow_anonymous: bool
    pagerlens_url: str
    pagerlens_token: str


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL") or os.environ.get("DB_EXTERNAL_DSN") or os.environ.get("PSQL_CONNECTION_STRING") or "",
        data_dir=os.environ.get("CPSIM_DATA_DIR", "data"),
        api_key=read_secret("CPSIM_API_KEY", "/etc/cpsim/api_key"),
        metrics_token=read_secret("CPSIM_METRICS_TOKEN", "/etc/cpsim/metrics_token"),
        allow_anonymous=os.environ.get("CPSIM_ALLOW_ANONYMOUS", "").lower() in {"1", "true", "yes"},
        pagerlens_url=os.environ.get("PAGERLENS_URL", "").rstrip("/"),
        pagerlens_token=read_secret("PAGERLENS_TOKEN", "/etc/cpsim/pagerlens_token"),
    )

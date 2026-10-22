import os
from dataclasses import dataclass

from apgkit.config import read_secret


@dataclass(frozen=True)
class Settings:
    pagerlens_url: str
    pagerlens_token: str


def get_settings() -> Settings:
    return Settings(
        pagerlens_url=os.environ.get("PAGERLENS_URL", "").rstrip("/"),
        pagerlens_token=read_secret("PAGERLENS_TOKEN", "/etc/cpsim/pagerlens_token"),
    )

from dataclasses import dataclass
from typing import Optional

from yarl import URL


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True)
class ZipkinConfig:
    url: URL
    app_name: str = "platform-container-runtime"
    sample_rate: float = 0.0


@dataclass(frozen=True)
class SentryConfig:
    dsn: URL
    cluster_name: str
    app_name: str = "platform-container-runtime"
    sample_rate: float = 0.0


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    cri_address: str
    zipkin: Optional[ZipkinConfig] = None
    sentry: Optional[SentryConfig] = None

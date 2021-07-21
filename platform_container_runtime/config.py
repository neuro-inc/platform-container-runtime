import os
from dataclasses import dataclass
from typing import Dict, Optional

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


class EnvironConfigFactory:
    def __init__(self, environ: Optional[Dict[str, str]] = None) -> None:
        self._environ = environ or os.environ

    def create(self) -> Config:
        return Config(
            server=self._create_server(),
            cri_address=self._environ["NP_CRI_ADDRESS"],
            zipkin=self.create_zipkin(),
            sentry=self.create_sentry(),
        )

    def _create_server(self) -> ServerConfig:
        host = self._environ.get("NP_HOST", ServerConfig.host)
        port = int(self._environ.get("NP_PORT", ServerConfig.port))
        return ServerConfig(host=host, port=port)

    def create_zipkin(self) -> Optional[ZipkinConfig]:
        if "NP_ZIPKIN_URL" not in self._environ:
            return None

        url = URL(self._environ["NP_ZIPKIN_URL"])
        app_name = self._environ.get("NP_ZIPKIN_APP_NAME", ZipkinConfig.app_name)
        sample_rate = float(
            self._environ.get("NP_ZIPKIN_SAMPLE_RATE", ZipkinConfig.sample_rate)
        )
        return ZipkinConfig(url=url, app_name=app_name, sample_rate=sample_rate)

    def create_sentry(self) -> Optional[SentryConfig]:
        if "NP_SENTRY_DSN" not in self._environ:
            return None

        return SentryConfig(
            dsn=URL(self._environ["NP_SENTRY_DSN"]),
            cluster_name=self._environ["NP_SENTRY_CLUSTER_NAME"],
            app_name=self._environ.get("NP_SENTRY_APP_NAME", SentryConfig.app_name),
            sample_rate=float(
                self._environ.get("NP_SENTRY_SAMPLE_RATE", SentryConfig.sample_rate)
            ),
        )

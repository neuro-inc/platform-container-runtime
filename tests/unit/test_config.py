from typing import Any, Dict

from yarl import URL

from platform_container_runtime.config import (
    Config,
    SentryConfig,
    ServerConfig,
    ZipkinConfig,
)
from platform_container_runtime.config_factory import EnvironConfigFactory


def test_create() -> None:
    environ: Dict[str, Any] = {
        "NP_HOST": "0.0.0.0",
        "NP_PORT": 8080,
        "NP_ZIPKIN_URL": "http://zipkin:9411",
        "NP_SENTRY_DSN": "https://test.com",
        "NP_SENTRY_CLUSTER_NAME": "test",
    }
    config = EnvironConfigFactory(environ).create()
    assert config == Config(
        server=ServerConfig(host="0.0.0.0", port=8080),
        zipkin=ZipkinConfig(url=URL("http://zipkin:9411")),
        sentry=SentryConfig(dsn=URL("https://test.com"), cluster_name="test"),
    )

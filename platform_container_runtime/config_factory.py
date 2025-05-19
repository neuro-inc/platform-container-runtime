import os
from typing import Optional

from yarl import URL

from .config import (
    Config,
    KubeClientAuthType,
    KubeConfig,
    ServerConfig,
)


class EnvironConfigFactory:
    def __init__(self, environ: Optional[dict[str, str]] = None) -> None:
        self._environ = environ or os.environ

    def create(self) -> Config:
        return Config(
            server=self.create_server(),
            node_name=self._environ["NP_NODE_NAME"],
            kube=self.create_kube(),
            cri_address=self._environ.get("NP_CRI_ADDRESS", Config.cri_address),
            runtime_address=self._environ.get(
                "NP_RUNTIME_ADDRESS", Config.runtime_address
            ),
        )

    def create_server(self) -> ServerConfig:
        host = self._environ.get("NP_HOST", ServerConfig.host)
        port = int(self._environ.get("NP_PORT", ServerConfig.port))
        return ServerConfig(host=host, port=port)

    def create_kube(self) -> KubeConfig:
        return KubeConfig(
            url=URL(self._environ["NP_KUBE_URL"]),
            auth_type=KubeClientAuthType(
                self._environ.get("NP_KUBE_AUTH_TYPE", KubeConfig.auth_type.value)
            ),
            token=self._environ.get("NP_KUBE_TOKEN"),
            token_path=self._environ.get("NP_KUBE_TOKEN_PATH"),
            cert_authority_data_pem=self._environ.get("NP_KUBE_CERT_AUTHORITY_DATA"),
            cert_authority_path=self._environ.get("NP_KUBE_CERT_AUTHORITY_PATH"),
            client_cert_path=self._environ.get("NP_KUBE_CLIENT_CERT_PATH"),
            client_key_path=self._environ.get("NP_KUBE_CLIENT_KEY_PATH"),
            conn_force_close=_parse_bool(
                self._environ.get("NP_KUBE_CONN_FORCE_CLOSE", "0")
            ),
            conn_timeout_s=int(
                self._environ.get("NP_KUBE_CONN_TIMEOUT", KubeConfig.conn_timeout_s)
            ),
            read_timeout_s=int(
                self._environ.get("NP_KUBE_READ_TIMEOUT", KubeConfig.read_timeout_s)
            ),
            conn_pool_size=int(
                self._environ.get("NP_KUBE_CONN_POOL_SIZE", KubeConfig.conn_pool_size)
            ),
            conn_keep_alive_timeout_s=int(
                self._environ.get(
                    "NP_KUBE_CONN_KEEP_ALIVE_TIMEOUT",
                    KubeConfig.conn_keep_alive_timeout_s,
                )
            ),
        )


def _parse_bool(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")

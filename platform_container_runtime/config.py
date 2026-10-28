import enum
from dataclasses import dataclass, field

from yarl import URL


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


class KubeClientAuthType(enum.Enum):
    NONE = "none"
    TOKEN = "token"
    CERTIFICATE = "certificate"


@dataclass(frozen=True)
class KubeConfig:
    url: URL
    cert_authority_path: str | None = None
    cert_authority_data_pem: str | None = None
    auth_type: KubeClientAuthType = KubeClientAuthType.NONE
    client_cert_path: str | None = None
    client_key_path: str | None = None
    token: str | None = field(repr=False, default=None)
    token_path: str | None = None
    token_update_interval_s: int = 300
    conn_force_close: bool = False
    conn_timeout_s: int = 300
    read_timeout_s: int = 100
    conn_pool_size: int = 100
    conn_keep_alive_timeout_s: int = 15


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    node_name: str
    kube: KubeConfig
    cri_address: str | None = None
    runtime_address: str | None = None

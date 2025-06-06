import enum
from dataclasses import dataclass
from typing import Optional

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
    cert_authority_path: Optional[str] = None
    cert_authority_data_pem: Optional[str] = None
    auth_type: KubeClientAuthType = KubeClientAuthType.NONE
    client_cert_path: Optional[str] = None
    client_key_path: Optional[str] = None
    token: Optional[str] = None
    token_path: Optional[str] = None
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
    cri_address: Optional[str] = None
    runtime_address: Optional[str] = None

import logging
import ssl
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

import aiohttp

from .config import KubeClientAuthType, KubeConfig

logger = logging.getLogger(__name__)


class KubeClientUnautorized(Exception):
    pass


class KubeClientException(Exception):
    pass


@dataclass(frozen=True)
class Metadata:
    name: str
    labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Metadata":
        return cls(name=payload["name"], labels=payload.get("labels", {}))


@dataclass(frozen=True)
class Node:
    metadata: Metadata
    container_runtime_version: str
    os: str
    architecture: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Node":
        return cls(
            metadata=Metadata.from_payload(payload["metadata"]),
            container_runtime_version=payload["status"]["nodeInfo"][
                "containerRuntimeVersion"
            ],
            os=payload["status"]["nodeInfo"]["operatingSystem"],
            architecture=payload["status"]["nodeInfo"]["architecture"],
        )


class KubeClient:
    def __init__(
        self,
        config: KubeConfig,
        trace_configs: Optional[list[aiohttp.TraceConfig]] = None,
    ) -> None:
        self._config = config
        self._token = config.token
        self._trace_configs = trace_configs
        self._client: Optional[aiohttp.ClientSession] = None

    def _create_ssl_context(self) -> Optional[ssl.SSLContext]:
        if self._config.url.scheme != "https":
            return None
        ssl_context = ssl.create_default_context(
            cafile=self._config.cert_authority_path,
            cadata=self._config.cert_authority_data_pem,
        )
        if self._config.auth_type == KubeClientAuthType.CERTIFICATE:
            ssl_context.load_cert_chain(
                self._config.client_cert_path,  # type: ignore
                self._config.client_key_path,
            )
        return ssl_context

    async def __aenter__(self) -> "KubeClient":
        self._client = await self._create_http_client()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    async def _create_http_client(self) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(
            limit=self._config.conn_pool_size,
            force_close=self._config.conn_force_close,
            ssl=self._create_ssl_context(),
        )
        if self._config.auth_type == KubeClientAuthType.TOKEN:
            token = self._token
            if not token:
                assert self._config.token_path is not None
                token = Path(self._config.token_path).read_text()
            headers = {"Authorization": "Bearer " + token}
        else:
            headers = {}
        timeout = aiohttp.ClientTimeout(
            connect=self._config.conn_timeout_s, total=self._config.read_timeout_s
        )
        return aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            trace_configs=self._trace_configs,
        )

    async def _reload_http_client(self) -> None:
        if self._client:
            await self._client.close()
        self._token = None
        self._client = await self._create_http_client()

    async def aclose(self) -> None:
        assert self._client
        await self._client.close()

    async def request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        assert self._client, "client is not intialized"
        doing_retry = kwargs.pop("doing_retry", False)

        async with self._client.request(*args, **kwargs) as resp:
            resp_payload = await resp.json()
        try:
            self._raise_for_status(resp_payload)
            return resp_payload
        except KubeClientUnautorized:
            if doing_retry:
                raise
            # K8s SA's token might be stale, need to refresh it and retry
            await self._reload_http_client()
            kwargs["doing_retry"] = True
            return await self.request(*args, **kwargs)

    def _raise_for_status(self, payload: dict[str, Any]) -> None:
        kind = payload["kind"]
        if kind == "Status":
            if payload.get("status") == "Success":
                return
            code = payload.get("code")
            if code == 401:
                raise KubeClientUnautorized(payload)
            raise KubeClientException(payload)

    async def get_nodes(self) -> Sequence[Node]:
        payload = await self.request(
            method="get", url=self._config.url / "api/v1/nodes"
        )
        assert payload["kind"] == "NodeList"
        return [Node.from_payload(p) for p in payload["items"]]

    async def get_node(self, name: str) -> Node:
        payload = await self.request(
            method="get", url=self._config.url / "api/v1/nodes" / name
        )
        assert payload["kind"] == "Node"
        return Node.from_payload(payload)

import asyncio
import logging
import ssl
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

import aiohttp

from .config import KubeClientAuthType, KubeConfig

SSLContext = ssl.SSLContext
logger = logging.getLogger(__name__)


class KubeClientUnauthorized(Exception):
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
        self._token_updater_task: Optional[asyncio.Task[None]] = None

    def _create_ssl_context(self) -> bool | SSLContext:
        if self._config.url.scheme != "https":
            return False
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
        await self._init()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    async def _init(self) -> None:
        connector = aiohttp.TCPConnector(
            limit=self._config.conn_pool_size,
            force_close=self._config.conn_force_close,
            ssl=self._create_ssl_context(),
        )
        if self._config.token_path:
            self._token = Path(self._config.token_path).read_text()
            self._token_updater_task = asyncio.create_task(self._start_token_updater())
        timeout = aiohttp.ClientTimeout(
            connect=self._config.conn_timeout_s, total=self._config.read_timeout_s
        )
        self._client = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            trace_configs=self._trace_configs,
        )

    async def _start_token_updater(self) -> None:
        if not self._config.token_path:
            return
        while True:
            try:
                token = Path(self._config.token_path).read_text()
                if token != self._token:
                    self._token = token
                    logger.info("Kube token was refreshed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Failed to update kube token: %s", exc)
            await asyncio.sleep(self._config.token_update_interval_s)

    async def aclose(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None
        if self._token_updater_task:
            self._token_updater_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._token_updater_task
            self._token_updater_task = None

    def _create_headers(
        self, headers: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        headers = dict(headers) if headers else {}
        if self._config.auth_type == KubeClientAuthType.TOKEN and self._token:
            headers["Authorization"] = "Bearer " + self._token
        return headers

    async def _request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        headers = self._create_headers(kwargs.pop("headers", None))
        assert self._client, "client is not initialized"
        async with self._client.request(*args, headers=headers, **kwargs) as resp:
            resp_payload = await resp.json()
            self._raise_for_status(resp_payload)
            return resp_payload

    def _raise_for_status(self, payload: dict[str, Any]) -> None:
        kind = payload["kind"]
        if kind == "Status":
            if payload.get("status") == "Success":
                return
            code = payload.get("code")
            if code == 401:
                raise KubeClientUnauthorized(payload)
            raise KubeClientException(payload)

    async def get_nodes(self) -> Sequence[Node]:
        payload = await self._request(
            method="get", url=self._config.url / "api/v1/nodes"
        )
        assert payload["kind"] == "NodeList"
        return [Node.from_payload(p) for p in payload["items"]]

    async def get_node(self, name: str) -> Node:
        payload = await self._request(
            method="get", url=self._config.url / "api/v1/nodes" / name
        )
        assert payload["kind"] == "Node"
        return Node.from_payload(payload)

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator, MutableMapping
from pathlib import Path
from typing import Any, cast

import aiohttp
import aiohttp.web
import pytest
from aiohttp.web import AppKey, Request
from yarl import URL

from platform_container_runtime.config import KubeClientAuthType, KubeConfig
from platform_container_runtime.kube_client import KubeClient

from .conftest import create_local_app_server

TOKEN_KEY: AppKey[str] = AppKey("token")


class TestKubeClientTokenUpdater:
    @pytest.fixture
    async def kube_app(self) -> aiohttp.web.Application:
        app = aiohttp.web.Application()
        app_kv: MutableMapping[AppKey[str], Any] = cast(
            MutableMapping[AppKey[str], Any], app
        )
        app_kv[TOKEN_KEY] = {"value": ""}

        async def _get_nodes(request: Request) -> aiohttp.web.Response:
            auth = request.headers["Authorization"]
            token = auth.split()[-1]
            # Retrieve and update the token state
            req_kv = cast(MutableMapping[AppKey[str], Any], request.app)
            state = cast(MutableMapping[str, str], req_kv[TOKEN_KEY])
            state["value"] = token
            return aiohttp.web.json_response({"kind": "NodeList", "items": []})

        app.router.add_routes(
            [
                aiohttp.web.get("/api/v1/nodes", _get_nodes),
            ]
        )
        return app

    @pytest.fixture
    async def kube_server(
        self, kube_app: aiohttp.web.Application, unused_tcp_port_factory: Any
    ) -> AsyncIterator[str]:
        async with create_local_app_server(
            kube_app, port=unused_tcp_port_factory()
        ) as address:
            yield f"http://{address.host}:{address.port}"

    @pytest.fixture
    def kube_token_path(self) -> Iterator[str]:
        _, path = tempfile.mkstemp()
        Path(path).write_text("token-1")
        yield path
        os.remove(path)

    @pytest.fixture
    async def kube_client(
        self, kube_server: str, kube_token_path: str
    ) -> AsyncIterator[KubeClient]:
        async with KubeClient(
            config=KubeConfig(
                url=URL(kube_server),
                auth_type=KubeClientAuthType.TOKEN,
                token_path=kube_token_path,
                token_update_interval_s=1,
            )
        ) as client:
            yield client

    async def test_token_periodically_updated(
        self,
        kube_app: aiohttp.web.Application,
        kube_client: KubeClient,
        kube_token_path: str,
    ) -> None:
        await kube_client.get_nodes()
        app_kv = cast(MutableMapping[AppKey[str], Any], kube_app)
        token_state = cast(MutableMapping[str, str], app_kv[TOKEN_KEY])
        assert token_state["value"] == "token-1"

        Path(kube_token_path).write_text("token-2")
        await asyncio.sleep(2)

        await kube_client.get_nodes()
        app_kv = cast(MutableMapping[AppKey[str], Any], kube_app)
        token_state = cast(MutableMapping[str, str], app_kv[TOKEN_KEY])
        assert token_state["value"] == "token-2"


class TestKubeClient:
    async def test_get_node(self, kube_client: KubeClient) -> None:
        nodes = await kube_client.get_nodes()

        assert nodes

        node = await kube_client.get_node(nodes[0].metadata.name)

        assert node.metadata.labels
        assert node.container_runtime_version
        assert node.os
        assert node.architecture

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import aiohttp
import aiohttp.web
import pytest
from yarl import URL

from platform_container_runtime.config import KubeClientAuthType, KubeConfig
from platform_container_runtime.kube_client import KubeClient

from .conftest import create_local_app_server


class TestKubeClientTokenUpdater:
    @pytest.fixture
    async def kube_app(self) -> aiohttp.web.Application:
        async def _get_nodes(request: aiohttp.web.Request) -> aiohttp.web.Response:
            auth = request.headers["Authorization"]
            token = auth.split()[-1]
            app["token"]["value"] = token
            return aiohttp.web.json_response({"kind": "NodeList", "items": []})

        app = aiohttp.web.Application()
        app["token"] = {"value": ""}
        app.router.add_routes([aiohttp.web.get("/api/v1/nodes", _get_nodes)])
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
        assert kube_app["token"]["value"] == "token-1"

        Path(kube_token_path).write_text("token-2")
        await asyncio.sleep(2)

        await kube_client.get_nodes()
        assert kube_app["token"]["value"] == "token-2"


class TestKubeClient:
    async def test_get_node(self, kube_client: KubeClient) -> None:
        nodes = await kube_client.get_nodes()

        assert nodes

        node = await kube_client.get_node(nodes[0].metadata.name)

        assert node.metadata.labels
        assert node.container_runtime_version
        assert node.os
        assert node.architecture

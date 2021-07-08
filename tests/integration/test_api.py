from dataclasses import dataclass
from typing import AsyncIterator

import aiohttp
import pytest
from aiohttp.web import HTTPOk

from platform_container_runtime.api import create_app
from platform_container_runtime.config import Config

from .conftest import ApiAddress, create_local_app_server


pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class ApiEndpoints:
    address: ApiAddress

    @property
    def api_v1_endpoint(self) -> str:
        return f"http://{self.address.host}:{self.address.port}/api/v1"

    @property
    def ping_url(self) -> str:
        return f"{self.api_v1_endpoint}/ping"


@pytest.fixture
async def api(config: Config) -> AsyncIterator[ApiEndpoints]:
    app = await create_app(config)
    async with create_local_app_server(app, port=8080) as address:
        yield ApiEndpoints(address=address)


class TestApi:
    async def test_ping(self, api: ApiEndpoints, client: aiohttp.ClientSession) -> None:
        async with client.get(api.ping_url) as resp:
            assert resp.status == HTTPOk.status_code
            text = await resp.text()
            assert text == "Pong"

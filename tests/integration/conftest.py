import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import aiohttp
import aiohttp.web
import pytest
from yarl import URL

from platform_container_runtime.config import Config, KubeConfig, ServerConfig

from .conftest_kube import get_service_cluster_ip, get_service_url


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiAddress:
    host: str
    port: int


@pytest.fixture
async def client() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def cri_address() -> str:
    url = URL(await get_service_url("cri"))
    return f"{url.host}:{url.port}"


@pytest.fixture
async def runtime_address() -> str:
    return await get_service_url("runtime")


@pytest.fixture
def config_factory(
    cri_address: str, runtime_address: str, kube_config: KubeConfig, kube_node: str
) -> Callable[..., Config]:
    def _f(**kwargs: Any) -> Config:
        defaults = dict(
            server=ServerConfig(host="0.0.0.0", port=8080),
            node_name=kube_node,
            cri_address=cri_address,
            runtime_address=runtime_address,
            kube=kube_config,
        )
        kwargs = {**defaults, **kwargs}
        return Config(**kwargs)

    return _f


@pytest.fixture
def config(
    config_factory: Callable[..., Config],
) -> Config:
    return config_factory()


@asynccontextmanager
async def create_local_app_server(
    app: aiohttp.web.Application, port: int = 8080
) -> AsyncIterator[ApiAddress]:
    runner = aiohttp.web.AppRunner(app)
    try:
        await runner.setup()
        api_address = ApiAddress("0.0.0.0", port)
        site = aiohttp.web.TCPSite(runner, api_address.host, api_address.port)
        await site.start()
        yield api_address
    finally:
        await runner.shutdown()
        await runner.cleanup()


@pytest.fixture
async def registry_address() -> str:
    return await get_service_cluster_ip(
        service_name="registry", namespace="kube-system"
    )

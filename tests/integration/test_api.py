import asyncio
import re
from dataclasses import dataclass
from typing import AsyncIterator

import aiohttp
import async_timeout
import pytest
from aiohttp.web import HTTPBadRequest, HTTPNoContent, HTTPNotFound, HTTPOk
from yarl import URL

from platform_container_runtime.api import create_app
from platform_container_runtime.config import Config

from .conftest import create_local_app_server
from .conftest_kube import get_pod, get_service_url, run


pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class ApiEndpoints:
    url: URL

    @property
    def api_v1_endpoint(self) -> URL:
        return self.url / "api/v1"

    @property
    def ping_url(self) -> URL:
        return self.api_v1_endpoint / "ping"

    @property
    def containers_endpoint(self) -> URL:
        return self.api_v1_endpoint / "containers"

    def attach(self, container_id: str) -> URL:
        return self.containers_endpoint / self._encode(container_id) / "attach"

    def exec(self, container_id: str) -> URL:
        return self.containers_endpoint / self._encode(container_id) / "exec"

    def kill(self, container_id: str) -> URL:
        return self.containers_endpoint / self._encode(container_id) / "kill"

    def _encode(self, value: str) -> str:
        return value.replace("/", "%2F")


@pytest.fixture
async def api(config: Config) -> AsyncIterator[ApiEndpoints]:
    app = await create_app(config)
    async with create_local_app_server(app, port=8080) as address:
        yield ApiEndpoints(
            url=URL.build(scheme="http", host=address.host, port=address.port)
        )


@pytest.fixture
async def api_minikube() -> ApiEndpoints:
    url = await get_service_url("platform-container-runtime")
    return ApiEndpoints(URL(url))


async def receive_tty_prompt(
    ws: aiohttp.ClientWebSocketResponse, timeout: float = 5
) -> bytes:
    ansi_re = re.compile(br"\033\[[;?0-9]*[a-zA-Z]")

    try:
        ret: bytes = b""

        async with async_timeout.timeout(timeout):
            while not ret.strip().endswith(b"#"):
                data = await ws.receive_bytes()

                print(data)

                assert data[0] == 1  # stdout channel

                ret += ansi_re.sub(b"", data[1:])
            return ret
    except asyncio.TimeoutError:
        raise AssertionError(f"[Timeout] {ret!r}")


async def receive_tty_bytes(
    ws: aiohttp.ClientWebSocketResponse, timeout: float = 5
) -> bytes:
    ansi_re = re.compile(br"\033\[[;?0-9]*[a-zA-Z]")
    data = await ws.receive_bytes(timeout=timeout)
    return ansi_re.sub(b"", data)


class TestApi:
    async def test_ping(self, api: ApiEndpoints, client: aiohttp.ClientSession) -> None:
        async with client.get(api.ping_url) as resp:
            assert resp.status == HTTPOk.status_code
            text = await resp.text()
            assert text == "Pong"

    @pytest.mark.minikube
    async def test_attach_non_tty(
        self, api_minikube: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run(
            "ubuntu:20.10",
            (
                'bash -c "'
                "sleep 5;"
                "echo 'hello stdout';"
                "echo 'hello stderr' >&2;"
                "sleep infinity"
                '"'
            ),
        ) as pod:
            async with client.ws_connect(api_minikube.attach(pod.container_id)) as ws:
                stdout_received = False
                stderr_received = False

                while True:
                    data = await ws.receive_bytes(timeout=10)

                    if len(data) == 1:
                        # empty message is sent to the lowest output channel
                        # after connection is established
                        assert data[0] == 1
                        continue

                    if data[0] == 1:  # stdout channel
                        stdout_received = True
                        assert data[1:].decode() == "hello stdout\n"

                    if data[0] == 2:  # stdout channel
                        stderr_received = True
                        assert data[1:].decode() == "hello stderr\n"

                    if stdout_received and stderr_received:
                        break

    @pytest.mark.minikube
    async def test_attach_tty(
        self, api_minikube: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run("ubuntu:20.10", "bash", stdin=True, tty=True) as pod:
            async with client.ws_connect(
                api_minikube.attach(pod.container_id).with_query(
                    stdin="true", tty="true", stdout="true", stderr="false"
                )
            ) as ws:
                expected_output = b"echo hello\r\nhello\r\n"
                output = b""

                await ws.send_bytes(b'\x04{"Width":100,"Height":100}')
                await ws.send_bytes(b"\n")
                await receive_tty_prompt(ws)

                await ws.send_bytes(b"\x00echo hello\n")

                while True:
                    data = await receive_tty_bytes(ws)
                    output += data[1:]

                    print(data)

                    if expected_output in output:
                        break

    async def test_attach_unknown(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with client.post(api.attach("unknown")) as resp:
            assert resp.status == HTTPNotFound.status_code, await resp.text()

    async def test_attach_tty_and_stderr(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with client.post(
            api.attach("unknown").with_query(tty="true", stderr="true")
        ) as resp:
            assert resp.status == HTTPBadRequest.status_code, await resp.text()

    async def test_attach_no_stdin_stdout_stderr(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with client.post(
            api.attach("unknown").with_query(
                stdin="false", stdout="false", stderr="false"
            )
        ) as resp:
            assert resp.status == HTTPBadRequest.status_code, await resp.text()

    @pytest.mark.minikube
    async def test_exec_non_tty(
        self, api_minikube: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run("ubuntu:20.10", 'bash -c "sleep infinity"') as pod:
            async with client.ws_connect(
                api_minikube.exec(pod.container_id).with_query(cmd="bash", stdin="true")
            ) as ws:
                await ws.send_bytes(
                    b"\x00echo 'hello stdout'\necho 'hello stderr' >&2\n"
                )

                stdout_received = False
                stderr_received = False

                while True:
                    data = await ws.receive_bytes(timeout=5)

                    if len(data) == 1:
                        # empty message is sent to the lowest output channel
                        # after connection is established
                        assert data[0] == 1
                        continue

                    if data[0] == 1:  # stdout channel
                        stdout_received = True
                        assert data[1:].decode() == "hello stdout\n"

                    if data[0] == 2:  # stdout channel
                        stderr_received = True
                        assert data[1:].decode() == "hello stderr\n"

                    if stdout_received and stderr_received:
                        break

    @pytest.mark.minikube
    async def test_exec_tty(
        self, api_minikube: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run("ubuntu:20.10", 'bash -c "sleep infinity"') as pod:
            async with client.ws_connect(
                api_minikube.exec(pod.container_id).with_query(
                    cmd="bash", stdin="true", tty="true", stdout="true", stderr="false"
                )
            ) as ws:
                expected_output = b"echo hello\r\nhello\r\n"
                output = b""

                await ws.send_bytes(b'\x04{"Width":100,"Height":100}')
                await ws.send_bytes(b"\n")
                await receive_tty_prompt(ws)

                await ws.send_bytes(b"\x00echo hello\n")

                while True:
                    data = await receive_tty_bytes(ws)
                    output += data[1:]

                    print(data)

                    if expected_output in output:
                        break

    async def test_exec_unknown(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with client.post(api.exec("unknown").with_query(cmd="bash")) as resp:
            assert resp.status == HTTPNotFound.status_code, await resp.text()

    async def test_exec_no_cmd(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with client.post(api.exec("unknown")) as resp:
            assert resp.status == HTTPBadRequest.status_code, await resp.text()

    async def test_exec_tty_and_stderr(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with client.post(
            api.exec("unknown").with_query(cmd="bash", tty="true", stderr="true")
        ) as resp:
            assert resp.status == HTTPBadRequest.status_code, await resp.text()

    async def test_exec_no_stdin_stdout_stderr(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with client.post(
            api.exec("unknown").with_query(
                cmd="bash", stdin="false", stdout="false", stderr="false"
            )
        ) as resp:
            assert resp.status == HTTPBadRequest.status_code, await resp.text()

    async def test_kill(self, api: ApiEndpoints, client: aiohttp.ClientSession) -> None:
        async with run("ubuntu:20.10", 'bash -c "sleep infinity"') as pod:
            pod_payload = await get_pod(pod.name)

            assert all(
                "terminated" not in s["lastState"] and "terminated" not in s["state"]
                for s in pod_payload["status"]["containerStatuses"]
            )

            async with client.post(api.kill(pod.container_id)) as resp:
                assert resp.status == HTTPNoContent.status_code, await resp.text()

            await asyncio.sleep(1)

            pod_payload = await get_pod(pod.name)

            assert all(
                "terminated" in s["lastState"] or "terminated" in s["state"]
                for s in pod_payload["status"]["containerStatuses"]
            )

    async def test_kill_unknown(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with client.post(api.kill("unknown")) as resp:
            assert resp.status == HTTPNotFound.status_code, await resp.text()

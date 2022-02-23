import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp
import async_timeout
import pytest
from aiohttp.web import HTTPBadRequest, HTTPNoContent, HTTPNotFound, HTTPOk
from yarl import URL

from platform_container_runtime.api import create_app
from platform_container_runtime.config import Config

from .conftest import create_local_app_server
from .conftest_kube import get_pod, get_service_url, run


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

    def commit(self, container_id: str) -> URL:
        return self.containers_endpoint / self._encode(container_id) / "commit"

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
    ansi_re = re.compile(rb"\033\[[;?0-9]*[a-zA-Z]")

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
    ansi_re = re.compile(rb"\033\[[;?0-9]*[a-zA-Z]")
    data = await ws.receive_bytes(timeout=timeout)
    return ansi_re.sub(b"", data)


class TestApi:
    async def test_ping(self, api: ApiEndpoints, client: aiohttp.ClientSession) -> None:
        async with client.get(api.ping_url) as resp:
            assert resp.status == HTTPOk.status_code
            text = await resp.text()
            assert text == "Pong"

    @pytest.mark.minikube
    async def test_attach_non_tty_stdout(
        self, api_minikube: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run(
            "ubuntu:20.10",
            'bash -c "while true; do echo hello; sleep 1; done"',
        ) as pod:
            async with client.ws_connect(api_minikube.attach(pod.container_id)) as ws:
                stdout_received = False

                while True:
                    data = await ws.receive_bytes(timeout=10)

                    if len(data) == 1:
                        # empty message is sent to the lowest output channel
                        # after connection is established
                        assert data[0] == 1
                        continue

                    if data[0] == 1:  # stdout channel
                        stdout_received = True
                        assert data[1:].decode() == "hello\n"

                    if stdout_received:
                        break

    @pytest.mark.minikube
    async def test_attach_non_tty_stderr(
        self, api_minikube: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run(
            "ubuntu:20.10",
            'bash -c "while true; do echo hello >&2; sleep 1; done"',
        ) as pod:
            async with client.ws_connect(api_minikube.attach(pod.container_id)) as ws:
                stderr_received = False

                while True:
                    data = await ws.receive_bytes(timeout=10)

                    if len(data) == 1:
                        # empty message is sent to the lowest output channel
                        # after connection is established
                        assert data[0] == 1
                        continue

                    if data[0] == 2:  # stderr channel
                        stderr_received = True
                        assert data[1:].decode() == "hello\n"

                    if stderr_received:
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
                await ws.send_bytes(b"\x00\n")
                await receive_tty_prompt(ws)

                await ws.send_bytes(b"\x00echo hello\n")

                while True:
                    data = await receive_tty_bytes(ws)
                    output += data[1:]

                    print(data)

                    if expected_output in output:
                        break

    @pytest.mark.minikube
    async def test_attach_terminated_container(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run(
            "ubuntu:20.10",
            'bash -c "exit 1"',
            expected_status="terminated",
        ) as pod:
            async with client.post(api.attach(pod.container_id)) as resp:
                assert (
                    resp.status == HTTPBadRequest.status_code
                    or resp.status == HTTPNotFound.status_code
                ), await resp.text()

                if resp.status == HTTPBadRequest.status_code:
                    assert "not running" in await resp.text()

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

                    if data[0] == 2:  # stderr channel
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
                await ws.send_bytes(b"\x00\n")
                await receive_tty_prompt(ws)

                await ws.send_bytes(b"\x00echo hello\n")

                while True:
                    data = await receive_tty_bytes(ws)
                    output += data[1:]

                    print(data)

                    if expected_output in output:
                        break

    @pytest.mark.minikube
    async def test_exec_tty_exit_code(
        self, api_minikube: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run("ubuntu:20.10", 'bash -c "sleep infinity"') as pod:
            async with client.ws_connect(
                api_minikube.exec(pod.container_id).with_query(
                    cmd="bash", stdin="true", tty="true", stdout="true", stderr="false"
                )
            ) as ws:
                await ws.send_bytes(b"\x00\n")
                await receive_tty_prompt(ws)

                await ws.send_bytes(b"\x00exit 42\n")

                while True:
                    msg = await ws.receive(timeout=5)

                    if msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        break

                    data = msg.data

                    print(data)

                    if data[0] == 3:
                        payload = json.loads(data[1:])
                        assert payload["exit_code"] == 42

    @pytest.mark.minikube
    async def test_exec_terminated_container(
        self, api: ApiEndpoints, client: aiohttp.ClientSession
    ) -> None:
        async with run(
            "ubuntu:20.10",
            'bash -c "exit 1"',
            expected_status="terminated",
        ) as pod:
            async with client.post(
                api.exec(pod.container_id).with_query(cmd="bash")
            ) as resp:
                assert (
                    resp.status == HTTPBadRequest.status_code
                    or resp.status == HTTPNotFound.status_code
                ), await resp.text()

                if resp.status == HTTPBadRequest.status_code:
                    assert "not running" in await resp.text()

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

    @pytest.mark.minikube
    async def test_commit(
        self,
        api_minikube: ApiEndpoints,
        client: aiohttp.ClientSession,
        registry_address: str,
        kube_container_runtime: str,
    ) -> None:
        if kube_container_runtime == "cri-o":
            pytest.skip("Commit is not supported")

        async with run("ubuntu:20.10", 'bash -c "sleep infinity"') as pod:
            repository = f"{registry_address}/ubuntu"
            tag = str(uuid.uuid4())
            image = f"{repository}:{tag}"

            async with client.post(
                api_minikube.commit(pod.container_id),
                json={"image": image, "push": True},
            ) as resp:
                assert resp.status == HTTPOk.status_code, str(resp)
                chunks = [
                    json.loads(chunk.decode("utf-8"))
                    async for chunk in resp.content
                    if chunk
                ]
                debug = f"Received chunks: `{chunks}`"
                assert isinstance(chunks, list), debug
                assert all(isinstance(s, dict) for s in chunks), debug
                assert len(chunks) >= 4, debug  # 2 for commit(), >=2 for push()

                # here we rely on chunks to be received in correct order

                assert chunks[0]["status"] == "CommitStarted", debug
                assert chunks[0]["details"]["image"] == image, debug
                assert re.match(r"\w{64}", chunks[0]["details"]["container"]), debug

                assert chunks[1] == {"status": "CommitFinished"}, debug

                msg = f"The push refers to repository [{repository}]"
                assert chunks[2].get("status") == msg, debug

                assert chunks[-1].get("aux", {}).get("Tag") == tag, debug

        async with run(image, 'bash -c "sleep infinity"') as pod:
            pass

    @pytest.mark.minikube
    async def test_commit_without_push(
        self,
        api_minikube: ApiEndpoints,
        client: aiohttp.ClientSession,
        registry_address: str,
        kube_container_runtime: str,
    ) -> None:
        if kube_container_runtime == "cri-o":
            pytest.skip("Commit is not supported")

        async with run("ubuntu:20.10", 'bash -c "sleep infinity"') as pod:
            repository = f"{registry_address}/ubuntu"
            tag = str(uuid.uuid4())
            image = f"{repository}:{tag}"

            async with client.post(
                api_minikube.commit(pod.container_id),
                json={"image": image},
            ) as resp:
                assert resp.status == HTTPOk.status_code, str(resp)
                chunks = [
                    json.loads(chunk.decode("utf-8"))
                    async for chunk in resp.content
                    if chunk
                ]
                debug = f"Received chunks: `{chunks}`"
                assert isinstance(chunks, list), debug
                assert all(isinstance(s, dict) for s in chunks), debug
                assert len(chunks) >= 2, debug

                # here we rely on chunks to be received in correct order

                assert chunks[0]["status"] == "CommitStarted", debug
                assert chunks[0]["details"]["image"] == image, debug
                assert re.match(r"\w{64}", chunks[0]["details"]["container"]), debug

                assert chunks[1] == {"status": "CommitFinished"}, debug

        async with run(image, 'bash -c "sleep infinity"', image_pull_policy="Never"):
            pass

    @pytest.mark.minikube
    async def test_commit_invalid_image(
        self,
        api_minikube: ApiEndpoints,
        client: aiohttp.ClientSession,
        registry_address: str,
        kube_container_runtime: str,
    ) -> None:
        if kube_container_runtime == "cri-o":
            pytest.skip("Commit is not supported")

        async with client.post(
            api_minikube.commit("unknown"),
            json={"image": f"_{registry_address}/ubuntu:latest"},
        ) as resp:
            assert resp.status == HTTPBadRequest.status_code, await resp.text()

    @pytest.mark.minikube
    async def test_commit_unknown_container(
        self,
        api_minikube: ApiEndpoints,
        client: aiohttp.ClientSession,
        registry_address: str,
        kube_container_runtime: str,
    ) -> None:
        if kube_container_runtime == "cri-o":
            pytest.skip("Commit is not supported")

        async with client.post(
            api_minikube.commit("unknown"),
            json={"image": f"{registry_address}/ubuntu:latest"},
        ) as resp:
            assert resp.status == HTTPNotFound.status_code, await resp.text()

    @pytest.mark.minikube
    async def test_commit_unknown_registry(
        self,
        api_minikube: ApiEndpoints,
        client: aiohttp.ClientSession,
        kube_container_runtime: str,
    ) -> None:
        if kube_container_runtime == "cri-o":
            pytest.skip("Commit is not supported")

        domain = str(uuid.uuid4())

        async with run("ubuntu:20.10", 'bash -c "sleep infinity"') as pod:
            async with client.post(
                api_minikube.commit(pod.container_id),
                json={"image": f"{domain}:5000/ubuntu:latest", "push": True},
            ) as resp:
                assert resp.status == HTTPOk.status_code, str(resp)
                chunks = [
                    json.loads(chunk.decode("utf-8"))
                    async for chunk in resp.content
                    if chunk
                ]
                debug = f"Received chunks: `{chunks}`"
                assert isinstance(chunks, list), debug
                assert all(isinstance(s, dict) for s in chunks), debug
                assert len(chunks) == 4, debug  # 2 for commit(), 2 for push()

                # here we rely on chunks to be received in correct order

                assert chunks[0]["status"] == "CommitStarted", debug
                assert chunks[1] == {"status": "CommitFinished"}, debug

                msg = f"The push refers to repository [{domain}:5000/ubuntu]"
                assert chunks[2].get("status") == msg, debug

                error = chunks[3]["error"]
                assert (
                    "no such host" in error
                    or "failure in name resolution" in error
                    or "Name or service not known" in error
                )

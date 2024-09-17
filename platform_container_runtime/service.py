import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from typing import Any, Optional, Union

import aiohttp
import aiohttp.web
from yarl import URL

from .cri_client import ContainerState, CriClient
from .errors import ContainerNotRunningError
from .runtime_client import RuntimeClient
from .utils import asyncgeneratorcontextmanager

logger = logging.getLogger()


STREAM_PROTOCOLS = ("v4.channel.k8s.io", "channel.k8s.io")


class Stream:
    def __init__(
        self,
        client: aiohttp.ClientSession,
        url: URL,
        protocols: Iterable[str] = STREAM_PROTOCOLS,
    ) -> None:
        self._client = client
        self._url = url
        self._protocols = protocols
        self._closing = False

    async def copy(self, resp: aiohttp.web.WebSocketResponse) -> None:
        tasks = []

        async with self._client.ws_connect(self._url, protocols=self._protocols) as ws:
            try:
                tasks.append(asyncio.create_task(self._proxy_ws(resp, ws)))
                tasks.append(asyncio.create_task(self._proxy_ws(ws, resp)))

                if tasks:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                await resp.close()

                for task in tasks:
                    if task.done():
                        continue

                    task.cancel()

                    with suppress(asyncio.CancelledError):
                        await task

    async def _proxy_ws(
        self,
        src: Union[aiohttp.ClientWebSocketResponse, aiohttp.web.WebSocketResponse],
        dst: Union[aiohttp.ClientWebSocketResponse, aiohttp.web.WebSocketResponse],
    ) -> None:
        try:
            async for msg in src:
                if self._closing:
                    break

                if msg.type == aiohttp.WSMsgType.BINARY:
                    data = msg.data

                    if data[0] == 3:
                        data = (
                            b"\x03"
                            + json.dumps(self.parse_error_channel(data)).encode()
                        )

                    await dst.send_bytes(data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    exc = src.exception()
                    logger.error(
                        "WS connection closed with exception %s", exc, exc_info=exc
                    )
                else:
                    raise ValueError(f"Unsupported WS message type {msg.type}")
        except StopAsyncIteration:
            pass
        finally:
            self._closing = True

    @classmethod
    def parse_error_channel(cls, channel: bytes) -> dict[str, Any]:
        assert channel[0] == 3, "Non error channel received"

        data = channel[1:]

        try:
            channel_payload = json.loads(data)
        except Exception:
            return {"exit_code": 1, "message": data.decode()}
        else:
            if channel_payload.get("status", "success").lower() == "success":
                return {"exit_code": 0}
            else:
                exit_code = 1

                for cause in channel_payload.get("details", {}).get("causes", ()):
                    if cause.get("reason") == "ExitCode":
                        exit_code = int(cause.get("message", 1))

                message = channel_payload.get("message")

                result = {"exit_code": exit_code}

                if message:
                    result["message"] = message

                return result


class Service:
    def __init__(
        self,
        cri_client: CriClient,
        runtime_client: RuntimeClient,
        streaming_client: aiohttp.ClientSession,
    ) -> None:
        self._cri_client = cri_client
        self._runtime_client = runtime_client
        self._streaming_client = streaming_client

    async def attach(
        self,
        container_id: str,
        *,
        stdin: bool = False,
        stdout: bool = False,
        stderr: bool = False,
        tty: bool = False,
    ) -> Stream:
        # For some reason cri-o always returns attach url
        # event if container does not exist. get_status should raise
        # ContainerNotFoundError if container does not exist.
        status = await self._cri_client.get_status(container_id)
        if status.state != ContainerState.RUNNING:
            raise ContainerNotRunningError(container_id)
        url = await self._cri_client.attach(
            container_id=container_id,
            tty=tty,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        return Stream(self._streaming_client, url=url)

    async def exec(
        self,
        container_id: str,
        cmd: str,
        *,
        tty: bool = False,
        stdin: bool = False,
        stdout: bool = False,
        stderr: bool = False,
    ) -> Stream:
        # For some reason cri-o always returns exec url
        # event if container does not exist. get_status should raise
        # ContainerNotFoundError if container does not exist.
        status = await self._cri_client.get_status(container_id)
        if status.state != ContainerState.RUNNING:
            raise ContainerNotRunningError(container_id)
        url = await self._cri_client.exec(
            container_id=container_id,
            cmd=cmd,
            tty=tty,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        return Stream(self._streaming_client, url=url)

    async def kill(self, container_id: str, timeout_s: int = 0) -> None:
        # NOTE: For Containerd the StopContainer RPC is idempotent, and doesn't
        # raise an error if the container has already been stopped.
        # For Docker it raises an error.
        await self._cri_client.stop_container(
            container_id=container_id,
            timeout_s=timeout_s,
        )

    @asyncgeneratorcontextmanager
    async def commit(
        self, container_id: str, image: str
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._runtime_client.commit(
            container_id=container_id, image=image
        ) as commit:
            async for chunk in commit:
                yield chunk

    @asyncgeneratorcontextmanager
    async def push(
        self, image: str, auth: Optional[dict[str, Any]] = None
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._runtime_client.push(image, auth) as push:
            async for chunk in push:
                yield chunk

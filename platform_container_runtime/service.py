import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Dict, Iterable, Union

import aiohttp
import aiohttp.web
from yarl import URL

from .cri import RuntimeService


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
    def parse_error_channel(cls, channel: bytes) -> Dict[str, Any]:
        assert channel[0] == 3, "Non error channel received"

        data = channel[1:]

        try:
            channel_payload = json.loads(data)
        except Exception:
            return {"exit_code": 1, "message": data.decode()}
        else:
            if channel_payload["status"].lower() == "success":
                return {"exit_code": 0}
            else:
                exit_code = 1

                for cause in channel_payload.get("details", {}).get("causes", ()):
                    if cause.get("reason") == "ExitCode":
                        exit_code = int(cause.get("message", 1))

                return {
                    "exit_code": exit_code,
                    "message": channel_payload["message"],
                }


class Service:
    def __init__(
        self,
        runtime_service: RuntimeService,
        streaming_client: aiohttp.ClientSession,
    ) -> None:
        self._runtime_service = runtime_service
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
        url = await self._runtime_service.attach(
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
        url = await self._runtime_service.exec(
            container_id=container_id,
            cmd=cmd,
            tty=tty,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        return Stream(self._streaming_client, url=url)

    async def kill(self, container_id: str, timeout_s: int = 0) -> None:
        await self._runtime_service.stop_container(
            container_id=container_id,
            timeout_s=timeout_s,
        )

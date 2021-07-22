import asyncio
import logging
from contextlib import suppress

import aiohttp
import aiohttp.web
from yarl import URL

from .cri import RuntimeService


logger = logging.getLogger()


class Stream:
    def __init__(
        self,
        client: aiohttp.ClientSession,
        url: URL,
        *,
        handle_input: bool = False,
        handle_output: bool = False,
    ) -> None:
        self._client = client
        self._url = url
        self._handle_input = handle_input
        self._handle_output = handle_output
        self._closing = False

    async def copy(self, resp: aiohttp.web.WebSocketResponse) -> None:
        tasks = []

        async with self._client.ws_connect(self._url) as ws:
            try:
                if self._handle_input:
                    tasks.append(asyncio.create_task(self._do_input(ws, resp)))
                if self._handle_output:
                    tasks.append(asyncio.create_task(self._do_output(ws, resp)))

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

    async def _do_input(
        self, ws: aiohttp.ClientWebSocketResponse, resp: aiohttp.web.WebSocketResponse
    ) -> None:
        try:
            async for msg in resp:
                if self._closing:
                    break

                if msg.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    exc = resp.exception()
                    logger.error(
                        "WS connection closed with exception %s", exc, exc_info=exc
                    )
                else:
                    raise ValueError(f"Unsupported WS message type {msg.type}")
        except StopAsyncIteration:
            self._closing = True

    async def _do_output(
        self, ws: aiohttp.ClientWebSocketResponse, resp: aiohttp.web.WebSocketResponse
    ) -> None:
        try:
            async for msg in ws:
                if self._closing:
                    break

                if msg.type == aiohttp.WSMsgType.BINARY:
                    await resp.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    exc = ws.exception()
                    logger.error(
                        "WS connection closed with exception %s", exc, exc_info=exc
                    )
                else:
                    raise ValueError(f"Unsupported WS message type {msg.type}")
        except StopAsyncIteration:
            self._closing = True


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
        return Stream(
            self._streaming_client,
            url,
            handle_input=stdin,
            handle_output=stdout or stderr,
        )

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
        return Stream(
            self._streaming_client,
            url,
            handle_input=stdin,
            handle_output=stdout or stderr,
        )

    async def kill(self, container_id: str, timeout_s: int = 0) -> None:
        await self._runtime_service.stop_container(
            container_id=container_id,
            timeout_s=timeout_s,
        )

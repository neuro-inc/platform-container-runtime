from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, MutableMapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, cast

import aiohttp
import aiohttp.web
import grpc.aio
from aiodocker import Docker
from aiohttp.typedefs import Handler
from aiohttp.web import (
    AppKey,
    HTTPBadRequest,
    HTTPInternalServerError,
    HTTPNoContent,
    HTTPNotFound,
    Request,
    Response,
    StreamResponse,
    json_response,
)
from aiohttp.web_middlewares import middleware
from neuro_logging import init_logging, notrace, setup_sentry
from yarl import URL

from platform_container_runtime import __version__

from .config import Config
from .config_factory import EnvironConfigFactory
from .containerd_client import ContainerdClient
from .cri_client import CriClient
from .errors import ContainerNotFoundError
from .kube_client import KubeClient
from .registry_client import RegistryClient
from .runtime_client import RuntimeClient
from .service import Service

logger = logging.getLogger(__name__)

PLATFORM_CONTAINER_RUNTIME_APP_KEY: AppKey[str] = AppKey(
    "platform_container_runtime_app"
)
SERVICE_KEY: AppKey[str] = AppKey("service")
CONFIG_KEY: AppKey[str] = AppKey("config")
API_V1_APP_KEY: AppKey[str] = AppKey("api_v1_app")


class ApiHandler:
    def register(self, app: aiohttp.web.Application) -> None:
        app.add_routes(
            [
                aiohttp.web.get("/ping", self.handle_ping),
            ]
        )

    @notrace
    async def handle_ping(self, req: Request) -> Response:
        return Response(text="Pong")


class PlatformContainerRuntimeApiHandler:
    def __init__(self, app: aiohttp.web.Application, config: Config) -> None:
        self._app = app
        self._config = config

    def register(self, app: aiohttp.web.Application) -> None:
        app.add_routes(
            [
                aiohttp.web.get("/{id}/attach", self.ws_attach),
                aiohttp.web.post("/{id}/attach", self.ws_attach),
                aiohttp.web.get("/{id}/exec", self.ws_exec),
                aiohttp.web.post("/{id}/exec", self.ws_exec),
                aiohttp.web.post("/{id}/kill", self.kill),
                aiohttp.web.post("/{id}/commit", self.commit),
            ]
        )

    @property
    def _service(self) -> Service:
        return cast(Service, self._app[SERVICE_KEY])

    async def ws_attach(self, req: Request) -> StreamResponse:
        container_id = self._get_container_id(req)
        tty = _parse_bool(req.query.get("tty", "false"))
        stdin = _parse_bool(req.query.get("stdin", "false"))
        stdout = _parse_bool(req.query.get("stdout", "true"))
        stderr = _parse_bool(req.query.get("stderr", "true"))

        if not (stdin or stdout or stderr):
            raise ValueError("Required at least one of stdin, stdout or stderr")

        if tty and stderr:
            raise ValueError("Stdout and stderr cannot be multiplexed in tty mode")

        stream = await self._service.attach(
            container_id,
            tty=tty,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        resp = aiohttp.web.WebSocketResponse()
        await resp.prepare(req)
        await stream.copy(resp)

        return resp

    async def ws_exec(self, req: Request) -> StreamResponse:
        container_id = self._get_container_id(req)
        cmd = req.query.get("cmd")
        tty = _parse_bool(req.query.get("tty", "false"))
        stdin = _parse_bool(req.query.get("stdin", "false"))
        stdout = _parse_bool(req.query.get("stdout", "true"))
        stderr = _parse_bool(req.query.get("stderr", "true"))

        if not cmd:
            raise ValueError("Command is required")

        if not (stdin or stdout or stderr):
            raise ValueError("Required at least one of stdin, stdout or stderr")

        if tty and stderr:
            raise ValueError("Stdout and stderr cannot be multiplexed in tty mode")

        stream = await self._service.exec(
            container_id,
            cmd=cmd,
            tty=tty,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        resp = aiohttp.web.WebSocketResponse()
        await resp.prepare(req)
        await stream.copy(resp)

        return resp

    async def kill(self, req: Request) -> StreamResponse:
        container_id = self._get_container_id(req)
        timeout_s = int(req.query.get("timeout_s", "0"))

        await self._service.kill(container_id, timeout_s)

        return HTTPNoContent()

    async def commit(self, req: Request) -> StreamResponse:
        container_id = self._get_container_id(req)
        payload = await req.json()
        image = payload.get("image")
        auth = payload.get("auth")
        push = payload.get("push", False)

        if not image:
            raise ValueError("Image is required")

        if auth and (not auth.get("username") or not auth.get("password")):
            raise ValueError("Invalid auth config")

        encoding = "utf-8"
        response = None

        try:
            async with self._service.commit(
                container_id=container_id, image=image
            ) as commit:
                async for chunk in commit:
                    if response is None:
                        response = await self._prepare_ndjson_response(req, encoding)
                    await response.write(_serialize_chunk(chunk, encoding))

            assert response is not None, "Commit failed"

            if not push:
                return response

            async with self._service.push(image, auth) as push:
                async for chunk in push:
                    await response.write(_serialize_chunk(chunk, encoding))

            return response
        except asyncio.CancelledError:
            raise
        except ContainerNotFoundError:
            raise
        except Exception as ex:
            if response is None:
                raise
            # middleware don't work for prepared StreamResponse, so we need to
            # catch a general exception and send it as a chunk
            msg_str = f"Unexpected error: {ex}"
            logging.exception(msg_str)
            chunk = {"error": msg_str}
            await response.write(_serialize_chunk(chunk, encoding))
            return response

    async def _prepare_ndjson_response(
        self, req: Request, encoding: str = "utf-8"
    ) -> StreamResponse:
        # Following docker engine API, the response should conform ndjson
        # see https://github.com/ndjson/ndjson-spec
        encoding = "utf-8"
        response = StreamResponse(status=200)
        response.enable_compression(aiohttp.web.ContentCoding.identity)
        response.content_type = "application/x-ndjson"
        response.charset = encoding
        await response.prepare(req)
        return response

    def _get_container_id(self, req: Request) -> str:
        return _strip_scheme(req.match_info["id"].replace("%2F", "/"))


def _strip_scheme(value: str) -> str:
    start = value.find("://")
    if start > 0:
        start += 3
    return value[start:] if start > 0 else value


def _parse_bool(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


def _serialize_chunk(chunk: dict[str, Any], encoding: str = "utf-8") -> bytes:
    chunk_str = json.dumps(chunk) + "\r\n"
    return chunk_str.encode(encoding)


@middleware
async def handle_exceptions(req: Request, handler: Handler) -> StreamResponse:
    try:
        return await handler(req)
    except ContainerNotFoundError as e:
        payload = {"error": str(e)}
        return json_response(payload, status=HTTPNotFound.status_code)
    except ValueError as e:
        payload = {"error": str(e)}
        return json_response(payload, status=HTTPBadRequest.status_code)
    except aiohttp.web.HTTPException:
        raise
    except Exception as e:
        msg_str = f"Unexpected exception: {str(e)}. Path with query: {req.path_qs}."
        logging.exception(msg_str)
        payload = {"error": msg_str}
        return json_response(payload, status=HTTPInternalServerError.status_code)


@asynccontextmanager
async def create_cri_client(
    config: Config, container_runtime_version: str
) -> AsyncIterator[CriClient]:
    if config.cri_address:
        cri_address = config.cri_address
    elif container_runtime_version.startswith("docker://"):
        cri_address = "unix:/hrun/dockershim.sock"
    elif container_runtime_version.startswith("containerd://"):
        cri_address = "unix:/hrun/containerd/containerd.sock"
    elif container_runtime_version.startswith("cri-o://"):
        cri_address = "unix:/hrun/crio/crio.sock"
    else:
        raise ValueError(
            f"Container runtime {container_runtime_version!r} is not supported"
        )

    logger.info("CRI address: %s", cri_address)
    logger.info("Initializing CRI client")

    async with grpc.aio.insecure_channel(cri_address) as channel:
        async with CriClient(channel) as client:
            yield client


@asynccontextmanager
async def create_runtime_client(
    config: Config,
    os: str,
    architecture: str,
    container_runtime_version: str,
) -> AsyncIterator[RuntimeClient]:
    logger.info("Initializing runtime client")

    if container_runtime_version.startswith("docker://"):
        async with Docker(
            config.runtime_address or "unix:///hrun/docker.sock"
        ) as docker:
            yield RuntimeClient(docker_client=docker)
    elif container_runtime_version.startswith("containerd://"):
        if config.runtime_address:
            url = URL(config.runtime_address)
            runtime_address = f"{url.host}:{url.port}"
        else:
            runtime_address = "unix:/hrun/containerd/containerd.sock"
        async with grpc.aio.insecure_channel(runtime_address) as channel:
            async with aiohttp.ClientSession() as session:
                yield RuntimeClient(
                    containerd_client=ContainerdClient(
                        channel,
                        registry_client=RegistryClient(session),
                        os=os,
                        architecture=architecture,
                    )
                )
    else:
        yield RuntimeClient()


async def create_api_v1_app() -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    handler = ApiHandler()
    handler.register(app)
    return app


async def create_platform_container_runtime_app(
    config: Config,
) -> aiohttp.web.Application:
    app = aiohttp.web.Application()
    handler = PlatformContainerRuntimeApiHandler(app, config)
    handler.register(app)
    return app


async def add_version_to_header(request: Request, response: StreamResponse) -> None:
    response.headers["X-Service-Version"] = f"platform-container-runtime/{__version__}"


async def create_app(config: Config) -> aiohttp.web.Application:
    app = aiohttp.web.Application(middlewares=[handle_exceptions])  # type: ignore
    app.on_response_prepare.append(add_version_to_header)
    app_kv: MutableMapping[AppKey[str], Any] = cast(
        MutableMapping[AppKey[str], Any], app
    )

    async def _init_app(app: aiohttp.web.Application) -> AsyncIterator[None]:
        async with AsyncExitStack() as exit_stack:
            logger.info("Initializing Service")
            kube_client = await exit_stack.enter_async_context(KubeClient(config.kube))
            node = await kube_client.get_node(config.node_name)
            logger.info("Container runtime version: %s", node.container_runtime_version)

            cri_client = await exit_stack.enter_async_context(
                create_cri_client(config, node.container_runtime_version)
            )
            runtime_client = await exit_stack.enter_async_context(
                create_runtime_client(
                    config,
                    os=node.os,
                    architecture=node.architecture,
                    container_runtime_version=node.container_runtime_version,
                )
            )
            streaming_client = await exit_stack.enter_async_context(
                aiohttp.ClientSession()
            )

            platform_app = cast(
                aiohttp.web.Application, app_kv[PLATFORM_CONTAINER_RUNTIME_APP_KEY]
            )
            platform_app_kv: MutableMapping[AppKey[str], Any] = cast(
                MutableMapping[AppKey[str], Any], platform_app
            )
            platform_app_kv[CONFIG_KEY] = config
            platform_app_kv[SERVICE_KEY] = Service(
                cri_client=cri_client,
                runtime_client=runtime_client,
                streaming_client=streaming_client,
            )

            yield

    app.cleanup_ctx.append(_init_app)

    api_v1_app = await create_api_v1_app()
    app_kv[API_V1_APP_KEY] = api_v1_app

    platform_app = await create_platform_container_runtime_app(config)
    app_kv[PLATFORM_CONTAINER_RUNTIME_APP_KEY] = platform_app
    api_v1_app.add_subapp("/containers", platform_app)
    app.add_subapp("/api/v1", api_v1_app)

    async def handle_ping(request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(text="Pong")

    app.router.add_get("/ping", handle_ping)

    return app


def main() -> None:  # pragma: no coverage
    init_logging()
    config = EnvironConfigFactory().create()
    logging.info("Loaded config: %r", config)
    setup_sentry()
    aiohttp.web.run_app(
        create_app(config), host=config.server.host, port=config.server.port
    )

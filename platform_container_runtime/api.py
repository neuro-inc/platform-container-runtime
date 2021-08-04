import logging
from contextlib import AsyncExitStack
from typing import AsyncIterator, Awaitable, Callable, List, Optional

import aiohttp
import aiohttp.web
import grpc.aio
from aiohttp.web import (
    HTTPBadRequest,
    HTTPInternalServerError,
    HTTPNoContent,
    HTTPNotFound,
    Request,
    Response,
    StreamResponse,
    json_response,
    middleware,
)
from neuro_logging import (
    init_logging,
    make_request_logging_trace_config,
    make_sentry_trace_config,
    make_zipkin_trace_config,
    notrace,
    setup_sentry,
    setup_zipkin_tracer,
)

from .config import Config, SentryConfig, ZipkinConfig
from .config_factory import EnvironConfigFactory
from .cri import ContainerNotFoundError, RuntimeService
from .kube_client import KubeClient
from .service import Service


logger = logging.getLogger(__name__)


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
            ]
        )

    @property
    def _service(self) -> Service:
        return self._app["service"]

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

    def _get_container_id(self, req: Request) -> str:
        return req.match_info["id"].replace("%2F", "/")


def _parse_bool(value: str) -> bool:
    return value.lower() in ("1", "true", "yes")


@middleware
async def handle_exceptions(
    req: Request, handler: Callable[[Request], Awaitable[StreamResponse]]
) -> StreamResponse:
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


def make_logging_trace_configs() -> List[aiohttp.TraceConfig]:
    return [make_request_logging_trace_config()]


def make_tracing_trace_configs(
    zipkin: Optional[ZipkinConfig], sentry: Optional[SentryConfig]
) -> List[aiohttp.TraceConfig]:
    trace_configs = []

    if zipkin:
        trace_configs.append(make_zipkin_trace_config())

    if sentry:
        trace_configs.append(make_sentry_trace_config())

    return trace_configs


async def create_cri_address(config: Config, kube_client: KubeClient) -> str:
    if config.cri_address:
        return config.cri_address

    node = await kube_client.get_node(config.node_name)

    logger.info("Container runtime version: %s", node.container_runtime_version)

    if node.container_runtime_version.startswith("docker://"):
        return "unix:/hrun/dockershim.sock"
    elif node.container_runtime_version.startswith("containerd://"):
        return "unix:/hrun/containerd/containerd.sock"
    else:
        raise ValueError(
            f"Container runtime {node.container_runtime_version!r} is not supported"
        )


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


async def create_app(config: Config) -> aiohttp.web.Application:
    app = aiohttp.web.Application(middlewares=[handle_exceptions])

    trace_configs = make_logging_trace_configs() + make_tracing_trace_configs(
        config.zipkin, config.sentry
    )

    async def _init_app(app: aiohttp.web.Application) -> AsyncIterator[None]:
        async with AsyncExitStack() as exit_stack:
            logger.info("Initializing Service")

            logger.info("Initializing kube client")
            kube_client = await exit_stack.enter_async_context(
                KubeClient(config.kube, trace_configs=trace_configs)
            )

            logger.info("Initializing CRI address")
            cri_address = await create_cri_address(config, kube_client)

            logger.info("Initializing gRPC channel")
            channel = await exit_stack.enter_async_context(
                grpc.aio.insecure_channel(cri_address)
            )
            runtime_service = await exit_stack.enter_async_context(
                RuntimeService(channel)
            )
            streaming_client = await exit_stack.enter_async_context(
                aiohttp.ClientSession(trace_configs=trace_configs)
            )

            app["platform_container_runtime_app"]["config"] = config
            app["platform_container_runtime_app"]["service"] = Service(
                runtime_service, streaming_client
            )

            yield

    app.cleanup_ctx.append(_init_app)

    api_v1_app = await create_api_v1_app()
    app["api_v1_app"] = api_v1_app

    platform_container_runtime_app = await create_platform_container_runtime_app(config)
    app["platform_container_runtime_app"] = platform_container_runtime_app
    api_v1_app.add_subapp("/containers", platform_container_runtime_app)

    app.add_subapp("/api/v1", api_v1_app)

    return app


def setup_tracing(config: Config) -> None:
    if config.zipkin:
        setup_zipkin_tracer(
            config.zipkin.app_name,
            config.server.host,
            config.server.port,
            config.zipkin.url,
            config.zipkin.sample_rate,
        )

    if config.sentry:
        setup_sentry(
            config.sentry.dsn,
            app_name=config.sentry.app_name,
            cluster_name=config.sentry.cluster_name,
            sample_rate=config.sentry.sample_rate,
        )


def main() -> None:  # pragma: no coverage
    init_logging()
    config = EnvironConfigFactory().create()
    logging.info("Loaded config: %r", config)
    setup_tracing(config)
    aiohttp.web.run_app(
        create_app(config), host=config.server.host, port=config.server.port
    )

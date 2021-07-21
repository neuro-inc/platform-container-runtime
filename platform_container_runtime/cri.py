import functools
import logging
import shlex
from types import TracebackType
from typing import Any, Awaitable, Callable, Optional, Type, TypeVar, cast

import grpc
import grpc.aio
from platform_logging import trace
from yarl import URL


T = TypeVar("T", bound=Callable[..., Awaitable[Any]])


def _handle_errors(func: T) -> T:
    @functools.wraps(func)
    async def new_func(self: "RuntimeService", *args: Any, **kwargs: Any) -> Any:
        try:
            if not self._runtime_service:
                raise RuntimeNotAvailableError()

            return await func(self, *args, **kwargs)
        except grpc.aio.AioRpcError as ex:
            status = ex.code()
            if status == grpc.StatusCode.UNKNOWN or status == grpc.StatusCode.NOT_FOUND:
                container_id = kwargs.get("container_id") or args[0]
                logging.warning("Container '%s' not found", container_id)
                raise ContainerNotFoundError(container_id)
            raise

    return cast(T, new_func)


class RuntimeNotAvailableError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "Container runtime is not available. May CRI Api version is not supported."
        )


class ContainerNotFoundError(Exception):
    def __init__(self, container_id: str) -> None:
        super().__init__(f"Container {container_id!r} not found")


class RuntimeService:
    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._channel = channel
        self._runtime_service: Optional["RuntimeService"] = None

    async def __aenter__(self) -> "RuntimeService":
        try:
            runtime_service_v1 = _RuntimeServiceV1(self._channel)
            await runtime_service_v1.version()
            self._runtime_service = runtime_service_v1
            logging.info("Using CRI v1 gRPC API")
        except Exception:
            runtime_service_v1alpha2 = _RuntimeServiceV1Alpha2(self._channel)
            await runtime_service_v1alpha2.version()
            self._runtime_service = runtime_service_v1alpha2
            logging.info("Using CRI v1alpha2 gRPC API")
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        pass

    @trace
    @_handle_errors
    async def attach(
        self,
        container_id: str,
        *,
        tty: bool = False,
        stdin: bool = False,
        stdout: bool = False,
        stderr: bool = False,
    ) -> URL:
        assert self._runtime_service

        return await self._runtime_service.attach(
            self._strip_scheme(container_id),
            tty=tty,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    @trace
    @_handle_errors
    async def exec(
        self,
        container_id: str,
        cmd: str,
        *,
        tty: bool = False,
        stdin: bool = False,
        stdout: bool = False,
        stderr: bool = False,
    ) -> URL:
        assert self._runtime_service

        return await self._runtime_service.exec(
            self._strip_scheme(container_id),
            cmd,
            tty=tty,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    @trace
    @_handle_errors
    async def stop_container(self, container_id: str, timeout_s: int = 0) -> None:
        assert self._runtime_service

        await self._runtime_service.stop_container(
            self._strip_scheme(container_id), timeout_s
        )

    def _strip_scheme(self, value: str) -> str:
        return value.replace("docker://", "").replace("containerd://", "")


class _RuntimeServiceV1(RuntimeService):
    from k8s.io.cri_api.pkg.apis.runtime.v1.api_pb2 import (
        AttachRequest,
        ExecRequest,
        StopContainerRequest,
        VersionRequest,
    )
    from k8s.io.cri_api.pkg.apis.runtime.v1.api_pb2_grpc import RuntimeServiceStub

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._runtime_service_stub = self.RuntimeServiceStub(channel)

    async def version(self) -> str:
        resp = await self._runtime_service_stub.Version(self.VersionRequest())
        return resp.version

    async def attach(
        self,
        container_id: str,
        *,
        tty: bool = False,
        stdin: bool = False,
        stdout: bool = False,
        stderr: bool = False,
    ) -> URL:
        resp = await self._runtime_service_stub.Attach(
            self.AttachRequest(
                container_id=container_id,
                tty=tty,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        )
        return URL(resp.url)

    async def exec(
        self,
        container_id: str,
        cmd: str,
        *,
        tty: bool = False,
        stdin: bool = False,
        stdout: bool = False,
        stderr: bool = False,
    ) -> URL:
        resp = await self._runtime_service_stub.Exec(
            self.ExecRequest(
                container_id=container_id,
                cmd=shlex.split(cmd),
                tty=tty,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        )
        return URL(resp.url)

    async def stop_container(self, container_id: str, timeout_s: int = 0) -> None:
        await self._runtime_service_stub.StopContainer(
            self.StopContainerRequest(
                container_id=container_id,
                timeout=timeout_s,
            )
        )


class _RuntimeServiceV1Alpha2(RuntimeService):
    from k8s.io.cri_api.pkg.apis.runtime.v1alpha2.api_pb2 import (
        AttachRequest,
        ExecRequest,
        StopContainerRequest,
        VersionRequest,
    )
    from k8s.io.cri_api.pkg.apis.runtime.v1alpha2.api_pb2_grpc import RuntimeServiceStub

    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._runtime_service_stub = self.RuntimeServiceStub(channel)

    async def version(self) -> str:
        resp = await self._runtime_service_stub.Version(self.VersionRequest())
        return resp.version

    async def attach(
        self,
        container_id: str,
        *,
        tty: bool = False,
        stdin: bool = False,
        stdout: bool = False,
        stderr: bool = False,
    ) -> URL:
        resp = await self._runtime_service_stub.Attach(
            self.AttachRequest(
                container_id=container_id,
                tty=tty,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        )
        return URL(resp.url)

    async def exec(
        self,
        container_id: str,
        cmd: str,
        *,
        tty: bool = False,
        stdin: bool = False,
        stdout: bool = False,
        stderr: bool = False,
    ) -> URL:
        resp = await self._runtime_service_stub.Exec(
            self.ExecRequest(
                container_id=container_id,
                cmd=shlex.split(cmd),
                tty=tty,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
        )
        return URL(resp.url)

    async def stop_container(self, container_id: str, timeout_s: int = 0) -> None:
        await self._runtime_service_stub.StopContainer(
            self.StopContainerRequest(
                container_id=container_id,
                timeout=timeout_s,
            )
        )

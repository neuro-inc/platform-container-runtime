import enum
import functools
import logging
import shlex
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Awaitable, Callable, Optional, Type, TypeVar, cast

import grpc
import grpc.aio
from neuro_logging import trace
from yarl import URL


T = TypeVar("T", bound=Callable[..., Awaitable[Any]])


class ContainerState(int, enum.Enum):
    CREATED = 0
    RUNNING = 1
    EXITED = 2
    UNKNOWN = 3


@dataclass(frozen=True)
class ContainerStatus:
    id: str
    state: ContainerState


def _handle_errors(func: T) -> T:
    @functools.wraps(func)
    async def new_func(self: "CriClient", *args: Any, **kwargs: Any) -> Any:
        try:
            if not self._cri_client:
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


class CriClient:
    def __init__(self, channel: grpc.aio.Channel) -> None:
        self._channel = channel
        self._cri_client: Optional["CriClient"] = None

    async def __aenter__(self) -> "CriClient":
        try:
            cri_client_v1 = _CriClientV1(self._channel)
            await cri_client_v1.version()
            self._cri_client = cri_client_v1
            logging.info("Using CRI v1 gRPC API")
        except Exception:
            cri_client_v1alpha2 = _CriClientV1Alpha2(self._channel)
            await cri_client_v1alpha2.version()
            self._cri_client = cri_client_v1alpha2
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
    async def get_status(self, container_id: str) -> ContainerStatus:
        assert self._cri_client

        return await self._cri_client.get_status(container_id)

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
        assert self._cri_client

        return await self._cri_client.attach(
            container_id,
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
        assert self._cri_client

        return await self._cri_client.exec(
            container_id,
            cmd,
            tty=tty,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )

    @trace
    @_handle_errors
    async def stop_container(self, container_id: str, timeout_s: int = 0) -> None:
        assert self._cri_client

        await self._cri_client.stop_container(container_id, timeout_s)


class _CriClientV1(CriClient):
    from k8s.io.cri_api.pkg.apis.runtime.v1.api_pb2 import (
        AttachRequest,
        ContainerStatusRequest,
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

    async def get_status(self, container_id: str) -> ContainerStatus:
        resp = await self._runtime_service_stub.ContainerStatus(
            self.ContainerStatusRequest(container_id=container_id)
        )
        return ContainerStatus(
            id=resp.status.id, state=ContainerState(resp.status.state)
        )

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


class _CriClientV1Alpha2(CriClient):
    from k8s.io.cri_api.pkg.apis.runtime.v1alpha2.api_pb2 import (
        AttachRequest,
        ContainerStatusRequest,
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

    async def get_status(self, container_id: str) -> ContainerStatus:
        resp = await self._runtime_service_stub.ContainerStatus(
            self.ContainerStatusRequest(container_id=container_id)
        )
        return ContainerStatus(
            id=resp.status.id, state=ContainerState(resp.status.state)
        )

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

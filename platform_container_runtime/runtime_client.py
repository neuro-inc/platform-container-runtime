from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

from aiodocker import Docker, DockerError
from docker_image.reference import (
    InvalidReference as _InvalidImageReference,
    Reference as _ImageReference,
)

from .containerd_client import ContainerdClient
from .utils import asyncgeneratorcontextmanager


class ContainerNotFoundError(Exception):
    def __init__(self, container_id: str) -> None:
        super().__init__(f"Container {container_id!r} not found")


@dataclass(frozen=True)
class ImageReference:
    """
    https://github.com/docker/distribution/blob/master/reference/reference.go
    """

    domain: str = ""
    path: str = ""
    tag: str = ""

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("blank reference path")

    @property
    def repository(self) -> str:
        if self.domain:
            return f"{self.domain}/{self.path}"
        return self.path

    def __str__(self) -> str:
        if self.tag:
            return f"{self.repository}:{self.tag}"
        return self.repository

    @classmethod
    def parse(cls, ref_str: str) -> "ImageReference":
        try:
            ref = _ImageReference.parse(ref_str)
        except _InvalidImageReference as exc:
            raise ValueError(str(exc))
        domain, path = ref.split_hostname()
        return cls(domain=domain or "", path=path, tag=ref["tag"] or "")


class RuntimeClient:
    def __init__(
        self,
        docker_client: Optional[Docker] = None,
        containerd_client: Optional[ContainerdClient] = None,
    ) -> None:
        self._docker_client = docker_client
        self._containerd_client = containerd_client

    @asyncgeneratorcontextmanager
    async def commit(
        self, container_id: str, image: str
    ) -> AsyncIterator[Dict[str, Any]]:
        image_ref = ImageReference.parse(image)

        if self._docker_client:
            try:
                container = await self._docker_client.containers.get(container_id)
            except DockerError as ex:
                if ex.status == 404:
                    raise ContainerNotFoundError(container_id)
                raise
            yield self._create_commit_started_chunk(
                container_id=container_id, image=image
            )
            await container.commit(repository=image_ref.repository, tag=image_ref.tag)
            yield self._create_commit_finished_chunk()
            return

        if self._containerd_client:
            await self._containerd_client.commit(container_id=container_id)

        raise ValueError("Commit is not supported by container runtime")

    @asyncgeneratorcontextmanager
    async def push(
        self, image: str, auth: Optional[Dict[str, Any]] = None
    ) -> AsyncIterator[Dict[str, Any]]:
        image_ref = ImageReference.parse(image)

        if self._docker_client:
            async for chunk in self._docker_client.images.push(
                image_ref.repository, tag=image_ref.tag, auth=auth, stream=True
            ):
                yield chunk

                if "error" in chunk:
                    break
            return

        raise ValueError("Push is not supported by container runtime")

    def _create_commit_started_chunk(
        self, container_id: str, image: str
    ) -> Dict[str, Any]:
        return {
            "status": "CommitStarted",
            "details": {"container": container_id, "image": image},
        }

    def _create_commit_finished_chunk(self) -> Dict[str, Any]:
        return {"status": "CommitFinished"}

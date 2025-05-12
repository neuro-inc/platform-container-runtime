from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import grpc.aio
from docker_image.reference import Reference
from neuro_logging import trace
from yarl import URL

from containerd.services.containers.v1.containers_pb2 import GetContainerRequest
from containerd.services.containers.v1.containers_pb2_grpc import ContainersStub
from containerd.services.content.v1.content_pb2 import (
    COMMIT,
    InfoRequest,
    ReadContentRequest,
    WriteContentRequest,
)
from containerd.services.content.v1.content_pb2_grpc import ContentStub
from containerd.services.diff.v1.diff_pb2 import DiffRequest
from containerd.services.diff.v1.diff_pb2_grpc import DiffStub
from containerd.services.images.v1.images_pb2 import (
    CreateImageRequest,
    GetImageRequest,
    Image as ImagePb2,
    UpdateImageRequest,
)
from containerd.services.images.v1.images_pb2_grpc import ImagesStub
from containerd.services.leases.v1.leases_pb2 import CreateRequest, DeleteRequest
from containerd.services.leases.v1.leases_pb2_grpc import LeasesStub
from containerd.services.snapshots.v1.snapshots_pb2 import (
    MountsRequest,
    RemoveSnapshotRequest,
    StatSnapshotRequest,
    ViewSnapshotRequest,
)
from containerd.services.snapshots.v1.snapshots_pb2_grpc import SnapshotsStub
from containerd.services.tasks.v1.tasks_pb2 import (
    GetRequest,
    PauseTaskRequest,
    ResumeTaskRequest,
)
from containerd.services.tasks.v1.tasks_pb2_grpc import TasksStub
from containerd.types.descriptor_pb2 import Descriptor as DescriptorPb2
from containerd.types.task.task_pb2 import CREATED, PAUSED, STOPPED
from google.protobuf.timestamp_pb2 import Timestamp

from .errors import (
    ContainerNotFoundError,
    ImageNotFoundError,
    MediaTypeNotSupportedError,
    PlatformNotSupportedError,
)
from .registry_client import Auth, RegistryClient
from .utils import asyncgeneratorcontextmanager

logger = logging.getLogger(__name__)


class MediaType(str, enum.Enum):
    OCI_IMAGE_INDEX_V1 = "application/vnd.oci.image.index.v1+json"
    OCI_IMAGE_MANIFEST_V1 = "application/vnd.oci.image.manifest.v1+json"

    DOCKER_MANIFEST_LIST_V2 = (
        "application/vnd.docker.distribution.manifest.list.v2+json"
    )
    DOCKER_MANIFEST_V2 = "application/vnd.docker.distribution.manifest.v2+json"
    DOCKER_IMAGE_CONFIG_V1 = "application/vnd.docker.container.image.v1+json"
    DOCKER_IMAGE_LAYER_GZIP = "application/vnd.docker.image.rootfs.diff.tar.gzip"


@dataclass(frozen=True)
class Descriptor:
    media_type: str
    digest: str
    size: int

    @classmethod
    def from_data(cls, media_type: str, data: dict[str, Any]) -> Descriptor:
        dump = json.dumps(data).encode()
        return cls(media_type=media_type, digest=_create_digest(dump), size=len(dump))

    @classmethod
    def parse(cls, content: dict[str, Any]) -> Descriptor:
        return cls(
            media_type=content["mediaType"],
            digest=content["digest"],
            size=content["size"],
        )

    def to_primitive(self) -> dict[str, Any]:
        return {"mediaType": self.media_type, "digest": self.digest, "size": self.size}

    def __str__(self) -> str:
        return f"({self.media_type},{self.digest},{self.size})"

    def __repr__(self) -> str:
        return self.__str__()


@dataclass(frozen=True)
class Clients:
    leases: LeasesStub
    containers: ContainersStub
    tasks: TasksStub
    snapshots: SnapshotsStub
    diff: DiffStub
    images: ImagesStub
    content: ContentStub
    registry: RegistryClient

    @classmethod
    def create(
        cls, channel: grpc.aio.Channel, registry_client: RegistryClient
    ) -> Clients:
        return cls(
            leases=LeasesStub(channel),
            containers=ContainersStub(channel),
            tasks=TasksStub(channel),
            snapshots=SnapshotsStub(channel),
            diff=DiffStub(channel),
            images=ImagesStub(channel),
            content=ContentStub(channel),
            registry=registry_client,
        )


class Metadata(grpc.aio.Metadata):
    def __init__(
        self, *, namespace: str | None = None, lease_id: str | None = None
    ) -> None:
        super().__init__()
        if namespace:
            self.add("containerd-namespace", namespace)
        if lease_id:
            self.add("containerd-lease", lease_id)


# leases are used to tell Containerd garbage collector
# to not delete resources while lease exists or is not expired
class Lease:
    def __init__(self, clients: Clients, namespace: str, duration: timedelta) -> None:
        self._leases_stub = clients.leases
        self._namespace = namespace
        self._duration = duration
        self._lease_id = ""

    async def __aenter__(self) -> str:
        expire = datetime.now(timezone.utc) + self._duration
        resp = await self._leases_stub.Create(
            CreateRequest(labels={"containerd.io/gc.expire": expire.isoformat()}),
            metadata=Metadata(namespace=self._namespace),
        )
        self._lease_id = resp.lease.id
        logger.info("Created lease %r", self._lease_id)
        return self._lease_id

    async def __aexit__(self, *args: Any, **kwargs: Any) -> None:
        await self._leases_stub.Delete(
            DeleteRequest(id=self._lease_id),
            metadata=Metadata(namespace=self._namespace),
        )
        logger.info("Removed lease %r", self._lease_id)


@dataclass(frozen=True)
class SnapshotDiff:
    id: str
    descriptor: Descriptor
    parent_key: str | None = None


class Snapshot:
    def __init__(
        self,
        clients: Clients,
        namespace: str,
        snapshotter: str,
        snapshot_key: str,
        is_active: bool = False,
    ) -> None:
        self._clients = clients
        self._namespace = namespace
        self._snapshotter = snapshotter
        self._snapshot_key = snapshot_key
        self._is_active = is_active

    @property
    def snapshotter(self) -> str:
        return self._snapshotter

    @property
    def snapshot_key(self) -> str:
        return self._snapshot_key

    @trace
    async def get_diff(self, *, lease_id: str | None = None) -> SnapshotDiff:
        parent_snapshot = await self._get_parent()
        if parent_snapshot:
            left_mounts = await parent_snapshot._get_mounts(lease_id=lease_id)
        else:
            left_mounts = None
        right_mounts = await self._get_mounts(lease_id=lease_id)
        resp = await self._clients.diff.Diff(
            DiffRequest(left=left_mounts, right=right_mounts),
            metadata=Metadata(namespace=self._namespace, lease_id=lease_id),
        )
        desc = Descriptor(
            # replace media type with docker compatible
            media_type=MediaType.DOCKER_IMAGE_LAYER_GZIP.value,
            digest=resp.diff.digest,
            size=resp.diff.size,
        )
        resp = await self._clients.content.Info(
            InfoRequest(digest=desc.digest),
            metadata=Metadata(namespace=self._namespace),
        )
        return SnapshotDiff(
            id=resp.info.labels["containerd.io/uncompressed"],
            descriptor=desc,
            parent_key=parent_snapshot.snapshot_key if parent_snapshot else None,
        )

    @trace
    async def _get_parent(self) -> Snapshot | None:
        resp = await self._clients.snapshots.Stat(
            StatSnapshotRequest(snapshotter=self._snapshotter, key=self._snapshot_key),
            metadata=Metadata(namespace=self._namespace),
        )
        if not resp.info.parent:
            return None
        return self.__class__(
            clients=self._clients,
            namespace=self._namespace,
            snapshotter=self._snapshotter,
            snapshot_key=resp.info.parent,
        )

    @trace
    async def _get_mounts(self, *, lease_id: str | None = None) -> list[Any]:
        if self._is_active:
            resp = await self._clients.snapshots.Mounts(
                MountsRequest(snapshotter=self._snapshotter, key=self._snapshot_key),
                metadata=Metadata(namespace=self._namespace),
            )
            return resp.mounts
        return await self._get_committed_snapshot_mounts(
            self._snapshot_key, lease_id=lease_id
        )

    @trace
    async def _get_committed_snapshot_mounts(
        self, snapshot_key: str, *, lease_id: str | None = None
    ) -> list[Any]:
        view_key = f"{self._snapshot_key}-parent-view"
        resp = await self._clients.snapshots.View(
            ViewSnapshotRequest(
                snapshotter=self._snapshotter,
                key=view_key,
                parent=snapshot_key,
            ),
            metadata=Metadata(namespace=self._namespace, lease_id=lease_id),
        )
        logger.info("Created parent snapshot view %r", view_key)
        await self._clients.snapshots.Remove(
            RemoveSnapshotRequest(snapshotter=self._snapshotter, key=view_key),
            metadata=Metadata(namespace=self._namespace),
        )
        logger.info("Removed parent snapshot view %r", view_key)
        return resp.mounts


class ImageProgess:
    def __init__(self, total: int) -> None:
        self._next = asyncio.Event()
        self._total = total
        self._current = 0
        self._stopped = False

    async def __aiter__(self) -> AsyncIterator[int]:
        while True:
            if self._stopped:
                break

            yield self._current

            if self._current >= self._total:
                break
            await self._next.wait()
            self._next.clear()

    @property
    def total(self) -> int:
        return self._total

    def step(self, progress: int) -> None:
        self._current += progress
        if progress:
            self._next.set()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._next.set()


class ImageManifest(dict[str, Any]):
    def __init__(
        self, clients: Clients, namespace: str, content: dict[str, Any]
    ) -> None:
        super().__init__(content)
        self._clients = clients
        self._namespace = namespace

    @property
    def config(self) -> dict[str, Any]:
        return _get_value(self, "config", "Config")

    @classmethod
    @trace
    async def read(
        cls,
        clients: Clients,
        namespace: str,
        architecture: str,
        os: str,
        descriptor: Descriptor,
    ) -> ImageManifest:
        manifest = await cls._read_manifest(
            clients,
            namespace=namespace,
            architecture=architecture,
            os=os,
            digest=descriptor.digest,
        )
        config = _get_value(manifest, "config", "Config")
        config_digest = _get_value(config, "digest", "Digest")
        manifest["config"] = await cls._read_config(
            clients, namespace=namespace, digest=config_digest
        )
        return cls(clients=clients, namespace=namespace, content=manifest)

    @classmethod
    @trace
    async def _read_manifest(
        cls, clients: Clients, namespace: str, architecture: str, os: str, digest: str
    ) -> dict[str, Any]:
        logger.info("Reading manifest %r", digest)
        while True:
            data: list[bytes] = []
            async for resp in clients.content.Read(
                ReadContentRequest(
                    digest=digest,
                    offset=0,  # from the start
                    size=0,  # entire content
                ),
                metadata=Metadata(namespace=namespace),
            ):
                data.append(resp.data)

            logger.info("Read content %r", digest)
            content = json.loads(b"".join(data))
            media_type = _get_value(content, "mediaType", "MediaType")

            if media_type in (
                MediaType.DOCKER_MANIFEST_V2,
                MediaType.OCI_IMAGE_MANIFEST_V1,
            ):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Reading image manifest:\n%s", json.dumps(content, indent=2)
                    )
                return content

            logger.debug("Searching for '%s/%s' manifest", architecture, os)

            if media_type in (
                MediaType.DOCKER_MANIFEST_LIST_V2,
                MediaType.OCI_IMAGE_INDEX_V1,
            ):
                for m in content["manifests"]:
                    platform = _get_value(m, "platform", "Platform")
                    manifest_os = _get_value(platform, "os", "Os")
                    manifest_arch = _get_value(platform, "architecture", "Architecture")
                    if (
                        manifest_os.lower() == os
                        and manifest_arch.lower() == architecture
                    ):
                        digest = m["digest"]
                        break
                else:
                    raise PlatformNotSupportedError(architecture=architecture, os=os)
                logger.debug("Found '%s/%s' manifest %r", architecture, os, digest)
                continue

            raise MediaTypeNotSupportedError(media_type)

    @classmethod
    @trace
    async def _read_config(
        cls, clients: Clients, namespace: str, digest: str
    ) -> dict[str, Any]:
        data: list[bytes] = []
        async for resp in clients.content.Read(
            ReadContentRequest(
                digest=digest,
                offset=0,  # from the start
                size=0,  # entire content
            ),
            metadata=Metadata(namespace=namespace),
        ):
            data.append(resp.data)
        content = json.loads(b"".join(data))
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Reading image config:\n%s", json.dumps(content, indent=2))
        return content

    @trace
    async def write(
        self,
        *,
        config_labels: dict[str, Any] | None = None,
        lease_id: str | None = None,
    ) -> Descriptor:
        config_desc = Descriptor.from_data(
            MediaType.DOCKER_IMAGE_CONFIG_V1, self.config
        )
        await self._write_config(
            content=self.config,
            desc=config_desc,
            labels=config_labels,
            lease_id=lease_id,
        )
        manifest = dict(self)
        manifest["config"] = config_desc.to_primitive()
        manifest_desc = Descriptor.from_data(MediaType.DOCKER_MANIFEST_V2, manifest)
        await self._write_manifest(
            content=manifest,
            desc=manifest_desc,
            lease_id=lease_id,
        )
        return manifest_desc

    @trace
    async def _write_manifest(
        self,
        content: dict[str, Any],
        desc: Descriptor,
        *,
        lease_id: str | None = None,
    ) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Writing image manifest:\n%s", json.dumps(content, indent=2))
        labels = {"containerd.io/gc.ref.content.config": content["config"]["digest"]}
        for i, layer in enumerate(content["layers"]):
            digest = _get_value(layer, "digest", "Digest")
            labels[f"containerd.io/gc.ref.content.l.{i}"] = digest

        async for resp in self._clients.content.Write(
            [
                WriteContentRequest(
                    action=COMMIT,
                    ref=desc.digest,
                    data=json.dumps(content).encode(),
                    offset=0,
                    total=desc.size,
                    expected=desc.digest,
                    labels=labels,
                ),
            ],
            metadata=Metadata(namespace=self._namespace, lease_id=lease_id),
        ):
            assert resp.action == COMMIT
            assert resp.offset == desc.size, "Not all data was written"
            assert resp.digest == desc.digest, "Data is corrupted"
        logger.info("Created image manifest content %r", desc.digest)

    @trace
    async def _write_config(
        self,
        content: dict[str, Any],
        desc: Descriptor,
        *,
        labels: dict[str, Any] | None = None,
        lease_id: str | None = None,
    ) -> None:
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Writing image config:\n%s", json.dumps(content, indent=2))
        async for resp in self._clients.content.Write(
            [
                WriteContentRequest(
                    action=COMMIT,
                    ref=desc.digest,
                    data=json.dumps(content).encode(),
                    offset=0,
                    total=desc.size,
                    expected=desc.digest,
                    labels=labels,
                ),
            ],
            metadata=Metadata(namespace=self._namespace, lease_id=lease_id),
        ):
            assert resp.action == COMMIT
            assert resp.offset == desc.size, "Not all data was written"
            assert resp.digest == desc.digest, "Data is corrupted"
        logger.info("Created image config content %r", desc.digest)

    @trace
    async def push(
        self, server: str, name: str, ref: str, auth: Auth | None
    ) -> Descriptor:
        logger.info("Pushing image '%s/%s:%s' manifest", server, name, ref)
        config_desc = Descriptor.from_data(
            MediaType.DOCKER_IMAGE_CONFIG_V1, self.config
        )
        upload_url = await self._clients.registry.start_blob_upload(
            server=server, name=name, auth=auth
        )
        await self._clients.registry.upload_blob(
            upload_url=upload_url,
            media_type=MediaType.DOCKER_IMAGE_CONFIG_V1.value,
            digest=config_desc.digest,
            data_length=config_desc.size,
            data=json.dumps(self.config).encode(),
            auth=auth,
        )
        logger.info("Pushed image '%s/%s:%s' config", server, name, ref)
        manifest = dict(self)
        manifest["config"] = config_desc.to_primitive()
        manifest_desc = Descriptor.from_data(MediaType.DOCKER_MANIFEST_V2, manifest)
        if logger.isEnabledFor(logging.INFO):
            logger.info("Updating manifest:\n%s", json.dumps(manifest, indent=2))
        await self._clients.registry.update_manifest(
            server=server,
            name=name,
            ref=ref,
            media_type=MediaType.DOCKER_MANIFEST_V2.value,
            manifest=manifest,
            auth=auth,
        )
        logger.info("Pushed image '%s/%s:%s' manifest", server, name, ref)
        return manifest_desc


class Image:
    def __init__(
        self, clients: Clients, namespace: str, name: str, manifest: ImageManifest
    ) -> None:
        self._clients = clients
        self._namespace = namespace
        self._name = name
        self._manifest = manifest

        ref = Reference.parse_normalized_named(name)
        server, repo = ref.split_hostname()
        self._image_server = server
        self._image_repo = repo
        self._image_tag = ref["tag"]

    @property
    def config(self) -> dict[str, Any]:
        return self._manifest.config

    @property
    def layers(self) -> list[dict[str, Any]]:
        return list(_get_value(self._manifest, "layers", "Layers"))

    @property
    def diff_ids(self) -> list[dict[str, Any]]:
        root_fs = _get_value(self.config, "rootfs", "RootFS")
        diff_ids = _get_value(root_fs, "diff_ids", "Diff_ids")
        return diff_ids

    @property
    def url(self) -> str:
        return f"{self._image_server}/{self._image_repo}:{self._image_tag}"

    @classmethod
    @trace
    async def read(
        cls, clients: Clients, namespace: str, name: str, architecture: str, os: str
    ) -> Image:
        resp = await clients.images.Get(
            GetImageRequest(name=name),
            metadata=Metadata(namespace=namespace),
        )
        manifest_descriptor = Descriptor(
            media_type=resp.image.target.media_type,
            digest=resp.image.target.digest,
            size=resp.image.target.size,
        )
        manifest = await ImageManifest.read(
            clients,
            namespace=namespace,
            architecture=architecture,
            os=os,
            descriptor=manifest_descriptor,
        )
        return cls(clients=clients, namespace=namespace, name=name, manifest=manifest)

    @trace
    async def write(
        self,
        config_labels: dict[str, Any] | None = None,
        *,
        lease_id: str | None = None,
    ) -> None:
        manifest_desc = await self._manifest.write(
            config_labels=config_labels, lease_id=lease_id
        )
        created_at = Timestamp()
        created_at.GetCurrentTime()
        image_pb2 = ImagePb2(
            name=self._name,
            target=DescriptorPb2(
                media_type=manifest_desc.media_type,
                digest=manifest_desc.digest,
                size=manifest_desc.size,
            ),
            created_at=created_at,
        )
        try:
            await self._clients.images.Update(
                UpdateImageRequest(image=image_pb2),
                metadata=Metadata(namespace=self._namespace),
            )
            logger.info("Updated image %r", self._name)
        except grpc.aio.AioRpcError as ex:
            status = ex.code()
            if status == grpc.StatusCode.UNKNOWN or status == grpc.StatusCode.NOT_FOUND:
                await self._clients.images.Create(
                    CreateImageRequest(image=image_pb2),
                    metadata=Metadata(namespace=self._namespace),
                )
                logger.info("Created image %r", self._name)
                return
            raise

    @asyncgeneratorcontextmanager
    async def push(self, auth: Auth | None = None) -> AsyncIterator[dict[str, Any]]:
        yield self._create_image_progress_step(
            status=(
                "The push refers to repository "
                f"[{self._image_server}/{self._image_repo}]"
            )
        )
        registry_api_version = await self._clients.registry.get_version(
            self._image_server, auth
        )
        assert registry_api_version == "v2", "Registry API V1 is not supported"
        for layer_diff_id, layer in zip(self.diff_ids, self.layers):
            layer_desc = Descriptor.parse(layer)
            async with self._push_layer(layer_diff_id, layer_desc, auth=auth) as it:
                async for progress in it:
                    yield progress
        manifest_desc = await self._manifest.push(
            server=self._image_server,
            name=self._image_repo,
            ref=self._image_tag,
            auth=auth,
        )
        yield self._create_image_progress_step(
            status=(
                f"{self._image_tag}: "
                f"digest: {manifest_desc.digest} "
                f"size: {manifest_desc.size}"
            )
        )
        yield self._create_image_progress_step(
            aux={
                "Tag": self._image_tag,
                "Digest": manifest_desc.digest,
                "Size": manifest_desc.size,
            }
        )

    @asyncgeneratorcontextmanager
    async def _push_layer(
        self,
        diff_id: str,
        desc: Descriptor,
        chunk_size: int = 1024 * 1024,  # 1 MB
        auth: Auth | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        layer_id = self._create_layer_id(diff_id)
        yield self._create_image_progress_step(layer_id=layer_id, status="Preparing")

        layers_exists = await self._clients.registry.check_blob(
            server=self._image_server,
            name=self._image_repo,
            digest=desc.digest,
            auth=auth,
        )

        if layers_exists:
            logger.info(
                "Image %r layer (%s,%s) already exists",
                self._name,
                layer_id,
                desc.digest,
            )

            yield self._create_image_progress_step(
                layer_id=layer_id, status="Layer already exists"
            )
            return

        logger.info("Pushing image %r layer (%s,%s)", self._name, layer_id, desc.digest)

        upload_url = await self._clients.registry.start_blob_upload(
            server=self._image_server, name=self._image_repo, auth=auth
        )
        push_task = None

        try:
            progress = ImageProgess(desc.size)
            push_task = asyncio.create_task(
                self._push_layer_monolithic(
                    upload_url=upload_url,
                    progress=progress,
                    desc=desc,
                    chunk_size=chunk_size,
                    auth=auth,
                )
            )

            async for current in progress:
                yield self._create_image_progress_step(
                    layer_id=layer_id,
                    status="Pushing",
                    current=current,
                    total=progress.total,
                )

            await push_task
        finally:
            if push_task is not None and not push_task.done():
                with suppress(asyncio.CancelledError):
                    push_task.cancel()

        yield self._create_image_progress_step(layer_id=layer_id, status="Pushed")

        logger.info("Pushed image %r layer (%s,%s)", self._name, layer_id, desc.digest)

    async def _push_layer_monolithic(
        self,
        upload_url: URL,
        progress: ImageProgess,
        desc: Descriptor,
        chunk_size: int,
        auth: Auth | None,
    ) -> None:
        try:
            async with self._read_layer_chunked(desc, chunk_size, progress) as chunks:
                await self._clients.registry.upload_blob(
                    upload_url=upload_url,
                    media_type="application/octet-stream",
                    digest=desc.digest,
                    data_length=desc.size,
                    data=chunks,
                    auth=auth,
                )
        finally:
            progress.stop()

    @asyncgeneratorcontextmanager
    async def _read_layer_chunked(
        self, desc: Descriptor, chunk_size: int, progress: ImageProgess
    ) -> AsyncIterator[bytes]:
        buffer: list[bytes] = []
        buffer_len = 0

        async for resp in self._clients.content.Read(
            ReadContentRequest(
                digest=desc.digest,
                offset=0,
                size=0,  # entire content
            ),
            metadata=Metadata(namespace=self._namespace),
        ):
            if buffer_len + len(resp.data) > chunk_size:
                prefix_len = chunk_size - buffer_len
                buffer.append(resp.data[0:prefix_len])
                chunk = b"".join(buffer)
                buffer = [resp.data[prefix_len:]]
                buffer_len = len(buffer[0])
                yield chunk
                progress.step(chunk_size)
            elif buffer_len + len(resp.data) == chunk_size:
                buffer.append(resp.data)
                chunk = b"".join(buffer)
                buffer = []
                buffer_len = 0
                yield chunk
                progress.step(chunk_size)
            else:
                buffer.append(resp.data)
                buffer_len += len(resp.data)
        if buffer:
            chunk = b"".join(buffer)
            yield b"".join(buffer)
            progress.step(len(chunk))

    def _create_image_progress_step(
        self,
        status: str = "",
        layer_id: str = "",
        aux: dict[str, Any] | None = None,
        current: float | None = None,
        total: float | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if status:
            result["status"] = status
        if aux:
            result["aux"] = aux
        if layer_id:
            result["id"] = layer_id
        if current is not None and total is not None:
            result["progressDetail"] = {"current": current, "total": total}
        return result

    def _create_layer_id(self, diff_id: str) -> str:
        i = diff_id.find(":")
        if i == -1:
            return diff_id[:12]
        start = i + 1
        end = start + 12
        return diff_id[start:end]


class Container:
    def __init__(
        self,
        clients: Clients,
        namespace: str,
        architecture: str,
        os: str,
        id: str,
        image: str,
        snapshot: Snapshot,
    ) -> None:
        self._clients = clients
        self._namespace = namespace
        self._architecture = architecture
        self._os = os
        self._id = id
        self._image = image
        self._snapshot = snapshot

    @property
    def id(self) -> str:
        return self._id

    @property
    def image(self) -> str:
        return self._image

    @property
    def snapshotter(self) -> str:
        return self._snapshot.snapshotter

    @property
    def snapshot_key(self) -> str:
        return self._snapshot.snapshot_key

    @trace
    async def pause(self) -> bool:
        resp = await self._clients.tasks.Get(
            GetRequest(container_id=self._id),
            metadata=Metadata(namespace=self._namespace),
        )
        if resp.process.status in (CREATED, PAUSED, STOPPED):
            logger.info("Container %r is not running", self._id)
            return False
        await self._clients.tasks.Pause(
            PauseTaskRequest(container_id=self._id),
            metadata=Metadata(namespace=self._namespace),
        )
        logger.info("Container %r paused", self._id)
        return True

    @trace
    async def resume(self) -> None:
        await self._clients.tasks.Resume(
            ResumeTaskRequest(container_id=self._id),
            metadata=Metadata(namespace=self._namespace),
        )
        logger.info("Container %r resumed", self._id)

    @trace
    async def commit(self, image: str) -> None:
        paused = False

        try:
            paused = await self.pause()

            async with Lease(
                self._clients, self._namespace, timedelta(hours=1)
            ) as lease_id:
                parent_image = await Image.read(
                    self._clients,
                    namespace=self._namespace,
                    name=self._image,
                    architecture=self._architecture,
                    os=self._os,
                )
                snapshot_diffs = await self._get_snapshot_diffs(lease_id=lease_id)
                new_image_manifest = ImageManifest(
                    self._clients,
                    namespace=self._namespace,
                    content=self._create_commit_image_manifest(
                        parent_image, snapshot_diffs
                    ),
                )
                new_image_diff_ids_digest = _create_digest_chain(
                    *(d.id for d in snapshot_diffs)
                )
                new_image = Image(
                    self._clients,
                    namespace=self._namespace,
                    name=image,
                    manifest=new_image_manifest,
                )
                await new_image.write(
                    config_labels={
                        f"containerd.io/gc.ref.snapshot.{self._snapshot.snapshotter}": (
                            new_image_diff_ids_digest
                        )
                    },
                    lease_id=lease_id,
                )
        finally:
            if paused:
                await self.resume()

    async def _get_snapshot_diffs(
        self, *, lease_id: str | None = None
    ) -> list[SnapshotDiff]:
        result = []
        snapshot = self._snapshot
        while snapshot:
            snapshot_diff = await snapshot.get_diff(lease_id=lease_id)
            result.append(snapshot_diff)
            logger.info("Created image layer %r", snapshot_diff.descriptor)
            if not snapshot_diff.parent_key:
                break
            snapshot = Snapshot(
                clients=self._clients,
                namespace=self._namespace,
                snapshotter=self.snapshotter,
                snapshot_key=snapshot_diff.parent_key,
            )
        result.reverse()
        return result

    def _create_commit_image_manifest(
        self, parent_image: Image, snapshot_diffs: Sequence[SnapshotDiff]
    ) -> dict[str, Any]:
        return {
            "schemaVersion": 2,
            "mediaType": MediaType.DOCKER_MANIFEST_V2,
            "config": self._create_commit_image_config(parent_image, snapshot_diffs),
            "layers": [d.descriptor.to_primitive() for d in snapshot_diffs],
        }

    def _create_commit_image_config(
        self, parent_image: Image, snapshot_diffs: Sequence[SnapshotDiff]
    ) -> dict[str, Any]:
        config = _get_value(parent_image.config, "config", "Config")
        return {
            "architecture": self._architecture,
            "os": self._os,
            "config": config,
            "rootfs": {
                "type": "layers",
                "diff_ids": [d.id for d in snapshot_diffs],
            },
            "author": "",
            "created": datetime.now(timezone.utc).isoformat(),
        }


class ContainerdClient:
    def __init__(
        self,
        channel: grpc.aio.Channel,
        registry_client: RegistryClient,
        architecture: str,
        os: str,
        namespace: str = "k8s.io",
    ) -> None:
        self._namespace = namespace
        self._architecture = architecture.lower()
        self._os = os.lower()
        self._clients = Clients.create(channel, registry_client)

    @trace
    async def get_container(self, container_id: str) -> Container:
        try:
            resp = await self._clients.containers.Get(
                GetContainerRequest(id=container_id),
                metadata=Metadata(namespace=self._namespace),
            )
            return Container(
                clients=self._clients,
                namespace=self._namespace,
                architecture=self._architecture,
                os=self._os,
                id=resp.container.id,
                image=resp.container.image,
                snapshot=Snapshot(
                    clients=self._clients,
                    namespace=self._namespace,
                    snapshotter=resp.container.snapshotter,
                    snapshot_key=resp.container.snapshot_key,
                    is_active=True,
                ),
            )
        except grpc.aio.AioRpcError as ex:
            status = ex.code()
            if status == grpc.StatusCode.UNKNOWN or status == grpc.StatusCode.NOT_FOUND:
                raise ContainerNotFoundError(container_id)
            raise

    @trace
    async def get_image(self, name: str) -> Image:
        try:
            return await Image.read(
                self._clients,
                namespace=self._namespace,
                name=name,
                architecture=self._architecture,
                os=self._os,
            )
        except grpc.aio.AioRpcError as ex:
            status = ex.code()
            if status == grpc.StatusCode.UNKNOWN or status == grpc.StatusCode.NOT_FOUND:
                raise ImageNotFoundError(name)
            raise


def _get_value(d: Mapping[str, Any], *key: str) -> Any:
    for k in key:
        v = d.get(k)
        if v is not None:
            return v
    raise ValueError(f"Dictionary has no keys {key!r}")


def _create_digest(value: str | bytes) -> str:
    data = value.encode() if isinstance(value, str) else value
    digest = hashlib.sha256(data).hexdigest()
    return "sha256:" + digest


def _create_digest_chain(*data: str | bytes) -> str:
    data0 = data[0].encode() if isinstance(data[0], str) else data[0]
    if len(data) == 1:
        return data0.decode()
    data1 = data[1].encode() if isinstance(data[1], str) else data[1]
    digest = hashlib.sha256(b" ".join((data0, data1))).hexdigest()
    return _create_digest_chain("sha256:" + digest, *data[2:])

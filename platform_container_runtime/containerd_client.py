import enum
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Mapping, Union

import grpc.aio
from google.protobuf.timestamp_pb2 import Timestamp

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


logger = logging.getLogger(__name__)


class ContainerdError(Exception):
    pass


class ContainerNotFoundError(ContainerdError):
    def __init__(self, container_id: str) -> None:
        super().__init__(f"Container {container_id!r} not found")


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
class Container:
    id: str
    image: str
    snapshotter: str
    snapshot_key: str


@dataclass(frozen=True)
class Descriptor:
    media_type: str
    digest: str
    size: int

    @classmethod
    def from_data(cls, media_type: str, data: Dict[str, Any]) -> "Descriptor":
        dump = json.dumps(data).encode()
        return cls(media_type=media_type, digest=_create_digest(dump), size=len(dump))

    def to_primitive(self) -> Dict[str, Any]:
        return {"mediaType": self.media_type, "digest": self.digest, "size": self.size}

    def __str__(self) -> str:
        return f"({self.media_type},{self.digest},{self.size})"

    def __repr__(self) -> str:
        return self.__str__()


class ContainerdClient:
    def __init__(
        self,
        channel: grpc.aio.Channel,
        architecture: str,
        os: str,
        namespace: str = "k8s.io",
    ) -> None:
        self._namespace = namespace
        self._architecture = architecture.lower()
        self._os = os.lower()
        self._leases_stub = LeasesStub(channel)
        self._containers_stub = ContainersStub(channel)
        self._tasks_stub = TasksStub(channel)
        self._snapshots_stub = SnapshotsStub(channel)
        self._diff_stub = DiffStub(channel)
        self._images_stub = ImagesStub(channel)
        self._content_stub = ContentStub(channel)

    async def get_container(self, container_id: str) -> Container:
        try:
            resp = await self._containers_stub.Get(
                GetContainerRequest(id=container_id),
                metadata=(("containerd-namespace", self._namespace),),
            )
            return Container(
                id=resp.container.id,
                image=resp.container.image,
                snapshotter=resp.container.snapshotter,
                snapshot_key=resp.container.snapshot_key,
            )
        except grpc.aio.AioRpcError as ex:
            status = ex.code()
            if status == grpc.StatusCode.UNKNOWN or status == grpc.StatusCode.NOT_FOUND:
                raise ContainerNotFoundError(container_id)
            raise

    async def commit(self, container_id: str, image: str) -> None:
        resp = await self._containers_stub.Get(
            GetContainerRequest(id=container_id),
            metadata=(("containerd-namespace", self._namespace),),
        )
        async with self._pause_container(container_id):
            async with self._lease(timedelta(hours=1)) as lease_id:
                await self._commit(
                    snapshotter=resp.container.snapshotter,
                    snapshot_key=resp.container.snapshot_key,
                    parent_image=resp.container.image,
                    image=image,
                    lease_id=lease_id,
                )

    @asynccontextmanager
    async def _lease(self, duration: timedelta) -> AsyncIterator[str]:
        # leases are used to tell Containerd garbage collector
        # to not delete resources while lease exists or is not expired
        expire = datetime.now(timezone.utc) + duration
        resp = await self._leases_stub.Create(
            CreateRequest(labels={"containerd.io/gc.expire": expire.isoformat()}),
            metadata=(("containerd-namespace", self._namespace),),
        )
        lease_id = resp.lease.id
        logger.info("Created lease %r", lease_id)
        yield lease_id
        await self._leases_stub.Delete(
            DeleteRequest(id=lease_id),
            metadata=(("containerd-namespace", self._namespace),),
        )
        logger.info("Removed lease %r", lease_id)

    @asynccontextmanager
    async def _pause_container(self, container_id: str) -> AsyncIterator[None]:
        resp = await self._tasks_stub.Get(
            GetRequest(container_id=container_id),
            metadata=(("containerd-namespace", self._namespace),),
        )
        if resp.process.status in (CREATED, PAUSED, STOPPED):
            logger.info("Container %r is not running", container_id)
            return
        await self._tasks_stub.Pause(
            PauseTaskRequest(container_id=container_id),
            metadata=(("containerd-namespace", self._namespace),),
        )
        logger.info("Container %r paused", container_id)
        yield
        await self._tasks_stub.Resume(
            ResumeTaskRequest(container_id=container_id),
            metadata=(("containerd-namespace", self._namespace),),
        )
        logger.info("Container %r resumed", container_id)

    async def _commit(
        self,
        snapshotter: str,
        snapshot_key: str,
        parent_image: str,
        image: str,
        lease_id: str,
    ) -> None:
        image_diff_desc = await self._get_image_diff_desc(
            snapshotter=snapshotter, snapshot_key=snapshot_key, lease_id=lease_id
        )
        logger.info("Created new image layer %r", image_diff_desc)
        image_diff_id = await self._get_image_diff_id(image_diff_desc)
        image_manifest_desc = await self._write_image_content(
            snapshotter=snapshotter,
            parent_image=parent_image,
            image_diff_desc=image_diff_desc,
            image_diff_id=image_diff_id,
            lease_id=lease_id,
        )
        await self._create_image(image, image_manifest_desc)

    async def _get_image_diff_desc(
        self, snapshotter: str, snapshot_key: str, lease_id: str
    ) -> Descriptor:
        resp = await self._snapshots_stub.Stat(
            StatSnapshotRequest(
                snapshotter=snapshotter,
                key=snapshot_key,
            ),
            metadata=(
                ("containerd-namespace", self._namespace),
                ("containerd-lease", lease_id),
            ),
        )
        lower_key = f"{snapshot_key}-parent-view"
        resp = await self._snapshots_stub.View(
            ViewSnapshotRequest(
                snapshotter=snapshotter,
                key=lower_key,
                parent=resp.info.parent,
            ),
            metadata=(("containerd-namespace", self._namespace),),
        )
        logger.info("Created parent snapshot vew %r", lower_key)
        lower_mounts = resp.mounts
        resp = await self._snapshots_stub.Mounts(
            MountsRequest(snapshotter=snapshotter, key=snapshot_key),
            metadata=(("containerd-namespace", self._namespace),),
        )
        upper_mounts = resp.mounts
        await self._snapshots_stub.Remove(
            RemoveSnapshotRequest(snapshotter=snapshotter, key=lower_key),
            metadata=(("containerd-namespace", self._namespace),),
        )
        logger.info("Removed parent snapshot vew %r", lower_key)
        resp = await self._diff_stub.Diff(
            DiffRequest(left=lower_mounts, right=upper_mounts),
            metadata=(
                ("containerd-namespace", self._namespace),
                ("containerd-lease", lease_id),
            ),
        )
        return Descriptor(
            # replace media type with docker compatible
            media_type=MediaType.DOCKER_IMAGE_LAYER_GZIP.value,
            digest=resp.diff.digest,
            size=resp.diff.size,
        )

    async def _get_image_diff_id(self, desc: Descriptor) -> str:
        resp = await self._content_stub.Info(
            InfoRequest(digest=desc.digest),
            metadata=(("containerd-namespace", self._namespace),),
        )
        return resp.info.labels["containerd.io/uncompressed"]

    async def _write_image_content(
        self,
        snapshotter: str,
        parent_image: str,
        image_diff_desc: Descriptor,
        image_diff_id: str,
        lease_id: str,
    ) -> Descriptor:
        parent_image_manifest = await self._read_image_manifest(parent_image)
        parent_image_config = await self._read_image_config(parent_image_manifest)
        image_config = self._create_image_config(parent_image_config, image_diff_id)
        image_config_desc = Descriptor.from_data(
            MediaType.DOCKER_IMAGE_CONFIG_V1, image_config
        )
        await self._write_image_config_content(
            snapshotter=snapshotter,
            image_config=image_config,
            image_config_desc=image_config_desc,
            lease_id=lease_id,
        )
        image_manifest = self._create_image_manifest(
            parent_image_manifest,
            image_config_desc=image_config_desc,
            image_diff_desc=image_diff_desc,
        )
        image_manifest_desc = Descriptor.from_data(
            MediaType.DOCKER_MANIFEST_V2, image_manifest
        )
        await self._write_image_manifest_content(
            image_manifest=image_manifest,
            image_manifest_desc=image_manifest_desc,
            image_config_desc=image_config_desc,
            lease_id=lease_id,
        )
        return image_manifest_desc

    async def _write_image_config_content(
        self,
        snapshotter: str,
        image_config: Dict[str, Any],
        image_config_desc: Descriptor,
        lease_id: str,
    ) -> None:
        diff_ids_digest = _create_digest_chain(*image_config["rootfs"]["diff_ids"])
        async for resp in self._content_stub.Write(
            [
                WriteContentRequest(
                    action=COMMIT,
                    ref=image_config_desc.digest,
                    data=json.dumps(image_config).encode(),
                    offset=0,
                    total=image_config_desc.size,
                    expected=image_config_desc.digest,
                    labels={
                        f"containerd.io/gc.ref.snapshot.{snapshotter}": diff_ids_digest
                    },
                ),
            ],
            metadata=(
                ("containerd-namespace", self._namespace),
                ("containerd-lease", lease_id),
            ),
        ):
            assert resp.action == COMMIT
            assert resp.offset == image_config_desc.size, "Not all data was written"
            assert resp.digest == image_config_desc.digest, "Data is corrupted"
        logger.info("Created image config content %r", image_config_desc.digest)

    async def _write_image_manifest_content(
        self,
        image_manifest: Dict[str, Any],
        image_manifest_desc: Descriptor,
        image_config_desc: Descriptor,
        lease_id: str,
    ) -> None:
        labels = {"containerd.io/gc.ref.content.0": image_config_desc.digest}
        for i, layer in enumerate(image_manifest["layers"]):
            digest = _get_value(layer, "digest", "Digest")
            labels[f"containerd.io/gc.ref.content.{i + 1}"] = digest

        async for resp in self._content_stub.Write(
            [
                WriteContentRequest(
                    action=COMMIT,
                    ref=image_manifest_desc.digest,
                    data=json.dumps(image_manifest).encode(),
                    offset=0,
                    total=image_manifest_desc.size,
                    expected=image_manifest_desc.digest,
                    labels=labels,
                ),
            ],
            metadata=(
                ("containerd-namespace", self._namespace),
                ("containerd-lease", lease_id),
            ),
        ):
            assert resp.action == COMMIT
            assert resp.offset == image_manifest_desc.size, "Not all data was written"
            assert resp.digest == image_manifest_desc.digest, "Data is corrupted"
        logger.info("Created image manifest %r", image_manifest_desc.digest)

    def _create_image_config(
        self,
        parent_image_config: Dict[str, Any],
        image_diff_id: str,
    ) -> Dict[str, Any]:
        config = _get_value(parent_image_config, "config", "Config")
        root_fs = _get_value(parent_image_config, "rootfs", "RootFS")
        layers = _get_value(root_fs, "diff_ids", "Diff_ids")
        return {
            "architecture": self._architecture,
            "os": self._os,
            "config": config,
            "rootfs": {
                "type": "layers",
                "diff_ids": layers + [image_diff_id],
            },
            "author": "",
            "created": datetime.now(timezone.utc).isoformat(),
        }

    def _create_image_manifest(
        self,
        parent_image_manifest: Dict[str, Any],
        image_config_desc: Descriptor,
        image_diff_desc: Descriptor,
    ) -> Dict[str, Any]:
        layers = _get_value(parent_image_manifest, "layers", "Layers")
        return {
            "schemaVersion": 2,
            "mediaType": MediaType.DOCKER_MANIFEST_V2,
            "config": image_config_desc.to_primitive(),
            "layers": layers + [image_diff_desc.to_primitive()],
        }

    async def _read_image_manifest(self, image: str) -> Dict[str, Any]:
        resp = await self._images_stub.Get(
            GetImageRequest(name=image),
            metadata=(("containerd-namespace", self._namespace),),
        )
        digest = resp.image.target.digest

        while True:
            data: List[bytes] = []
            async for resp in self._content_stub.Read(
                ReadContentRequest(
                    digest=digest,
                    offset=0,  # from the start
                    size=0,  # entire content
                ),
                metadata=(("containerd-namespace", self._namespace),),
            ):
                data.append(resp.data)

            logger.info("Read content %s", digest)
            content = json.loads(b"".join(data))
            media_type = _get_value(content, "mediaType", "MediaType")

            if media_type in (
                MediaType.DOCKER_MANIFEST_V2,
                MediaType.OCI_IMAGE_MANIFEST_V1,
            ):
                return content

            if media_type in (
                MediaType.DOCKER_MANIFEST_LIST_V2,
                MediaType.OCI_IMAGE_INDEX_V1,
            ):
                for m in content["manifests"]:
                    platform = _get_value(m, "platform", "Platform")
                    os = _get_value(platform, "os", "Os")
                    arch = _get_value(platform, "architecture", "Architecture")
                    if os.lower() == self._os and arch.lower() == self._architecture:
                        digest = m["digest"]
                        break
                else:
                    raise ContainerdError(f"Platform ({os},{arch}) is not supported")
                continue

            raise ContainerdError(f"Media type {media_type!r} is not supported")

    async def _read_image_config(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        config = _get_value(manifest, "config", "Config")
        digest = _get_value(config, "digest", "Digest")
        data: List[bytes] = []
        async for resp in self._content_stub.Read(
            ReadContentRequest(
                digest=digest,
                offset=0,  # from the start
                size=0,  # entire content
            ),
            metadata=(("containerd-namespace", self._namespace),),
        ):
            data.append(resp.data)
        return json.loads(b"".join(data))

    async def _create_image(self, image: str, image_manifest_desc: Descriptor) -> None:
        created_at = Timestamp()
        created_at.GetCurrentTime()
        image_pb2 = ImagePb2(
            name=image,
            target=DescriptorPb2(
                media_type=image_manifest_desc.media_type,
                digest=image_manifest_desc.digest,
                size=image_manifest_desc.size,
            ),
            created_at=created_at,
        )
        try:
            await self._images_stub.Update(
                UpdateImageRequest(image=image_pb2),
                metadata=(("containerd-namespace", self._namespace),),
            )
            logger.info("Updated image %r", image)
        except grpc.aio.AioRpcError as ex:
            status = ex.code()
            if status == grpc.StatusCode.UNKNOWN or status == grpc.StatusCode.NOT_FOUND:
                await self._images_stub.Create(
                    CreateImageRequest(image=image_pb2),
                    metadata=(("containerd-namespace", self._namespace),),
                )
                logger.info("Created image %r", image)
                return
            raise


def _get_value(d: Mapping[str, Any], *key: str) -> Any:
    for k in key:
        v = d.get(k)
        if v is not None:
            return v
    raise ValueError(f"Dictionary has no keys {key!r}")


def _create_digest(value: Union[str, bytes]) -> str:
    data = value.encode() if isinstance(value, str) else value
    digest = hashlib.sha256(data).hexdigest()
    return "sha256:" + digest


def _create_digest_chain(*data: Union[str, bytes]) -> str:
    data0 = data[0].encode() if isinstance(data[0], str) else data[0]
    if len(data) == 1:
        return data0.decode()
    data1 = data[1].encode() if isinstance(data[1], str) else data[1]
    digest = hashlib.sha256(b" ".join((data0, data1))).hexdigest()
    return _create_digest_chain("sha256:" + digest, *data[2:])

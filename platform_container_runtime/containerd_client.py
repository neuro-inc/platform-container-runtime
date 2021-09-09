import enum
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Union

import grpc.aio
from google.protobuf.timestamp_pb2 import Timestamp
from neuro_logging import trace

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


@dataclass(frozen=True)
class Stubs:
    leases: LeasesStub
    containers: ContainersStub
    tasks: TasksStub
    snapshots: SnapshotsStub
    diff: DiffStub
    images: ImagesStub
    content: ContentStub

    @classmethod
    def create(cls, channel: grpc.aio.Channel) -> "Stubs":
        return cls(
            leases=LeasesStub(channel),
            containers=ContainersStub(channel),
            tasks=TasksStub(channel),
            snapshots=SnapshotsStub(channel),
            diff=DiffStub(channel),
            images=ImagesStub(channel),
            content=ContentStub(channel),
        )


class Metadata(grpc.aio.Metadata):
    def __init__(
        self, *, namespace: Optional[str] = None, lease_id: Optional[str] = None
    ) -> None:
        super().__init__()
        if namespace:
            self.add("containerd-namespace", namespace)
        if lease_id:
            self.add("containerd-lease", lease_id)


# leases are used to tell Containerd garbage collector
# to not delete resources while lease exists or is not expired
class Lease:
    def __init__(self, stubs: Stubs, namespace: str, duration: timedelta) -> None:
        self._leases_stub = stubs.leases
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


class Snapshot:
    def __init__(
        self,
        stubs: Stubs,
        namespace: str,
        snapshotter: str,
        snapshot_key: str,
    ) -> None:
        self._stubs = stubs
        self._namespace = namespace
        self._snapshotter = snapshotter
        self._snapshot_key = snapshot_key

    @property
    def snapshotter(self) -> str:
        return self._snapshotter

    @property
    def snapshot_key(self) -> str:
        return self._snapshot_key

    @trace
    async def get_diff(self, lease_id: Optional[str] = None) -> SnapshotDiff:
        resp = await self._stubs.snapshots.Stat(
            StatSnapshotRequest(snapshotter=self._snapshotter, key=self._snapshot_key),
            metadata=Metadata(namespace=self._namespace),
        )
        lower_key = f"{self._snapshot_key}-parent-view"
        resp = await self._stubs.snapshots.View(
            ViewSnapshotRequest(
                snapshotter=self._snapshotter,
                key=lower_key,
                parent=resp.info.parent,
            ),
            metadata=Metadata(namespace=self._namespace, lease_id=lease_id),
        )
        logger.info("Created parent snapshot vew %r", lower_key)
        lower_mounts = resp.mounts
        resp = await self._stubs.snapshots.Mounts(
            MountsRequest(snapshotter=self._snapshotter, key=self._snapshot_key),
            metadata=Metadata(namespace=self._namespace),
        )
        upper_mounts = resp.mounts
        await self._stubs.snapshots.Remove(
            RemoveSnapshotRequest(snapshotter=self._snapshotter, key=lower_key),
            metadata=Metadata(namespace=self._namespace),
        )
        logger.info("Removed parent snapshot vew %r", lower_key)
        resp = await self._stubs.diff.Diff(
            DiffRequest(left=lower_mounts, right=upper_mounts),
            metadata=Metadata(namespace=self._namespace, lease_id=lease_id),
        )
        desc = Descriptor(
            # replace media type with docker compatible
            media_type=MediaType.DOCKER_IMAGE_LAYER_GZIP.value,
            digest=resp.diff.digest,
            size=resp.diff.size,
        )
        resp = await self._stubs.content.Info(
            InfoRequest(digest=desc.digest),
            metadata=Metadata(namespace=self._namespace),
        )
        return SnapshotDiff(
            id=resp.info.labels["containerd.io/uncompressed"], descriptor=desc
        )


class ImageManifest(Dict[str, Any]):
    def __init__(self, stubs: Stubs, namespace: str, content: Dict[str, Any]) -> None:
        super().__init__(content)
        self._stubs = stubs
        self._namespace = namespace

    @property
    def config(self) -> Dict[str, Any]:
        return _get_value(self, "config", "Config")

    @classmethod
    @trace
    async def read(
        cls,
        stubs: Stubs,
        namespace: str,
        architecture: str,
        os: str,
        descriptor: Descriptor,
    ) -> "ImageManifest":
        manifest = await cls._read_manifest(
            stubs,
            namespace=namespace,
            architecture=architecture,
            os=os,
            digest=descriptor.digest,
        )
        config = _get_value(manifest, "config", "Config")
        config_digest = _get_value(config, "digest", "Digest")
        manifest["config"] = await cls._read_config(
            stubs, namespace=namespace, digest=config_digest
        )
        return cls(stubs=stubs, namespace=namespace, content=manifest)

    @classmethod
    @trace
    async def _read_manifest(
        cls, stubs: Stubs, namespace: str, architecture: str, os: str, digest: str
    ) -> Dict[str, Any]:
        while True:
            data: List[bytes] = []
            async for resp in stubs.content.Read(
                ReadContentRequest(
                    digest=digest,
                    offset=0,  # from the start
                    size=0,  # entire content
                ),
                metadata=Metadata(namespace=namespace),
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
                    if os.lower() == os and arch.lower() == architecture:
                        digest = m["digest"]
                        break
                else:
                    raise ContainerdError(f"Platform ({os},{arch}) is not supported")
                continue

            raise ContainerdError(f"Media type {media_type!r} is not supported")

    @classmethod
    @trace
    async def _read_config(
        cls, stubs: Stubs, namespace: str, digest: str
    ) -> Dict[str, Any]:
        data: List[bytes] = []
        async for resp in stubs.content.Read(
            ReadContentRequest(
                digest=digest,
                offset=0,  # from the start
                size=0,  # entire content
            ),
            metadata=Metadata(namespace=namespace),
        ):
            data.append(resp.data)
        return json.loads(b"".join(data))

    @trace
    async def write(
        self,
        config_labels: Optional[Dict[str, Any]] = None,
        lease_id: Optional[str] = None,
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
        content: Dict[str, Any],
        desc: Descriptor,
        lease_id: Optional[str] = None,
    ) -> None:
        labels = {"containerd.io/gc.ref.content.0": content["config"]["digest"]}
        for i, layer in enumerate(content["layers"]):
            digest = _get_value(layer, "digest", "Digest")
            labels[f"containerd.io/gc.ref.content.{i + 1}"] = digest

        async for resp in self._stubs.content.Write(
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
        content: Dict[str, Any],
        desc: Descriptor,
        labels: Optional[Dict[str, Any]] = None,
        lease_id: Optional[str] = None,
    ) -> None:
        async for resp in self._stubs.content.Write(
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


class Image:
    def __init__(
        self, stubs: Stubs, namespace: str, name: str, manifest: ImageManifest
    ) -> None:
        self._stubs = stubs
        self._namespace = namespace
        self._name = name
        self._manifest = manifest

    @property
    def config(self) -> Dict[str, Any]:
        return self._manifest.config

    @property
    def layers(self) -> List[Dict[str, Any]]:
        return list(_get_value(self._manifest, "layers", "Layers"))

    @classmethod
    @trace
    async def read(
        cls, stubs: Stubs, namespace: str, name: str, architecture: str, os: str
    ) -> "Image":
        resp = await stubs.images.Get(
            GetImageRequest(name=name),
            metadata=Metadata(namespace=namespace),
        )
        manifest_descriptor = Descriptor(
            media_type=resp.image.target.media_type,
            digest=resp.image.target.digest,
            size=resp.image.target.size,
        )
        manifest = await ImageManifest.read(
            stubs,
            namespace=namespace,
            architecture=architecture,
            os=os,
            descriptor=manifest_descriptor,
        )
        return cls(stubs=stubs, namespace=namespace, name=name, manifest=manifest)

    @trace
    async def write(
        self,
        config_labels: Optional[Dict[str, Any]] = None,
        lease_id: Optional[str] = None,
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
            await self._stubs.images.Update(
                UpdateImageRequest(image=image_pb2),
                metadata=Metadata(namespace=self._namespace),
            )
            logger.info("Updated image %r", self._name)
        except grpc.aio.AioRpcError as ex:
            status = ex.code()
            if status == grpc.StatusCode.UNKNOWN or status == grpc.StatusCode.NOT_FOUND:
                await self._stubs.images.Create(
                    CreateImageRequest(image=image_pb2),
                    metadata=Metadata(namespace=self._namespace),
                )
                logger.info("Created image %r", self._name)
                return
            raise


class Container:
    def __init__(
        self,
        stubs: Stubs,
        namespace: str,
        architecture: str,
        os: str,
        id: str,
        image: str,
        snapshot: Snapshot,
    ) -> None:
        self._stubs = stubs
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
        resp = await self._stubs.tasks.Get(
            GetRequest(container_id=self._id),
            metadata=Metadata(namespace=self._namespace),
        )
        if resp.process.status in (CREATED, PAUSED, STOPPED):
            logger.info("Container %r is not running", self._id)
            return False
        await self._stubs.tasks.Pause(
            PauseTaskRequest(container_id=self._id),
            metadata=Metadata(namespace=self._namespace),
        )
        logger.info("Container %r paused", self._id)
        return True

    @trace
    async def resume(self) -> None:
        await self._stubs.tasks.Resume(
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
                self._stubs, self._namespace, timedelta(hours=1)
            ) as lease_id:
                parent_image = await Image.read(
                    self._stubs,
                    namespace=self._namespace,
                    name=self._image,
                    architecture=self._architecture,
                    os=self._os,
                )
                image_diff = await self._snapshot.get_diff(lease_id=lease_id)
                logger.info("Created new image layer %r", image_diff.descriptor)
                new_image_manifest = ImageManifest(
                    self._stubs,
                    namespace=self._namespace,
                    content=self._create_commit_image_manifest(
                        parent_image, image_diff
                    ),
                )
                new_image_diff_ids_digest = _create_digest_chain(
                    *new_image_manifest.config["rootfs"]["diff_ids"]
                )
                new_image = Image(
                    self._stubs,
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

    def _create_commit_image_manifest(
        self, parent_image: Image, image_diff: SnapshotDiff
    ) -> Dict[str, Any]:
        return {
            "schemaVersion": 2,
            "mediaType": MediaType.DOCKER_MANIFEST_V2,
            "config": self._create_commit_image_config(parent_image, image_diff),
            "layers": parent_image.layers + [image_diff.descriptor.to_primitive()],
        }

    def _create_commit_image_config(
        self, parent_image: Image, image_diff: SnapshotDiff
    ) -> Dict[str, Any]:
        root_fs = _get_value(parent_image.config, "rootfs", "RootFS")
        layers = _get_value(root_fs, "diff_ids", "Diff_ids")
        return {
            "architecture": self._architecture,
            "os": self._os,
            "config": parent_image.config,
            "rootfs": {
                "type": "layers",
                "diff_ids": layers + [image_diff.id],
            },
            "author": "",
            "created": datetime.now(timezone.utc).isoformat(),
        }


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
        self._stubs = Stubs.create(channel)

    @trace
    async def get_container(self, container_id: str) -> Container:
        try:
            resp = await self._stubs.containers.Get(
                GetContainerRequest(id=container_id),
                metadata=Metadata(namespace=self._namespace),
            )
            return Container(
                stubs=self._stubs,
                namespace=self._namespace,
                architecture=self._architecture,
                os=self._os,
                id=resp.container.id,
                image=resp.container.image,
                snapshot=Snapshot(
                    stubs=self._stubs,
                    namespace=self._namespace,
                    snapshotter=resp.container.snapshotter,
                    snapshot_key=resp.container.snapshot_key,
                ),
            )
        except grpc.aio.AioRpcError as ex:
            status = ex.code()
            if status == grpc.StatusCode.UNKNOWN or status == grpc.StatusCode.NOT_FOUND:
                raise ContainerNotFoundError(container_id)
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

import grpc.aio

from containerd.services.containers.v1.containers_pb2 import GetContainerRequest
from containerd.services.containers.v1.containers_pb2_grpc import ContainersStub
from containerd.services.images.v1.images_pb2_grpc import ImagesStub
from containerd.services.snapshots.v1.snapshots_pb2_grpc import SnapshotsStub
from containerd.services.tasks.v1.tasks_pb2_grpc import TasksStub


class ContainerdClient:
    def __init__(self, channel: grpc.aio.Channel, namespace: str = "k8s.io") -> None:
        self._namespace = namespace
        self._containers_stub = ContainersStub(channel)
        self._tasks_stub = TasksStub(channel)
        self._snapshots_stub = SnapshotsStub(channel)
        self._images_stub = ImagesStub(channel)

    async def commit(self, container_id: str) -> None:
        get_cont_resp = await self._containers_stub.Get(
            GetContainerRequest(id=container_id),
            metadata=(("containerd-namespace", self._namespace),),
        )
        print(get_cont_resp)

        # parent_image = get_cont_resp.container.image
        # get_cont_resp.container.snapshotter
        # get_cont_resp.container.snapshot_key

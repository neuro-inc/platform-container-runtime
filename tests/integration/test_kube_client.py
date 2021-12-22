import pytest

from platform_container_runtime.kube_client import KubeClient

pytestmark = pytest.mark.asyncio


class TestKubeClient:
    async def test_get_node(self, kube_client: KubeClient) -> None:
        nodes = await kube_client.get_nodes()

        assert nodes

        node = await kube_client.get_node(nodes[0].metadata.name)

        assert node.metadata.labels
        assert node.container_runtime_version
        assert node.os
        assert node.architecture

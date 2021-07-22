import pytest

from platform_container_runtime.kube_client import KubeClient


pytestmark = pytest.mark.asyncio


class TestKubeClient:
    async def test_get_node(self, kube_client: KubeClient) -> None:
        node = await kube_client.get_node("minikube")

        assert node.metadata.name == "minikube"
        assert node.metadata.labels
        assert node.container_runtime_version.startswith("docker://")

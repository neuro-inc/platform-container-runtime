import asyncio
import json
import os
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml
from yarl import URL

from platform_container_runtime.kube_client import (
    KubeClient,
    KubeClientAuthType,
    KubeConfig,
    Node,
)


@dataclass(frozen=True)
class Pod:
    name: str
    container_id: str


async def get_pod(
    pod_name: str,
    namespace: str = "default",
) -> dict[str, Any]:
    process = await asyncio.create_subprocess_shell(
        f"kubectl --context=minikube -n {namespace} get pod {pod_name} -o json",
        stdout=subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    return json.loads(stdout)


async def get_container_id(
    pod_name: str,
    namespace: str = "default",
    timeout_s: int = 30,
    interval_s: int = 1,
    expected_status: str = "running",
) -> str:
    while timeout_s:
        output = await get_pod(pod_name, namespace=namespace)
        container_statuses = output["status"].get("containerStatuses", ())

        if not container_statuses or any(
            expected_status not in s.get("state", {}) for s in container_statuses
        ):
            time.sleep(interval_s)
            timeout_s -= interval_s
            continue

        return container_statuses[0]["containerID"]

    pytest.fail(f"Pod {pod_name!r} container is not in {expected_status!r} status.")


@asynccontextmanager
async def run(
    image: str,
    cmd: str,
    image_pull_policy: str = "",
    namespace: str = "default",
    tty: bool = False,
    stdin: bool = False,
    attach: bool = False,
    timeout_s: int = 60,
    interval_s: int = 1,
    restart: str = "Never",
    expected_status: str = "running",
) -> AsyncIterator[Pod]:
    pod_name = str(uuid.uuid4())
    opts = f" --attach={str(attach).lower()}"

    if stdin:
        opts += " -i --leave-stdin-open=true"

    if tty:
        opts += " -t"

    if image_pull_policy:
        opts += f" --image-pull-policy {image_pull_policy}"

    try:
        kubectl = f"kubectl --context=minikube -n {namespace}"
        process = await asyncio.create_subprocess_shell(
            f"{kubectl} run {pod_name}{opts} "
            f"--image {image} "
            f"--restart {restart} "
            f"-- {cmd}"
        )
        await process.communicate()
        yield Pod(
            name=pod_name,
            container_id=await get_container_id(
                pod_name,
                timeout_s=timeout_s,
                interval_s=interval_s,
                expected_status=expected_status,
            ),
        )
    finally:
        process = await asyncio.create_subprocess_shell(
            f"{kubectl} delete pod {pod_name} --force --grace-period=0"
        )
        await process.communicate()


async def get_service_url(service_name: str, namespace: str = "default") -> str:
    timeout_s = 60
    interval_s = 1

    while timeout_s:
        process = await asyncio.create_subprocess_shell(
            f"minikube service -n {namespace} {service_name} --url",
            stdout=subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if stdout:
            url = stdout.decode().strip()
            # Sometimes `minikube service ... --url` returns a prefixed
            # string such as: "* https://127.0.0.1:8081/"
            start_idx = url.find("http")
            if start_idx > 0:
                url = url[start_idx:]
            return url
        time.sleep(interval_s)
        timeout_s -= interval_s

    pytest.fail(f"Service {service_name} is unavailable.")


@pytest.fixture(scope="session")
def _kube_config_payload() -> dict[str, Any]:
    kube_config_path = os.path.expanduser("~/.kube/config")
    with open(kube_config_path) as kube_config:
        return yaml.safe_load(kube_config)


@pytest.fixture(scope="session")
def _kube_config_cluster_payload(_kube_config_payload: dict[str, Any]) -> Any:
    cluster_name = "minikube"
    clusters = {
        cluster["name"]: cluster["cluster"]
        for cluster in _kube_config_payload["clusters"]
    }
    return clusters[cluster_name]


@pytest.fixture(scope="session")
def _cert_authority_data_pem(
    _kube_config_cluster_payload: dict[str, Any],
) -> Optional[str]:
    ca_path = _kube_config_cluster_payload["certificate-authority"]
    if ca_path:
        return Path(ca_path).read_text()
    return None


@pytest.fixture(scope="session")
def _kube_config_user_payload(_kube_config_payload: dict[str, Any]) -> Any:
    user_name = "minikube"
    users = {user["name"]: user["user"] for user in _kube_config_payload["users"]}
    return users[user_name]


@pytest.fixture
def kube_config(
    _kube_config_cluster_payload: dict[str, Any],
    _kube_config_user_payload: dict[str, Any],
    _cert_authority_data_pem: Optional[str],
) -> KubeConfig:
    cluster = _kube_config_cluster_payload
    user = _kube_config_user_payload
    return KubeConfig(
        url=URL(cluster["server"]),
        auth_type=KubeClientAuthType.CERTIFICATE,
        cert_authority_data_pem=_cert_authority_data_pem,
        client_cert_path=user["client-certificate"],
        client_key_path=user["client-key"],
        # https://docs.aiohttp.org/en/stable/client_advanced.html#graceful-shutdown
        conn_force_close=True,
    )


@pytest.fixture
async def kube_client(kube_config: KubeConfig) -> AsyncIterator[KubeClient]:
    async with KubeClient(kube_config) as client:
        yield client


@pytest.fixture
async def _kube_node(kube_client: KubeClient) -> Node:
    nodes = await kube_client.get_nodes()
    assert len(nodes) == 1, "Should be exactly one minikube node"
    return nodes[0]


@pytest.fixture
async def kube_node(_kube_node: Node) -> str:
    return _kube_node.metadata.name


@pytest.fixture
async def kube_container_runtime(_kube_node: Node) -> str:
    version = _kube_node.container_runtime_version
    end = version.find("://")
    return version[0:end]
